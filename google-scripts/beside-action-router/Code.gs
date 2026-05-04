/**
 * beside_router.gs
 *
 * Watches the "Beside Notes & Memos" Google Doc for new entries,
 * extracts ACTION ITEMS, and routes them to the correct dashboard docs.
 *
 * SETUP (one-time):
 *   1. Go to script.google.com → New project → paste this file
 *   2. Run installTrigger() once from the editor (Run menu)
 *   3. Approve the permissions popup
 *   Done — runs automatically every 15 minutes from Google's servers.
 *
 * To uninstall: run removeTrigger() from the editor.
 *
 * MULTI-TENANT NOTE:
 *   The three DOC_ID constants below are TOMAC-SPECIFIC. For multi-tenant
 *   deployment (e.g. guinea pig install), each tenant needs their own
 *   deployment of this script with their own Doc IDs. Track J / setup.sh
 *   should template these from firm_context.yaml at install time.
 */

var BESIDE_DOC_ID     = "1fl05-kYeeJuORNNwiMx0yhnRQWhKZ9Gd-1FtosznHg4";
var FOLLOWUPS_DOC_ID  = "10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY";
var DEAL_DOC_ID       = "1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc";
var PROCESSED_KEY     = "beside_processed_headings";


// ── Trigger install / remove ──────────────────────────────────────────────────

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === "routeNewActions") {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger("routeNewActions")
    .timeBased()
    .everyMinutes(15)
    .create();
  Logger.log("Trigger installed — runs every 15 minutes.");
}

function removeTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === "routeNewActions") {
      ScriptApp.deleteTrigger(t);
    }
  });
  Logger.log("Trigger removed.");
}


// ── Main ──────────────────────────────────────────────────────────────────────

function routeNewActions() {
  var text      = getDocText(BESIDE_DOC_ID);
  var processed = getProcessed();
  var entries   = parseEntries(text);
  var newCount  = 0;

  entries.forEach(function(entry) {
    if (processed[entry.heading]) return;

    var cosActions  = entry.actions.filter(function(a) {
      return a.dashboard === "CoS" || a.dashboard === "Both";
    });
    var dealActions = entry.actions.filter(function(a) {
      return a.dashboard === "Deal Pipeline" || a.dashboard === "Both";
    });

    if (cosActions.length > 0)  appendToFollowups(cosActions, entry.title);
    if (dealActions.length > 0) appendToDealPipeline(dealActions, entry.title);

    processed[entry.heading] = new Date().toISOString();
    newCount++;
  });

  if (newCount > 0) {
    saveProcessed(processed);
    Logger.log("Routed " + newCount + " new beside note(s).");
  } else {
    Logger.log("No new beside notes to route.");
  }
}


// ── Parsing ───────────────────────────────────────────────────────────────────

function getDocText(docId) {
  return DocumentApp.openById(docId).getBody().getText();
}

function parseEntries(text) {
  var entries = [];
  var blocks = text.split(/═{10,}/);

  blocks.forEach(function(block) {
    block = block.trim();
    if (!block) return;

    var lines   = block.split("\n");
    var heading = lines[0].trim();
    if (!heading || heading.length < 5) return;

    var title = heading.split("  —  ")[0].trim();
    if (!title) title = heading;

    var actions = parseActions(block);
    if (actions.length === 0) return;

    entries.push({ heading: heading, title: title, actions: actions });
  });

  return entries;
}

function parseActions(block) {
  var actions = [];
  var re = /\[ACTION-\d+\]([\s\S]*?)(?=\[ACTION-\d+\]|$)/g;
  var match;

  while ((match = re.exec(block)) !== null) {
    var chunk = match[1];
    var action = {};

    ["Date/Deadline", "Time", "Action", "Owner", "Parties", "Context", "Dashboard", "Priority"].forEach(function(field) {
      var fm = chunk.match(new RegExp(field + "\\s*:\\s*(.+)"));
      if (fm) action[field.toLowerCase().replace("/", "_")] = fm[1].trim();
    });

    action.dashboard = (chunk.match(/Dashboard\s*:\s*(.+)/) || ["",""])[1].trim();

    if (action.action) actions.push(action);
  }

  return actions;
}


// ── Follow-ups routing ────────────────────────────────────────────────────────

function appendToFollowups(actions, callTitle) {
  var doc  = DocumentApp.openById(FOLLOWUPS_DOC_ID);
  var body = doc.getBody();
  var text = body.getText();

  var nums   = text.match(/^\|\s*(\d+)\s*\|/gm) || [];
  var maxNum = 0;
  nums.forEach(function(n) {
    var m = n.match(/\d+/);
    if (m) maxNum = Math.max(maxNum, parseInt(m[0]));
  });

  var rows = actions.map(function(a) {
    maxNum++;
    var who      = (a.parties || "Yoni").split(",")[0].trim();
    var what     = a.action || "";
    var due      = a["date/deadline"] || a.date_deadline || "TBD";
    var priority = a.priority || "Medium";
    return "| " + maxNum + " | " + who + " | " + what + " [" + priority + "] | " + due + " | Tomac Cove | beside.com | " + callTitle + " |";
  });

  body.appendParagraph(rows.join("\n"));
  doc.saveAndClose();
  Logger.log("  → " + actions.length + " action(s) → Follow-ups");
}


// ── Deal Pipeline routing ─────────────────────────────────────────────────────

function appendToDealPipeline(actions, callTitle) {
  var doc  = DocumentApp.openById(DEAL_DOC_ID);
  var body = doc.getBody();

  body.appendParagraph("\nBeside call: " + callTitle).setHeading(DocumentApp.ParagraphHeading.HEADING3);

  actions.forEach(function(a) {
    var line = (a.action || "") +
               "  |  Owner: " + (a.owner || "Yoni") +
               "  |  Due: "   + (a["date/deadline"] || a.date_deadline || "TBD") +
               "  |  Priority: " + (a.priority || "Medium") +
               "\n  Context: " + (a.context || "");
    body.appendParagraph(line);
  });

  doc.saveAndClose();
  Logger.log("  → " + actions.length + " action(s) → Deal Pipeline");
}


// ── State ─────────────────────────────────────────────────────────────────────

function getProcessed() {
  var raw = PropertiesService.getScriptProperties().getProperty(PROCESSED_KEY);
  return raw ? JSON.parse(raw) : {};
}

function saveProcessed(data) {
  PropertiesService.getScriptProperties().setProperty(PROCESSED_KEY, JSON.stringify(data));
}


// ── Manual reset ──────────────────────────────────────────────────────────────

function resetProcessed() {
  PropertiesService.getScriptProperties().deleteProperty(PROCESSED_KEY);
  Logger.log("Processed state cleared — next run will re-route all entries.");
}
