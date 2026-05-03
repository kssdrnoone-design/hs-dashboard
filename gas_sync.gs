/**
 * 高校受験ダッシュボード - 予約データ家族共有用 GAS Webアプリ
 *
 * 【セットアップ手順】
 *  1. https://script.new を開く
 *  2. 表示されるコードを全消去 → このファイルの中身を全コピペ
 *  3. 左上のプロジェクト名を「hs-dashboard-sync」などに変更
 *  4. 右上「デプロイ」→「新しいデプロイ」
 *  5. 種類: ウェブアプリ
 *  6. 説明: 任意（例: hs-dashboard 予約共有 v1）
 *  7. 次のユーザーとして実行: 自分（あなたのGoogleアカウント）
 *  8. アクセスできるユーザー: 全員
 *  9. 「デプロイ」→「アクセスを許可」→ Googleアカウントで承認
 * 10. 表示される「ウェブアプリのURL」をコピー
 * 11. アプリの「✅ 予約済」タブ → 「⚙️ 共有設定」 → URLを貼り付け
 *
 * 【家族への配布】
 * 「⚙️ 共有設定」内の「📦 家族配布用リンクをコピー」ボタンで、
 * URL付きリンクが生成されます。家族にそのリンクを送るだけで自動設定完了。
 *
 * 【データの確認・編集】
 * 自動的に「hs-dashboard-reservations」というスプレッドシートが
 * Googleドライブに作成されます。直接編集することも可能。
 */

const SHEET_NAME = 'reservations';
const COLUMNS = [
  'school_id', 'title', 'date', 'time',
  'place', 'reservation_no', 'with', 'memo', 'status', 'updated_at'
];

function _getSpreadsheet() {
  const props = PropertiesService.getScriptProperties();
  let id = props.getProperty('SPREADSHEET_ID');
  if (id) {
    try { return SpreadsheetApp.openById(id); }
    catch (e) { /* 削除されてた場合は作り直す */ }
  }
  const ss = SpreadsheetApp.create('hs-dashboard-reservations');
  props.setProperty('SPREADSHEET_ID', ss.getId());
  return ss;
}

function _getSheet() {
  const ss = _getSpreadsheet();
  let sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.getRange(1, 1, 1, COLUMNS.length).setValues([COLUMNS]);
    sh.setFrozenRows(1);
  }
  return sh;
}

function _readItems() {
  const sh = _getSheet();
  const last = sh.getLastRow();
  if (last < 2) return [];
  const cols = sh.getRange(1, 1, 1, COLUMNS.length).getValues()[0];
  const data = sh.getRange(2, 1, last - 1, COLUMNS.length).getValues();
  return data.map(row => {
    const obj = {};
    cols.forEach((c, i) => {
      let v = row[i];
      if (v instanceof Date) {
        if (c === 'date') v = Utilities.formatDate(v, 'JST', 'yyyy-MM-dd');
        else if (c === 'time') v = Utilities.formatDate(v, 'JST', 'HH:mm');
        else v = v.toISOString();
      }
      obj[c] = (v === null || v === undefined) ? '' : String(v);
    });
    return obj;
  }).filter(it => it.school_id && it.date);
}

function _writeItems(items) {
  const sh = _getSheet();
  const last = sh.getLastRow();
  if (last >= 2) sh.getRange(2, 1, last - 1, COLUMNS.length).clearContent();
  if (!items || items.length === 0) return;
  const now = new Date().toISOString();
  const rows = items.map(it => COLUMNS.map(c => {
    if (c === 'updated_at') return it.updated_at || now;
    return (it[c] === null || it[c] === undefined) ? '' : it[c];
  }));
  sh.getRange(2, 1, rows.length, COLUMNS.length).setValues(rows);
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  try {
    const items = _readItems();
    return _json({ ok: true, items: items, updatedAt: new Date().toISOString(), count: items.length });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function doPost(e) {
  try {
    const body = e.postData ? e.postData.contents : '';
    const payload = JSON.parse(body || '{}');
    const action = payload.action || 'save';
    if (action === 'save' && Array.isArray(payload.items)) {
      _writeItems(payload.items);
      return _json({ ok: true, count: payload.items.length, updatedAt: new Date().toISOString() });
    }
    if (action === 'load') {
      return _json({ ok: true, items: _readItems(), updatedAt: new Date().toISOString() });
    }
    return _json({ ok: false, error: 'unknown action: ' + action });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

/* テスト用：エディタから直接実行して動作確認 */
function _selfTest() {
  _writeItems([
    { school_id: 'tamakagakugijutsu', title: 'テスト説明会', date: '2026-07-15', time: '14:00',
      place: '本校', reservation_no: 'T-1', with: '父', memo: 'テストデータ', status: '予約中' }
  ]);
  Logger.log(JSON.stringify(_readItems(), null, 2));
}
