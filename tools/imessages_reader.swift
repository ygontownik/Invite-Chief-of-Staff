// imessages_reader.swift — scoped chat.db reader for TCIP iMessage capture
// ===========================================================================
// Purpose: read inbound messages from ~/Library/Messages/chat.db and emit
//          one JSON object per line to stdout. Owns the Full Disk Access
//          grant so the broader Python pipeline doesn't need it.
//
// Compile + sign:
//   swiftc -O -o imessages_reader imessages_reader.swift
//   codesign -s - imessages_reader     # ad-hoc signature → stable TCC identity
//
// Grant Full Disk Access to the resulting binary in:
//   System Settings → Privacy & Security → Full Disk Access → +
//
// Usage:
//   imessages_reader --since-rowid 12345 [--limit 5000]
// Output (one JSON object per line, ascending ROWID):
//   {"rowid":N,"cocoa_date":N,"text":"...","service":"iMessage","handle_id":"+15551234567"}

import Foundation
import SQLite3

// ── arg parsing ──────────────────────────────────────────────────────────────
var sinceRowid: Int64 = 0
var limit: Int = 5000
var dbPathOverride: String? = nil  // for testing

let args = CommandLine.arguments
var i = 1
while i < args.count {
    let a = args[i]
    switch a {
    case "--since-rowid":
        i += 1
        if i < args.count, let v = Int64(args[i]) { sinceRowid = v }
    case "--limit":
        i += 1
        if i < args.count, let v = Int(args[i]) { limit = max(1, min(v, 50_000)) }
    case "--db":
        i += 1
        if i < args.count { dbPathOverride = args[i] }
    case "-h", "--help":
        print("usage: imessages_reader [--since-rowid N] [--limit N] [--db PATH]")
        exit(0)
    default:
        FileHandle.standardError.write("unknown arg: \(a)\n".data(using: .utf8)!)
        exit(2)
    }
    i += 1
}

// ── open chat.db read-only ───────────────────────────────────────────────────
let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
let dbPath = dbPathOverride ?? "\(home)/Library/Messages/chat.db"

var db: OpaquePointer?
let openFlags = SQLITE_OPEN_READONLY
let rc = sqlite3_open_v2(dbPath, &db, openFlags, nil)
if rc != SQLITE_OK {
    let msg = String(cString: sqlite3_errstr(rc))
    FileHandle.standardError.write(
        "error: cannot open \(dbPath): \(msg)\n".data(using: .utf8)!)
    if rc == SQLITE_CANTOPEN || rc == SQLITE_AUTH {
        FileHandle.standardError.write(
            "Hint: grant Full Disk Access to this binary in System Settings → Privacy & Security.\n"
                .data(using: .utf8)!)
    }
    exit(3)
}
defer { sqlite3_close(db) }

// ── prepare query ────────────────────────────────────────────────────────────
let sql = """
    SELECT
      m.ROWID    AS rowid,
      m.date     AS cocoa_date,
      m.text     AS text,
      m.service  AS service,
      h.id       AS handle_id
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    WHERE m.is_from_me = 0
      AND m.ROWID > ?
      AND m.text IS NOT NULL
      AND length(m.text) > 0
    ORDER BY m.ROWID ASC
    LIMIT ?
"""

var stmt: OpaquePointer?
if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) != SQLITE_OK {
    let msg = String(cString: sqlite3_errmsg(db))
    FileHandle.standardError.write("error: prepare failed: \(msg)\n".data(using: .utf8)!)
    exit(4)
}
defer { sqlite3_finalize(stmt) }

sqlite3_bind_int64(stmt, 1, sinceRowid)
sqlite3_bind_int(stmt, 2, Int32(limit))

// ── stream rows as JSONL ─────────────────────────────────────────────────────
let stdoutHandle = FileHandle.standardOutput
var rows = 0

while sqlite3_step(stmt) == SQLITE_ROW {
    let rowid = sqlite3_column_int64(stmt, 0)
    let cocoaDate = sqlite3_column_int64(stmt, 1)

    let textCStr = sqlite3_column_text(stmt, 2)
    let text = textCStr.map { String(cString: $0) } ?? ""

    let serviceCStr = sqlite3_column_text(stmt, 3)
    let service = serviceCStr.map { String(cString: $0) } ?? ""

    let handleCStr = sqlite3_column_text(stmt, 4)
    let handle = handleCStr.map { String(cString: $0) } ?? ""

    let obj: [String: Any] = [
        "rowid": rowid,
        "cocoa_date": cocoaDate,
        "text": text,
        "service": service,
        "handle_id": handle,
    ]
    guard
        let data = try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes])
    else { continue }
    stdoutHandle.write(data)
    stdoutHandle.write("\n".data(using: .utf8)!)
    rows += 1
}

FileHandle.standardError.write("emitted \(rows) row(s)\n".data(using: .utf8)!)
exit(0)
