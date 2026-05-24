// imessages_reader.swift — scoped chat.db reader with AddressBook name resolution
// ===========================================================================
// Purpose: read inbound messages from ~/Library/Messages/chat.db, join each
//          sender's handle (phone/email) against the local AddressBook SQLite
//          databases, and emit one JSON object per line to stdout. Owns the
//          Full Disk Access grant so the broader Python pipeline stays
//          unprivileged.
//
// Compile + sign:
//   swiftc -O -o imessages_reader imessages_reader.swift
//   codesign -s - imessages_reader     # ad-hoc signature → stable TCC identity
//
// Grant Full Disk Access to the resulting binary in:
//   System Settings → Privacy & Security → Full Disk Access → +
// (FDA covers both chat.db and ~/Library/Application Support/AddressBook/.)
//
// Usage:
//   imessages_reader --since-rowid 12345 [--limit 5000]
// Output (one JSON object per line, ascending ROWID):
//   {"rowid":N,"cocoa_date":N,"text":"...","service":"iMessage",
//    "handle_id":"+15551234567","sender_name":"Mark Saxe","sender_org":"Tomac Cove"}
//   sender_name / sender_org are present only when the handle resolves.

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

let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()

// ── helpers ──────────────────────────────────────────────────────────────────
func normalizePhone(_ raw: String) -> String {
    var digits = raw.filter { $0.isNumber }
    // US country code strip
    if digits.count == 11, digits.hasPrefix("1") {
        digits = String(digits.dropFirst())
    }
    return digits
}

func normalizeHandle(_ raw: String) -> String {
    var s = raw
    if s.hasPrefix("tel:") { s = String(s.dropFirst(4)) }
    if s.hasPrefix("mailto:") { s = String(s.dropFirst(7)) }
    if s.contains("@") { return s.lowercased() }
    return normalizePhone(s)
}

// ── AddressBook → identity map ───────────────────────────────────────────────
// key = normalized handle (phone digits or lowercased email)
// value = (name, org)
var identityMap: [String: (name: String, org: String)] = [:]

func discoverAddressBookDBs() -> [String] {
    var paths: [String] = []
    let base = "\(home)/Library/Application Support/AddressBook"
    let legacy = "\(base)/AddressBook-v22.abcddb"
    if FileManager.default.fileExists(atPath: legacy) {
        paths.append(legacy)
    }
    let sourcesDir = "\(base)/Sources"
    if let entries = try? FileManager.default.contentsOfDirectory(atPath: sourcesDir) {
        for entry in entries {
            let candidate = "\(sourcesDir)/\(entry)/AddressBook-v22.abcddb"
            if FileManager.default.fileExists(atPath: candidate) {
                paths.append(candidate)
            }
        }
    }
    return paths
}

func loadAddressBook(_ path: String) {
    var db: OpaquePointer?
    let uri = "file:\(path)?mode=ro&immutable=1"
    if sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) != SQLITE_OK {
        FileHandle.standardError.write(
            "warn: cannot open AddressBook \(path)\n".data(using: .utf8)!)
        sqlite3_close(db)
        return
    }
    defer { sqlite3_close(db) }

    // Phones
    let phoneSQL = """
        SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, p.ZFULLNUMBER
        FROM ZABCDRECORD r
        JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
    """
    var stmt: OpaquePointer?
    if sqlite3_prepare_v2(db, phoneSQL, -1, &stmt, nil) == SQLITE_OK {
        while sqlite3_step(stmt) == SQLITE_ROW {
            let first = sqlite3_column_text(stmt, 0).map { String(cString: $0) } ?? ""
            let last  = sqlite3_column_text(stmt, 1).map { String(cString: $0) } ?? ""
            let org   = sqlite3_column_text(stmt, 2).map { String(cString: $0) } ?? ""
            let num   = sqlite3_column_text(stmt, 3).map { String(cString: $0) } ?? ""
            var fullName = "\(first) \(last)".trimmingCharacters(in: .whitespaces)
            if fullName.isEmpty { fullName = org }
            if fullName.isEmpty || num.isEmpty { continue }
            let key = normalizePhone(num)
            if !key.isEmpty && identityMap[key] == nil {
                identityMap[key] = (fullName, org)
            }
        }
    }
    sqlite3_finalize(stmt)

    // Emails (Apple ID iMessage)
    let emailSQL = """
        SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, e.ZADDRESS
        FROM ZABCDRECORD r
        JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
    """
    var stmt2: OpaquePointer?
    if sqlite3_prepare_v2(db, emailSQL, -1, &stmt2, nil) == SQLITE_OK {
        while sqlite3_step(stmt2) == SQLITE_ROW {
            let first = sqlite3_column_text(stmt2, 0).map { String(cString: $0) } ?? ""
            let last  = sqlite3_column_text(stmt2, 1).map { String(cString: $0) } ?? ""
            let org   = sqlite3_column_text(stmt2, 2).map { String(cString: $0) } ?? ""
            let email = sqlite3_column_text(stmt2, 3).map { String(cString: $0) } ?? ""
            var fullName = "\(first) \(last)".trimmingCharacters(in: .whitespaces)
            if fullName.isEmpty { fullName = org }
            if fullName.isEmpty || email.isEmpty { continue }
            let key = email.lowercased()
            if identityMap[key] == nil {
                identityMap[key] = (fullName, org)
            }
        }
    }
    sqlite3_finalize(stmt2)
}

let addressBookPaths = discoverAddressBookDBs()
for p in addressBookPaths {
    loadAddressBook(p)
}
FileHandle.standardError.write(
    "AddressBook: \(addressBookPaths.count) db(s), \(identityMap.count) handle→name entries\n"
        .data(using: .utf8)!)

// ── open chat.db read-only ───────────────────────────────────────────────────
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
// Drop the is_from_me=0 filter so we emit BOTH inbound (counterparty intel)
// and outbound (your replies — used by Python for thread-anchor inference and
// regex-based my_action extraction). Each row carries is_from_me so callers
// can choose how to treat each direction.
let sql = """
    SELECT
      m.ROWID    AS rowid,
      m.date     AS cocoa_date,
      m.text     AS text,
      m.service  AS service,
      h.id       AS handle_id,
      cmj.chat_id AS chat_id,
      m.is_from_me AS is_from_me
    FROM message m
    LEFT JOIN handle h ON m.handle_id = h.ROWID
    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
    WHERE m.ROWID > ?
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
var resolved = 0

while sqlite3_step(stmt) == SQLITE_ROW {
    let rowid = sqlite3_column_int64(stmt, 0)
    let cocoaDate = sqlite3_column_int64(stmt, 1)

    let textCStr = sqlite3_column_text(stmt, 2)
    let text = textCStr.map { String(cString: $0) } ?? ""

    let serviceCStr = sqlite3_column_text(stmt, 3)
    let service = serviceCStr.map { String(cString: $0) } ?? ""

    let handleCStr = sqlite3_column_text(stmt, 4)
    let handle = handleCStr.map { String(cString: $0) } ?? ""

    let chatId = sqlite3_column_int64(stmt, 5)
    let isFromMe = sqlite3_column_int(stmt, 6)

    var obj: [String: Any] = [
        "rowid": rowid,
        "cocoa_date": cocoaDate,
        "text": text,
        "service": service,
        "handle_id": handle,
        "chat_id": chatId,
        "is_from_me": Int(isFromMe),
    ]
    let key = normalizeHandle(handle)
    if !key.isEmpty, let identity = identityMap[key] {
        obj["sender_name"] = identity.name
        if !identity.org.isEmpty {
            obj["sender_org"] = identity.org
        }
        resolved += 1
    }
    guard
        let data = try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes])
    else { continue }
    stdoutHandle.write(data)
    stdoutHandle.write("\n".data(using: .utf8)!)
    rows += 1
}

FileHandle.standardError.write(
    "emitted \(rows) row(s), \(resolved) resolved to a contact\n".data(using: .utf8)!)
exit(0)
