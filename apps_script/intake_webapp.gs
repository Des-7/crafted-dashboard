function doPost(e) {
  try {
    var sheet = SpreadsheetApp.openById('157jWe_Wz0WzL3AsiMgunELwpxbGN0wpGQddmwBjnymE').getSheetByName('Videos intake');
    var data = JSON.parse(e.postData.contents);

    var required = ['course_code', 'video_code', 'title', 'video_type'];
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
      video_type: String(data.video_type).trim().toLowerCase()
    };
    headers.forEach(function(h, idx) {
      var key = String(h).trim().toLowerCase().replace(/\s+/g, '_');
      if (values.hasOwnProperty(key)) row[idx] = values[key];
    });

    sheet.appendRow(row);
    return ContentService.createTextOutput(JSON.stringify({ok: true}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: String(err)}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
