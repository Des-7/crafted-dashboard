var SHEET_ID = '157jWe_Wz0WzL3AsiMgunELwpxbGN0wpGQddmwBjnymE';
var SHEET_NAME = 'Videos intake';

// Read-only endpoint: returns the CURRENT distinct course_code values from the
// live sheet as {ok:true, courses:[...]}. intake.html fetches this (GET) on
// page load to populate the course dropdown, so a brand-new course_code in the
// sheet appears automatically with no code change.
function doGet(e) {
  try {
    var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
    var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var courseCol = -1;
    for (var i = 0; i < headers.length; i++) {
      if (String(headers[i]).trim().toLowerCase().replace(/\s+/g, '_') === 'course_code') { courseCol = i; break; }
    }
    var courses = [];
    var lastRow = sheet.getLastRow();
    if (courseCol >= 0 && lastRow > 1) {
      var vals = sheet.getRange(2, courseCol + 1, lastRow - 1, 1).getValues();
      var seen = {};
      for (var r = 0; r < vals.length; r++) {
        var c = String(vals[r][0]).trim();
        if (c && !seen[c]) { seen[c] = true; courses.push(c); }
      }
      courses.sort();
    }
    return ContentService.createTextOutput(JSON.stringify({ok: true, courses: courses}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: String(err), courses: []}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function doPost(e) {
  try {
    // --- Defense in depth: only allow the htu.edu.jo Workspace domain. ---
    // The PRIMARY gate is the deployment setting "Who has access =
    // Anyone within htu.edu.jo". This code check is a backstop so the form
    // still refuses strangers if that setting is ever reverted to "Anyone".
    // With "Execute as: Me", getActiveUser().getEmail() returns the caller's
    // address for authenticated same-domain users, and "" for anonymous
    // access -- so an accidental "Anyone" deployment fails this check closed.
    var email = Session.getActiveUser().getEmail();
    if (!email || !email.toLowerCase().endsWith('@htu.edu.jo')) {
      return ContentService.createTextOutput(JSON.stringify({ok: false, error: 'unauthorized'}))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
    var data = JSON.parse(e.postData.contents);

    var required = ['course_code', 'video_code', 'title', 'video_type', 'drive_folder_link', 'submitter'];
    for (var i = 0; i < required.length; i++) {
      if (!data[required[i]] || String(data[required[i]]).trim() === '') {
        return ContentService.createTextOutput(JSON.stringify({ok: false, error: 'الحقل ده فاضي: ' + required[i]}))
          .setMimeType(ContentService.MimeType.JSON);
      }
    }

    var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var row = new Array(headers.length).fill('');
    var values = {
      timestamp: new Date().toLocaleString(),
      submitter: data.submitter || '',
      course_code: String(data.course_code).trim(),
      video_code: String(data.video_code).trim(),
      title: String(data.title).trim(),
      video_type: String(data.video_type).trim().toLowerCase(),
      drive_folder_link: String(data.drive_folder_link).trim()
    };
    var courseIdx = -1;
    headers.forEach(function(h, idx) {
      var key = String(h).trim().toLowerCase().replace(/\s+/g, '_');
      if (values.hasOwnProperty(key)) row[idx] = values[key];
      if (key === 'course_code') courseIdx = idx;
    });

    // Write deterministically to the row right after the last real data row.
    // We do NOT use sheet.appendRow(): if the grid has any stray/phantom
    // content far down (common on a sheet an automation writes to), appendRow
    // lands on that phantom last row (e.g. row 1000) instead of the true next
    // row. Anchor on the course_code column, which every real row always has.
    var targetRow = 2; // first data row, below the header
    if (courseIdx >= 0) {
      var colVals = sheet.getRange(1, courseIdx + 1, sheet.getMaxRows(), 1).getValues();
      for (var r = colVals.length - 1; r >= 1; r--) { // r=0 is the header
        if (String(colVals[r][0]).trim() !== '') { targetRow = r + 2; break; }
      }
    } else {
      targetRow = sheet.getLastRow() + 1; // fallback if header ever renamed
    }
    sheet.getRange(targetRow, 1, 1, row.length).setValues([row]);
    return ContentService.createTextOutput(JSON.stringify({ok: true}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: String(err)}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
