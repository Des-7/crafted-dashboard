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

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Trimmed string value of a cell by column index (safe on short/undefined rows).
function cell(row, i) {
  return (i >= 0 && i < row.length) ? String(row[i]).trim() : '';
}

// Password-gated Team dataset. The password is compared SERVER-SIDE against the
// TEAM_PASSWORD Script Property; it is never shipped to the browser. On any
// mismatch we return {ok:false,error:'auth_failed'} and NO video data, so a
// wrong password leaks nothing (verifiable in the network tab). On success we
// return the full per-video dataset the Team page needs, read from the sheet.
function handleTeamData(data) {
  var expected = PropertiesService.getScriptProperties().getProperty('TEAM_PASSWORD');
  if (!expected || String(data.password) !== expected) {
    return jsonOut({ok: false, error: 'auth_failed'});
  }
  var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return jsonOut({ok: true, videos: []});
  var grid = sheet.getRange(1, 1, lastRow, sheet.getLastColumn()).getValues();
  var headers = grid[0].map(function (h) {
    return String(h).trim().toLowerCase().replace(/\s+/g, '_');
  });
  var col = {
    course_code: headers.indexOf('course_code'),
    video_code: headers.indexOf('video_code'),
    title: headers.indexOf('title'),
    video_type: headers.indexOf('video_type'),
    status: headers.indexOf('status'),
    submitter: headers.indexOf('submitter'),
    filestage_link: headers.indexOf('filestage_link'),
    deliverable_link: headers.indexOf('deliverable_link'),
    // No true review-round count lives in the sheet (that is a DB-only
    // state_transitions count); filestage_version is the closest per-video
    // proxy, used as "Round N" when present.
    review_round: headers.indexOf('filestage_version')
  };
  var videos = [];
  for (var r = 1; r < grid.length; r++) {
    var row = grid[r];
    var code = cell(row, col.course_code);
    if (!code) continue; // skip blank / phantom rows
    videos.push({
      course_code: code,
      video_code: cell(row, col.video_code),
      title: cell(row, col.title),
      video_type: cell(row, col.video_type),
      status: cell(row, col.status).toLowerCase(),
      submitter: cell(row, col.submitter),
      filestage_link: cell(row, col.filestage_link),
      deliverable_link: cell(row, col.deliverable_link),
      review_round: cell(row, col.review_round)
    });
  }
  return jsonOut({ok: true, videos: videos});
}

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);

    // Team dashboard data request (password-gated). Kept in the SAME project as
    // intake so there is a single deployment / URL.
    if (data && data.action === 'team_data') {
      return handleTeamData(data);
    }

    // NOTE: domain-locking is intentionally DISABLED for now.
    // A previous version checked Session.getActiveUser().getEmail() ended in
    // '@htu.edu.jo'. Under the current design the form POSTs from a static
    // GitHub Pages page with a credential-less fetch, so the caller is always
    // anonymous and getEmail() returns "" -- the check rejected EVERY real
    // submission. Real Workspace-domain locking is DEFERRED pending an HTU
    // IT/admin fix (the "Anyone within htu.edu.jo" deployment option needs the
    // script owned by an @htu.edu.jo Workspace account). Re-enable here and at
    // the deployment level together once that is resolved.
    var sheet = SpreadsheetApp.openById(SHEET_ID).getSheetByName(SHEET_NAME);

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
