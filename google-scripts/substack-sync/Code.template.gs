// STATUS: TEMPLATE — populate per tenant; do NOT commit live values.
/**
 * substack-sync.template.gs (project name: "Substack sync")
 *
 * Pulls Substack newsletters and selected research sources from Gmail,
 * groups by author, writes one Google Doc per author into a shared
 * Drive folder. Triggered daily via setupTwoTriggers() — runs at 6am and
 * 7am EST.
 *
 * SCOPE: dashboard pipeline input — Substack docs feed downstream
 * dashboard summarization.
 *
 * SECURITY:
 *   - No RBN credentials in this template. The legacy backfillRBN() function
 *     in the live editor source contained an unrotated RBN password; that
 *     entire dead-code section is OMITTED from this template.
 *   - If a tenant needs RBN scraping in the future, store credentials in
 *     Script Properties (NEVER hardcode), and place the placeholder
 *     __RBN_PASSWORD_FROM_KEYCHAIN__ wherever the credential would be
 *     fetched, with a code path that loads from Script Properties at
 *     runtime. The placeholder must NOT be replaced with a literal value
 *     in any committed file.
 *   - SHARING: verify the Apps Script project's collaborator list per
 *     tenant. The legacy primary-tenant project showed a "shared" icon —
 *     anyone in that share list had read access to the password before
 *     rotation. Re-share only with explicit per-tenant approval.
 *
 * Functions present in the live primary-tenant editor but NOT in this
 * template (capture per-tenant if needed):
 *   - syncCapstone()
 *   - syncTranscriptSummaries()
 *   - parseRBNArticle_(), buildItems_(), writeItem_(), insertArticleAt_(),
 *     appendArticle_(), rebuildTOC_(), fetchRBN_()
 *   - fixMissing_0325(), cleanupDuplicates_0325(), diagDoc(), addRBNContent()
 *   - doPost(), setupWebhookSecret()
 *   - seedTranscriptEntries(), runTranscriptSetup()
 *   - setupDailyTrigger(), setupTCIPTriggers()
 *   - backfillRBN(), rbnLogin(), cleanRBNBody()  ← CONTAINED LEAKED CREDENTIAL; DELETE FROM EDITOR
 *
 * Placeholders to substitute at install time:
 *   __SUBSTACK_FEEDS_FOLDER_ID__   — Drive folder where per-author docs are written
 *   __SUBSTACK_SENDERS_QUERY__     — Gmail "from:" OR-clause for tenant sender list
 *   __SUBSTACK_DAYS_BACK__         — integer lookback window (default 14)
 *   __RBN_PASSWORD_FROM_KEYCHAIN__ — sentinel; only used if RBN scraping enabled per tenant
 */

var FOLDER_ID = '__SUBSTACK_FEEDS_FOLDER_ID__';
var DAYS_BACK = __SUBSTACK_DAYS_BACK__;

function syncSubstackToGoogleDocs() {
  var folder = DriveApp.getFolderById(FOLDER_ID);
  var cutoffDate = new Date();
  cutoffDate.setDate(cutoffDate.getDate() - DAYS_BACK);
  var dateStr = Utilities.formatDate(cutoffDate, Session.getScriptTimeZone(), 'yyyy/MM/dd');

  // __SUBSTACK_SENDERS_QUERY__ should expand to a parenthesized OR-clause, e.g.
  //   (from:substack.com OR from:info@example.com)
  var query = '__SUBSTACK_SENDERS_QUERY__ after:' + dateStr + ' -from:no-reply@substack.com';
  var threads = GmailApp.search(query, 0, 100);

  var authorEmails = {};

  for (var t = 0; t < threads.length; t++) {
    var messages = threads[t].getMessages();
    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      var fromRaw = msg.getFrom();
      var emailMatch = fromRaw.match(/<(.+?)>/);
      var senderEmail = emailMatch ? emailMatch[1] : fromRaw;
      if (senderEmail === 'no-reply@substack.com') continue;

      var nameMatch = fromRaw.match(/^(.+?)\s*</);
      var senderName = nameMatch ? nameMatch[1].replace(/"/g, '').trim() : senderEmail;

      var baseEmail = senderEmail.replace(/\+[^@]*/, '');

      if (!authorEmails[baseEmail]) {
        authorEmails[baseEmail] = { name: senderName, articles: [] };
      }

      var subject = msg.getSubject();
      var date = msg.getDate();
      var body = msg.getPlainBody();

      body = cleanSubstackBody(body);

      var isDuplicate = false;
      for (var a = 0; a < authorEmails[baseEmail].articles.length; a++) {
        if (authorEmails[baseEmail].articles[a].subject === subject) {
          isDuplicate = true;
          break;
        }
      }

      if (!isDuplicate) {
        authorEmails[baseEmail].articles.push({
          subject: subject,
          date: date,
          body: body
        });
      }
    }
  }

  var existingDocs = {};
  var files = folder.getFiles();
  while (files.hasNext()) {
    var file = files.next();
    existingDocs[file.getName()] = file;
  }

  for (var email in authorEmails) {
    var author = authorEmails[email];
    var docName = author.name;

    var doc;
    if (existingDocs[docName]) {
      doc = DocumentApp.openById(existingDocs[docName].getId());
    } else {
      doc = DocumentApp.create(docName);
      var docFile = DriveApp.getFileById(doc.getId());
      folder.addFile(docFile);
      DriveApp.getRootFolder().removeFile(docFile);
    }

    var docBody = doc.getBody();
    var existingText = docBody.getText();

    author.articles.sort(function(a, b) { return b.date - a.date; });

    for (var i = 0; i < author.articles.length; i++) {
      var article = author.articles[i];
      if (existingText.indexOf(article.subject) !== -1) continue;

      if (docBody.getText().length > 1) {
        docBody.appendHorizontalRule();
        docBody.appendParagraph('');
      }

      docBody.appendParagraph(article.subject)
        .setHeading(DocumentApp.ParagraphHeading.HEADING1);
      docBody.appendParagraph('Date: ' + Utilities.formatDate(article.date, Session.getScriptTimeZone(), 'MMMM dd, yyyy'))
        .setItalic(true);
      docBody.appendParagraph('');
      docBody.appendParagraph(article.body);
    }

    doc.saveAndClose();
    Logger.log('Processed: ' + docName + ' (' + author.articles.length + ' articles)');
  }

  Logger.log('Done! Processed ' + Object.keys(authorEmails).length + ' authors.');
}

function cleanSubstackBody(body) {
  body = body.replace(/­/g, '');
  body = body.replace(/​/g, '');
  body = body.replace(/[ ]/g, ' ');
  body = body.replace(/Forwarded this email\? Subscribe here for more[\s\S]*?READ IN APP\s*/g, '');
  body = body.replace(/You're currently a free subscriber[\s\S]*$/g, '');
  body = body.replace(/Upgrade to paid[\s\S]*?subscription\.\s*/g, '');
  body = body.replace(/Like\s*Comment\s*Restack[\s\S]*$/g, '');
  body = body.replace(/\n{4,}/g, '\n\n\n');
  return body.trim();
}

function syncAll() {
  syncSubstackToGoogleDocs();
  // Per-tenant: enable additional sync functions only if their source has been
  // captured into this project AND any credentials they require are stored in
  // Script Properties (never hardcoded). Examples retained for documentation:
  //   syncCapstone();
  //   syncTranscriptSummaries();
}

function setupTwoTriggers() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    var fn = t.getHandlerFunction();
    if (fn === 'syncAll' || fn === 'syncSubstackToGoogleDocs') {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger('syncAll')
    .timeBased()
    .atHour(6)
    .everyDays(1)
    .inTimezone('America/New_York')
    .create();

  ScriptApp.newTrigger('syncAll')
    .timeBased()
    .atHour(7)
    .everyDays(1)
    .inTimezone('America/New_York')
    .create();

  Logger.log('Done: created triggers at 6am and 7am EST for syncAll');
}

// ─────────────────────────────────────────────────────────────────────────────
// RBN scraping (DISABLED in template)
//
// The legacy primary-tenant editor contains backfillRBN() with a hardcoded
// password. That function and its helpers (rbnLogin, cleanRBNBody for RBN,
// fetchRBN_, parseRBNArticle_, etc.) are intentionally NOT included in this
// template. If a future tenant needs RBN scraping:
//   1. Rotate the RBN password.
//   2. Store the new password in Script Properties (key: RBN_PASSWORD).
//   3. Reimplement the function reading from PropertiesService — the literal
//      sentinel __RBN_PASSWORD_FROM_KEYCHAIN__ should remain in any committed
//      template; never replace it with a real credential in source control.
// ─────────────────────────────────────────────────────────────────────────────
