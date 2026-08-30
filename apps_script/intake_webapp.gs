var SHEET_ID = '157jWe_Wz0WzL3AsiMgunELwpxbGN0wpGQddmwBjnymE';
var SHEET_NAME = 'Videos intake';
// Registry tab: the authoritative list of courses (incl. brand-new ones with
// zero videos yet). Header row: course_code | faculty_slug | display_name.
var REGISTRY_SHEET = 'Courses';

// Read the Courses registry tab -> {codes:[course_code...], faculty:{code:slug}}.
// Missing tab / empty tab returns empties (callers fall back gracefully). Order
// and original casing of course_code are preserved.
function readRegistry(ss) {
  var out = {codes: [], faculty: {}};
  var reg = ss.getSheetByName(REGISTRY_SHEET);
  if (!reg || reg.getLastRow() < 2) return out;
  var head = reg.getRange(1, 1, 1, reg.getLastColumn()).getValues()[0].map(function (h) {
    return String(h).trim().toLowerCase().replace(/\s+/g, '_');
  });
  var ccIdx = head.indexOf('course_code');
  var fsIdx = head.indexOf('faculty_slug');
  if (ccIdx < 0) return out;
  var rows = reg.getRange(2, 1, reg.getLastRow() - 1, reg.getLastColumn()).getValues();
  for (var i = 0; i < rows.length; i++) {
    var code = String(rows[i][ccIdx]).trim();
    if (!code) continue;
    out.codes.push(code);
    if (fsIdx >= 0) {
      var slug = String(rows[i][fsIdx]).trim();
      if (slug) out.faculty[code] = slug;
    }
  }
  return out;
}

// Read-only endpoint returning {ok:true, courses:[...], faculties:{code:slug}}.
// intake.html fetches this (GET) on page load to populate the course dropdown.
// The course list is the UNION of (a) the Courses registry tab -- so a brand-new
// course with ZERO data rows still appears -- and (b) distinct course_code
// values from existing "Videos intake" rows, kept as a safety net so anything
// already in use never disappears if the registry lags. faculties feeds
// team.html's course->faculty grouping from the same registry.
function doGet(e) {
  try {
    var ss = SpreadsheetApp.openById(SHEET_ID);
    var sheet = ss.getSheetByName(SHEET_NAME);
    var seen = {};
    var courses = [];

    // (b) existing intake rows (safety net)
    var headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    var courseCol = -1;
    for (var i = 0; i < headers.length; i++) {
      if (String(headers[i]).trim().toLowerCase().replace(/\s+/g, '_') === 'course_code') { courseCol = i; break; }
    }
    var lastRow = sheet.getLastRow();
    if (courseCol >= 0 && lastRow > 1) {
      var vals = sheet.getRange(2, courseCol + 1, lastRow - 1, 1).getValues();
      for (var r = 0; r < vals.length; r++) {
        var c = String(vals[r][0]).trim();
        if (c && !seen[c]) { seen[c] = true; courses.push(c); }
      }
    }

    // (a) registry tab (authoritative; includes zero-row courses)
    var reg = readRegistry(ss);
    for (var k = 0; k < reg.codes.length; k++) {
      if (!seen[reg.codes[k]]) { seen[reg.codes[k]] = true; courses.push(reg.codes[k]); }
    }

    courses.sort();
    return ContentService.createTextOutput(JSON.stringify({ok: true, courses: courses, faculties: reg.faculty}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: String(err), courses: [], faculties: {}}))
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
  var ss = SpreadsheetApp.openById(SHEET_ID);
  var reg = readRegistry(ss);
  var sheet = ss.getSheetByName(SHEET_NAME);
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return jsonOut({ok: true, videos: [], faculties: reg.faculty, courses: reg.codes});
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
  return jsonOut({ok: true, videos: videos, faculties: reg.faculty, courses: reg.codes});
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
    var ss = SpreadsheetApp.openById(SHEET_ID);
    var sheet = ss.getSheetByName(SHEET_NAME);

    var required = ['course_code', 'video_code', 'title', 'video_type', 'drive_folder_link', 'submitter'];
    for (var i = 0; i < required.length; i++) {
      if (!data[required[i]] || String(data[required[i]]).trim() === '') {
        return jsonOut({ok: false, error: 'This field is required: ' + required[i]});
      }
    }

    // Reject a course that is not in the Courses registry (case-insensitive).
    // The dropdown is built from this same registry, so this only fires on a
    // stale/hand-crafted submission — but it gives the form a real reason to show
    // instead of silently writing an orphan row.
    var reg = readRegistry(ss);
    var courseIn = String(data.course_code).trim();
    var courseKnown = reg.codes.some(function (c) {
      return c.toLowerCase() === courseIn.toLowerCase();
    });
    if (!courseKnown) {
      return jsonOut({ok: false, code: 'unknown_course', error: "This course isn't registered yet: " + courseIn});
    }

    // Reject a video_code that already exists in the sheet (case-insensitive,
    // trimmed). Previously a duplicate was silently written as a second row.
    //
    // A RETIRED row does not count. `ops_cli video retire` stamps `retired_at`
    // on the intake row of a video that was removed and is being remade: the row
    // stays as history (one of ours carries six Filestage versions) but must not
    // block the code from being registered again. Without this, removing a video
    // made it permanently unregisterable and the failure was silent — W8 stamped
    // 'Registered ✓' on a registration this endpoint had refused (2026-08-30;
    // four such rows existed before anyone noticed).
    //
    // The column is OPTIONAL: absent `retired_at`, every row counts as live and
    // behaviour is exactly as before, so this is safe to deploy before the
    // column is added.
    var dupHead = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(function (h) {
      return String(h).trim().toLowerCase().replace(/\s+/g, '_');
    });
    var vcCol = dupHead.indexOf('video_code');
    var retCol = dupHead.indexOf('retired_at');
    if (vcCol >= 0 && sheet.getLastRow() > 1) {
      var nRows = sheet.getLastRow() - 1;
      var vcIn = String(data.video_code).trim().toLowerCase();
      var vcVals = sheet.getRange(2, vcCol + 1, nRows, 1).getValues();
      var retVals = retCol >= 0
        ? sheet.getRange(2, retCol + 1, nRows, 1).getValues()
        : null;
      for (var v = 0; v < vcVals.length; v++) {
        if (retVals && String(retVals[v][0]).trim() !== '') {
          continue;  // retired: kept as history, never a blocker
        }
        if (String(vcVals[v][0]).trim().toLowerCase() === vcIn) {
          return jsonOut({ok: false, code: 'duplicate_video_code', error: 'This video code already exists: ' + String(data.video_code).trim()});
        }
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
      // Only sent (and required) by the form for instructor-led types; blank
      // otherwise. Lands in the "Instructor Name" column IF that header exists
      // in the sheet -- add it, or this value is silently dropped like any
      // unmatched key. It never overwrites another column.
      instructor_name: String(data.instructor_name || '').trim(),
      drive_folder_link: String(data.drive_folder_link).trim(),
      // Optional per-video setting ('edit' | 'skip'), sent by W8 from the filming
      // sheet's "Design Review" column and NEVER by the form. Deliberately absent
      // from `required` above: a submission without it is normal and must stay
      // legal. Same rule as instructor_name -- it lands in the "design_review"
      // column IF that header exists in the sheet, and is otherwise dropped like
      // any unmatched key, never overwriting another column.
      //
      // A sender that omits the key entirely and one that sends '' are treated
      // the same HERE (both write a blank cell), but the distinction still
      // matters upstream: W8 omits the key rather than sending '' so that a blank
      // filming cell leaves the intake cell untouched. Downstream, a blank cell
      // means "unset" and the video falls back to the worker's own default --
      // so never substitute a default value here.
      design_review: String(data.design_review || '').trim()
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
