/**
 * otter-drive-watcher.gs
 *
 * Google Apps Script — Otter Drive Watcher
 *
 * Polls the three Otter Drive folders every 5 minutes.
 * When it finds a file newer than the last check, it POSTs to the
 * dashboard server's /otter-webhook endpoint so cos_otter_backfill.py
 * runs within minutes of a transcript landing in Drive.
 *
 * SETUP (one-time):
 *   1. Project Settings → Script Properties → Add:
 *        SERVER_URL      <dashboard-server-webhook-url>
 *        WEBHOOK_SECRET  <shared-secret>
 *   2. Run installTrigger() once to create the 5-min repeating trigger.
 *   3. Authorize Drive and UrlFetch scopes when prompted.
 *
 * Corner cases handled:
 *   - First run: stamps lastChecked = now, does not fire (avoids re-processing backlog)
 *   - Duplicate fires: server rate-limits to 1 dispatch per 120s; dedup tracker handles the rest
 *   - Network error: logged to Execution Log, trigger continues on next interval
 *   - Non-transcript files: filtered by MIME type (Google Docs + text/plain only)
 *   - Empty folders / no new files: no POST sent
 *   - Concurrent GAS executions: LockService prevents overlap
 *   - ngrok / server down: muteHttpExceptions=true; non-2xx is logged but not thrown
 */

var FOLDER_IDS = [
  "1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S",  // Otter / Tomac Cove
  "1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF",  // Otter / Recruiting
  "1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq",  // Otter / Other
];

var PROPS_LAST_CHECKED = "lastChecked";
var PROPS_LAST_SENT    = "lastSent";

var TRANSCRIPT_MIMES = [
  "application/vnd.google-apps.document",
  "text/plain",
];

function checkNewOtterTranscripts() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    Logger.log("checkNewOtterTranscripts: lock held by another execution, skipping");
    return;
  }
  try {
    _checkNewOtterTranscripts();
  } finally {
    lock.releaseLock();
  }
}

function _checkNewOtterTranscripts() {
  var props = PropertiesService.getScriptProperties();
  var serverUrl     = props.getProperty("SERVER_URL");
  var webhookSecret = props.getProperty("WEBHOOK_SECRET");

  if (!serverUrl || !webhookSecret) {
    Logger.log("ERROR: SERVER_URL or WEBHOOK_SECRET not set in Script Properties.");
    return;
  }

  var now            = new Date();
  var lastCheckedStr = props.getProperty(PROPS_LAST_CHECKED);

  if (!lastCheckedStr) {
    props.setProperty(PROPS_LAST_CHECKED, now.toISOString());
    Logger.log("First run: initialized lastChecked to " + now.toISOString() + ". No POST sent.");
    return;
  }

  var lastChecked = new Date(lastCheckedStr);
  var newFiles    = [];

  for (var i = 0; i < FOLDER_IDS.length; i++) {
    var folderId = FOLDER_IDS[i];
    try {
      var folder = DriveApp.getFolderById(folderId);
      var files  = folder.getFiles();
      while (files.hasNext()) {
        var file         = files.next();
        var modifiedTime = file.getLastUpdated();
        var mime         = file.getMimeType();

        if (modifiedTime <= lastChecked) continue;
        if (TRANSCRIPT_MIMES.indexOf(mime) === -1) continue;

        newFiles.push({
          id:       file.getId(),
          name:     file.getName(),
          modified: modifiedTime.toISOString(),
          mime:     mime,
        });
      }
    } catch (e) {
      Logger.log("Error scanning folder " + folderId + ": " + e.message);
    }
  }

  props.setProperty(PROPS_LAST_CHECKED, now.toISOString());

  if (newFiles.length === 0) {
    Logger.log("No new transcript files since " + lastCheckedStr);
    return;
  }

  Logger.log("Found " + newFiles.length + " new file(s): " +
             newFiles.map(function(f) { return f.name; }).join(", "));

  var payload = JSON.stringify({ files: newFiles, detectedAt: now.toISOString() });
  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            payload,
    headers:            { "X-Otter-Secret": webhookSecret },
    muteHttpExceptions: true,
    followRedirects:    false,
  };

  var resp;
  try {
    resp = UrlFetchApp.fetch(serverUrl, options);
  } catch (e) {
    Logger.log("Network error posting to " + serverUrl + ": " + e.message);
    return;
  }

  var code = resp.getResponseCode();
  if (code >= 200 && code < 300) {
    props.setProperty(PROPS_LAST_SENT, now.toISOString());
    Logger.log("Webhook accepted (" + code + "): " + resp.getContentText());
  } else {
    Logger.log("Webhook returned " + code + ": " + resp.getContentText());
  }
}

function installTrigger() {
  removeTrigger();
  ScriptApp.newTrigger("checkNewOtterTranscripts")
    .timeBased()
    .everyMinutes(1)
    .create();
  Logger.log("1-minute trigger installed.");
}

function removeTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "checkNewOtterTranscripts") {
      ScriptApp.deleteTrigger(triggers[i]);
      Logger.log("Removed trigger: " + triggers[i].getUniqueId());
    }
  }
}

function testWebhook() {
  var props         = PropertiesService.getScriptProperties();
  var serverUrl     = props.getProperty("SERVER_URL");
  var webhookSecret = props.getProperty("WEBHOOK_SECRET");

  if (!serverUrl || !webhookSecret) {
    Logger.log("ERROR: SERVER_URL or WEBHOOK_SECRET not configured.");
    return;
  }

  var payload = JSON.stringify({ files: [{ id: "test", name: "TEST", modified: new Date().toISOString() }], test: true });
  var options = {
    method:             "post",
    contentType:        "application/json",
    payload:            payload,
    headers:            { "X-Otter-Secret": webhookSecret },
    muteHttpExceptions: true,
  };
  var resp = UrlFetchApp.fetch(serverUrl, options);
  Logger.log("testWebhook response: " + resp.getResponseCode() + " — " + resp.getContentText());
}
