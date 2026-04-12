"""
高校受験情報ダッシュボード
- 都立＋私立高校の公式HPを定期スクレイピング
- 説明会・入試日程を正規表現で抽出
- 5タブHTML生成＋新着Gmail通知
"""

import json
import os
import sys
import time
import hashlib
import re
import random
from datetime import datetime, timedelta, date
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# === パス設定 ===
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "03_config.json"
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DATA_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

TODAY = datetime.now().strftime("%Y%m%d")
TODAY_HUMAN = datetime.now().strftime("%Y/%m/%d")
THIS_YEAR = datetime.now().year
LATEST_JSON = DATA_DIR / "latest.json"

# === 説明会系イベントキーワード（開催系のみ。結果発表・出願系は除外） ===
EVENT_KEYWORDS = [
    "学校説明会", "説明会", "見学会", "学校見学",
    "オープンスクール", "オープンキャンパス",
    "体験授業", "体験入学", "授業体験",
    "公開授業", "授業公開",
    "個別相談会", "入試相談会",
    "文化祭", "学園祭", "合同説明会",
]
# 除外キーワード（これが含まれる文脈は採用しない）
EXCLUDE_KEYWORDS = [
    "得点分布", "合格発表", "選抜結果",
    "終了しました", "実施しました", "開催しました",
    "中止となりました",
]
# サブページ追跡用キーワード（トップから辿るリンクテキストに含まれるもの）
SUBPAGE_LINK_KEYWORDS = [
    "説明会", "学校見学", "学校説明", "見学会",
    "オープンスクール", "オープンキャンパス", "オープンハイ",
    "入試", "受験", "中学生", "生徒募集", "来校",
    "イベント", "学校案内", "案内",
    "授業公開", "公開授業",
]

# === 日付パターン ===
# YYYY年MM月DD日
DATE_RE_FULL = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# MM月DD日（年省略）
DATE_RE_SHORT = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# YYYY/MM/DD, YYYY-MM-DD, YYYY.MM.DD
DATE_RE_NUMERIC = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def make_event_id(school_id, event_date, title):
    """イベントの一意ID（差分検知用）"""
    raw = f"{school_id}|{event_date}|{title[:60]}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def escape_html(text):
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


YEAR_HINT_RE = re.compile(r'(?:令和\s*(\d{1,2})\s*年|(\d{4})\s*年|(\d{4})/)')


def infer_year_from_context(context):
    """contextから年ヒントを抽出（令和 > 西暦 > None）"""
    m = YEAR_HINT_RE.search(context)
    if not m:
        return None
    if m.group(1):  # 令和N年
        return 2018 + int(m.group(1))
    if m.group(2):  # YYYY年
        return int(m.group(2))
    if m.group(3):  # YYYY/
        return int(m.group(3))
    return None


def parse_date_candidate(match, context):
    """マッチした日付文字列を datetime.date に変換。年が省略されている場合はcontextから推定"""
    groups = match.groups()
    try:
        if len(groups) == 3 and len(groups[0]) == 4:
            # YYYY年MM月DD日 または YYYY/MM/DD
            y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
            return date(y, m, d)
        elif len(groups) == 2:
            # MM月DD日（年省略）: contextから年を推定、無ければ今日以降になるよう調整
            m, d = int(groups[0]), int(groups[1])
            hint_year = infer_year_from_context(context)
            if hint_year:
                return date(hint_year, m, d)
            candidate = date(THIS_YEAR, m, d)
            if candidate < date.today():
                candidate = date(THIS_YEAR + 1, m, d)
            return candidate
        else:
            return None
    except (ValueError, TypeError):
        return None


def extract_events_from_text(text, school, source_url=None):
    """ページテキストから日付＋周辺テキストを抽出"""
    events = []
    seen_keys = set()
    today = date.today()
    earliest = today - timedelta(days=3)     # 直近3日前まで（ギリギリ見逃し防止）
    latest = today + timedelta(days=270)     # 約9か月先まで

    # 全日付パターンを走査
    for pattern in (DATE_RE_FULL, DATE_RE_NUMERIC, DATE_RE_SHORT):
        for m in pattern.finditer(text):
            # 先にcontextを取得（年推定に必要）
            ctx_start = max(0, m.start() - 50)
            ctx_end = min(len(text), m.end() + 40)
            context = text[ctx_start:ctx_end]
            context = re.sub(r"\s+", " ", context).strip()

            ev_date = parse_date_candidate(m, context)
            if not ev_date:
                continue
            if ev_date < earliest or ev_date > latest:
                continue

            # 除外キーワード判定
            if any(ex in context for ex in EXCLUDE_KEYWORDS):
                continue

            # イベントキーワードを含むか判定
            matched_kw = [kw for kw in EVENT_KEYWORDS if kw in context]
            if not matched_kw:
                continue

            # タイトル推定: 日付文字列の前後から説明会名を含む断片を取る
            title_source = context
            # 最も近いイベントキーワードを中心に
            kw = matched_kw[0]
            kw_pos = context.find(kw)
            if kw_pos >= 0:
                title_start = max(0, kw_pos - 20)
                title_end = min(len(context), kw_pos + 40)
                title = context[title_start:title_end].strip()
            else:
                title = title_source[:60]

            iso = ev_date.isoformat()
            dedupe_key = f"{iso}|{kw}"
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            events.append({
                "event_id": make_event_id(school["id"], iso, title),
                "school_id": school["id"],
                "school_name": school["name"],
                "category": school.get("category", ""),
                "ward": school.get("ward", ""),
                "deviation": school.get("deviation"),
                "date": iso,
                "date_human": ev_date.strftime("%Y/%m/%d (%a)"),
                "title": title,
                "keyword": kw,
                "context": context,
                "source_url": source_url or school.get("url_event") or school.get("url_top", ""),
            })

    # 日付昇順
    events.sort(key=lambda e: e["date"])
    return events


def fetch_page_text(page, url, timeout_ms=20000):
    """1ページ取得→可視テキスト抽出"""
    try:
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        time.sleep(0.3)
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        return text[:30000] if text else ""
    except Exception as e:
        return f"__ERROR__:{str(e)[:200]}"


def find_subpage_links(page, base_url, max_links=3):
    """トップページから説明会関連サブページURLを抽出"""
    try:
        js = """
        (keywords) => {
            const anchors = Array.from(document.querySelectorAll('a'));
            const found = [];
            const seen = new Set();
            for (const a of anchors) {
                const txt = (a.textContent || '').trim();
                const href = a.href || '';
                if (!href || href.startsWith('javascript:') || href.startsWith('mailto:')) continue;
                if (href.includes('#')) continue;
                if (seen.has(href)) continue;
                for (const kw of keywords) {
                    if (txt.includes(kw)) {
                        found.push({text: txt.slice(0, 40), href: href});
                        seen.add(href);
                        break;
                    }
                }
            }
            return found;
        }
        """
        links = page.evaluate(js, SUBPAGE_LINK_KEYWORDS)
        # 同一ドメインのみ、max_links件まで
        base_host = re.sub(r'^https?://', '', base_url).split('/')[0]
        filtered = []
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            link_host = re.sub(r'^https?://', '', href).split('/')[0]
            if link_host != base_host:
                continue
            if href == base_url or href == base_url.rstrip("/"):
                continue
            filtered.append(href)
            if len(filtered) >= max_links:
                break
        return filtered
    except Exception:
        return []


def scrape_school(page, school, timeout_ms=20000):
    """各校公式HPのトップ＋説明会関連サブページを辿ってイベント抽出"""
    url = school.get("url_top", "")
    if not url:
        return {"school": school, "events": [], "error": "no url", "ok": False, "pages_scraped": 0}

    all_events = []
    pages_scraped = 0
    errors = []

    # 優先: url_event（直接指定された説明会ページ）
    url_event = school.get("url_event")
    if url_event:
        ev_text = fetch_page_text(page, url_event, timeout_ms)
        pages_scraped += 1
        if ev_text.startswith("__ERROR__:"):
            errors.append(f"event: {ev_text[10:]}")
        elif ev_text:
            all_events.extend(extract_events_from_text(ev_text, school, source_url=url_event))

    # トップページ
    top_text = fetch_page_text(page, url, timeout_ms)
    if top_text.startswith("__ERROR__:"):
        if not all_events:
            return {"school": school, "events": [], "error": top_text[10:], "ok": False, "pages_scraped": pages_scraped}
        errors.append(f"top: {top_text[10:]}")
    else:
        pages_scraped += 1
        if top_text:
            all_events.extend(extract_events_from_text(top_text, school, source_url=url))

        # サブページリンク抽出（トップ取得成功時のみ）
        sub_links = find_subpage_links(page, url, max_links=5)

        # サブページ巡回
        for sub_url in sub_links:
            time.sleep(0.5)
            sub_text = fetch_page_text(page, sub_url, timeout_ms)
            pages_scraped += 1
            if sub_text and not sub_text.startswith("__ERROR__:"):
                all_events.extend(extract_events_from_text(sub_text, school, source_url=sub_url))

    # 学校内重複除外（date+keywordで重複）
    seen = set()
    unique = []
    for e in all_events:
        key = (e["date"], e["keyword"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    return {"school": school, "events": unique, "error": None, "ok": True, "pages_scraped": pages_scraped}


def diff_detect(current_events, latest_file):
    """前回JSONと比較して新規event_idを抽出"""
    if not latest_file.exists():
        return current_events, []  # 初回は全部「既存扱い」にしないのが良い → 全て新規
    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            prev = json.load(f)
        prev_ids = {e["event_id"] for e in prev.get("events", [])}
    except Exception:
        return current_events, []

    new_events = [e for e in current_events if e["event_id"] not in prev_ids]
    return current_events, new_events


def save_snapshot(events, metadata):
    """差分検知用の最新スナップショット保存"""
    snapshot = {
        "saved_at": datetime.now().isoformat(),
        "meta": metadata,
        "events": events,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    # 日付アーカイブ
    archive = DATA_DIR / f"raw_{TODAY}.json"
    with open(archive, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


# =====================================================================
# HTML生成
# =====================================================================

CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Segoe UI', 'Yu Gothic UI', 'Meiryo', sans-serif;
    background: #f8f9fa;
    color: #333;
    font-size: 15px;
    line-height: 1.7;
    padding: 12px;
}
.container { max-width: 1100px; margin: 0 auto; }
h1 { text-align: center; color: #2c3e50; font-size: 1.6em; margin-bottom: 4px; }
.subtitle { text-align: center; color: #7f8c8d; margin-bottom: 16px; font-size: 0.9em; }

/* タブ */
.tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    border-bottom: 2px solid #e0e6ed;
    padding-bottom: 0;
}
.tab-btn {
    background: #fff;
    border: 1px solid #e0e6ed;
    border-bottom: none;
    padding: 10px 16px;
    border-radius: 8px 8px 0 0;
    cursor: pointer;
    font-size: 0.95em;
    color: #555;
    font-weight: 500;
    transition: all 0.15s;
}
.tab-btn:hover { background: #f0f4f8; }
.tab-btn.active {
    background: #3498db;
    color: #fff;
    border-color: #3498db;
}
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* カード類 */
.section {
    background: #fff;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}
.section h2 {
    color: #2c3e50;
    border-bottom: 3px solid #3498db;
    padding-bottom: 6px;
    margin-bottom: 16px;
    font-size: 1.15em;
}

/* 説明会アイテム */
.event-item {
    border: 1px solid #ecf0f1;
    border-left: 4px solid #3498db;
    border-radius: 6px;
    padding: 12px 14px;
    margin-bottom: 10px;
    background: #fbfcfd;
}
.event-item.is-new { border-left-color: #e74c3c; background: #fff5f4; }
.event-item .ev-date {
    font-weight: bold;
    color: #e74c3c;
    font-size: 1.02em;
    margin-right: 10px;
}
.event-item .ev-school {
    color: #2c3e50;
    font-weight: 600;
    margin-right: 6px;
}
.event-item .ev-kw {
    display: inline-block;
    background: #3498db;
    color: #fff;
    font-size: 0.75em;
    padding: 2px 8px;
    border-radius: 10px;
    margin-left: 6px;
}
.event-item .ev-context {
    color: #7f8c8d;
    font-size: 0.85em;
    margin-top: 6px;
}
.event-item .ev-link { font-size: 0.85em; }
.new-badge {
    display: inline-block;
    background: #e74c3c;
    color: #fff;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.7em;
    font-weight: bold;
    margin-left: 6px;
    vertical-align: middle;
}

/* 学校カード */
.school-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
}
.school-card {
    background: #fff;
    border: 1px solid #e0e6ed;
    border-radius: 10px;
    padding: 14px 16px;
    transition: box-shadow 0.15s;
}
.school-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,0.08); }
.school-card h3 { font-size: 1.05em; color: #2c3e50; margin-bottom: 6px; }
.school-card .dev {
    display: inline-block;
    background: #e74c3c;
    color: #fff;
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 0.85em;
    font-weight: bold;
    margin-right: 6px;
}
.school-card .meta { color: #7f8c8d; font-size: 0.82em; margin: 6px 0; }
.school-card .next-event {
    background: #fef9e7;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 0.85em;
    margin-top: 8px;
    color: #e67e22;
}
.school-card a {
    color: #3498db;
    text-decoration: none;
    font-size: 0.85em;
}

/* 比較表 */
table.compare {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9em;
}
table.compare th, table.compare td {
    padding: 8px 10px;
    text-align: left;
    border-bottom: 1px solid #ecf0f1;
}
table.compare th {
    background: #f0f4f8;
    color: #2c3e50;
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: 0;
}
table.compare th:hover { background: #e1e8ef; }
table.compare tr:hover { background: #fbfcfd; }

/* 新着・エラー */
.empty { color: #95a5a6; text-align: center; padding: 20px; font-size: 0.9em; }
.error-list { font-size: 0.85em; color: #e67e22; }
.error-list li { padding: 4px 0; }

/* モバイル対応 */
@media (max-width: 600px) {
    body { padding: 8px; font-size: 14px; }
    .section { padding: 14px; }
    .tab-btn { padding: 8px 10px; font-size: 0.85em; }
    .school-grid { grid-template-columns: 1fr; }
    table.compare { font-size: 0.82em; }
    table.compare th, table.compare td { padding: 6px 4px; }
}
"""

TAB_JS = """
function showTab(id, btn) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    btn.classList.add('active');
}
function sortTable(tableId, colIdx, type) {
    const table = document.getElementById(tableId);
    const tbody = table.tBodies[0];
    const rows = Array.from(tbody.rows);
    const th = table.tHead.rows[0].cells[colIdx];
    const asc = !th.dataset.asc || th.dataset.asc === 'false';
    rows.sort((a, b) => {
        let x = a.cells[colIdx].innerText.trim();
        let y = b.cells[colIdx].innerText.trim();
        if (type === 'num') { x = parseFloat(x) || 0; y = parseFloat(y) || 0; }
        return asc ? (x > y ? 1 : -1) : (x < y ? 1 : -1);
    });
    rows.forEach(r => tbody.appendChild(r));
    table.tHead.rows[0].querySelectorAll('th').forEach(h => h.dataset.asc = '');
    th.dataset.asc = asc;
}
"""


def render_calendar_tab(all_events, new_ids):
    if not all_events:
        return '<div class="empty">説明会データが見つかりませんでした。各校HPを直接ご確認ください。</div>'
    today = date.today()
    upcoming = [e for e in all_events if date.fromisoformat(e["date"]) >= today]
    upcoming.sort(key=lambda e: e["date"])

    html_parts = [f'<p style="color:#7f8c8d; font-size:0.85em; margin-bottom:12px;">直近{len(upcoming)}件の説明会・イベント</p>']
    for e in upcoming[:80]:
        is_new = e["event_id"] in new_ids
        css_class = "event-item is-new" if is_new else "event-item"
        new_badge = '<span class="new-badge">NEW</span>' if is_new else ""
        html_parts.append(
            f'<div class="{css_class}">'
            f'<span class="ev-date">{escape_html(e["date_human"])}</span>'
            f'<span class="ev-school">{escape_html(e["school_name"])}</span>'
            f'<span class="ev-kw">{escape_html(e["keyword"])}</span>{new_badge}'
            f'<div class="ev-context">{escape_html(e["context"])}</div>'
            f'<div class="ev-link"><a href="{escape_html(e["source_url"])}" target="_blank">詳細ページへ →</a></div>'
            f'</div>'
        )
    return "\n".join(html_parts)


def render_schools_tab(schools, all_events):
    # 学校ごとに次回イベント日を計算
    today = date.today()
    next_event_by_school = {}
    for e in all_events:
        ev_date = date.fromisoformat(e["date"])
        if ev_date < today:
            continue
        sid = e["school_id"]
        if sid not in next_event_by_school or ev_date < date.fromisoformat(next_event_by_school[sid]["date"]):
            next_event_by_school[sid] = e

    # 偏差値降順
    sorted_schools = sorted(schools, key=lambda s: s.get("deviation") or 0, reverse=True)

    html_parts = ['<div class="school-grid">']
    for s in sorted_schools:
        next_e = next_event_by_school.get(s["id"])
        next_html = (
            f'<div class="next-event">📅 次回: {escape_html(next_e["date_human"])} {escape_html(next_e["keyword"])}</div>'
            if next_e else '<div class="next-event" style="background:#f0f4f8; color:#95a5a6;">次回イベント未検出</div>'
        )
        html_parts.append(
            f'<div class="school-card">'
            f'<h3>{escape_html(s["name"])}</h3>'
            f'<span class="dev">偏差値 {s.get("deviation", "-")}</span>'
            f'<div class="meta">{escape_html(s.get("category", ""))} / {escape_html(s.get("ward", ""))}</div>'
            f'{next_html}'
            f'<div style="margin-top:8px;"><a href="{escape_html(s.get("url_top",""))}" target="_blank">公式サイト →</a></div>'
            f'</div>'
        )
    html_parts.append('</div>')
    return "\n".join(html_parts)


def render_compare_tab(schools, all_events):
    today = date.today()
    # 各校の次回イベント日
    next_event_by_school = {}
    event_count_by_school = {}
    for e in all_events:
        sid = e["school_id"]
        event_count_by_school[sid] = event_count_by_school.get(sid, 0) + 1
        ev_date = date.fromisoformat(e["date"])
        if ev_date < today:
            continue
        if sid not in next_event_by_school or ev_date < date.fromisoformat(next_event_by_school[sid]["date"]):
            next_event_by_school[sid] = e

    html_parts = [
        '<table class="compare" id="compareTable">',
        '<thead><tr>',
        '<th onclick="sortTable(\'compareTable\',0,\'str\')">学校名</th>',
        '<th onclick="sortTable(\'compareTable\',1,\'num\')">偏差値</th>',
        '<th onclick="sortTable(\'compareTable\',2,\'str\')">区分</th>',
        '<th onclick="sortTable(\'compareTable\',3,\'str\')">所在地</th>',
        '<th onclick="sortTable(\'compareTable\',4,\'str\')">次回イベント</th>',
        '<th onclick="sortTable(\'compareTable\',5,\'num\')">検出数</th>',
        '</tr></thead><tbody>',
    ]
    for s in sorted(schools, key=lambda x: x.get("deviation") or 0, reverse=True):
        next_e = next_event_by_school.get(s["id"])
        next_txt = f'{next_e["date_human"]} {next_e["keyword"]}' if next_e else "-"
        html_parts.append(
            f'<tr>'
            f'<td><a href="{escape_html(s.get("url_top",""))}" target="_blank">{escape_html(s["name"])}</a></td>'
            f'<td>{escape_html(s.get("deviation","-"))}</td>'
            f'<td>{escape_html(s.get("category",""))}</td>'
            f'<td>{escape_html(s.get("ward",""))}</td>'
            f'<td>{escape_html(next_txt)}</td>'
            f'<td>{event_count_by_school.get(s["id"], 0)}</td>'
            f'</tr>'
        )
    html_parts.append('</tbody></table>')
    return "\n".join(html_parts)


def render_admissions_tab():
    # Phase1ではテキストガイドのみ。Phase2で都教委スクレイプ予定
    return """
    <div style="padding:14px;">
      <h3 style="margin-bottom:12px;">都立高校 入試スケジュール（令和7年度 参考）</h3>
      <p style="color:#7f8c8d; font-size:0.88em; margin-bottom:12px;">
        ※ 正確な最新日程は東京都教育委員会の公式発表をご確認ください。<br>
        このタブはPhase2で都教委サイトから自動取得予定。
      </p>
      <table class="compare">
        <thead><tr><th>区分</th><th>主な日程（参考）</th></tr></thead>
        <tbody>
          <tr><td>推薦入試 出願</td><td>1月中旬</td></tr>
          <tr><td>推薦入試 試験</td><td>1月下旬</td></tr>
          <tr><td>推薦入試 合格発表</td><td>2月上旬</td></tr>
          <tr><td>一次・前期 出願</td><td>2月上旬</td></tr>
          <tr><td>一次・前期 試験</td><td>2月下旬</td></tr>
          <tr><td>一次・前期 合格発表</td><td>3月上旬</td></tr>
          <tr><td>二次・後期 出願</td><td>3月上旬</td></tr>
          <tr><td>二次・後期 試験</td><td>3月中旬</td></tr>
        </tbody>
      </table>
      <p style="margin-top:14px;">
        <a href="https://www.kyoiku.metro.tokyo.lg.jp/admission/high_school/" target="_blank">東京都教育委員会 都立高校入試情報 →</a>
      </p>
    </div>
    """


def render_new_tab(new_events):
    if not new_events:
        return '<div class="empty">新着イベントはありません。</div>'
    html_parts = [f'<p style="color:#7f8c8d; font-size:0.85em; margin-bottom:12px;">前回収集からの新着 {len(new_events)}件</p>']
    for e in new_events:
        html_parts.append(
            f'<div class="event-item is-new">'
            f'<span class="ev-date">{escape_html(e["date_human"])}</span>'
            f'<span class="ev-school">{escape_html(e["school_name"])}</span>'
            f'<span class="ev-kw">{escape_html(e["keyword"])}</span>'
            f'<div class="ev-context">{escape_html(e["context"])}</div>'
            f'<div class="ev-link"><a href="{escape_html(e["source_url"])}" target="_blank">詳細ページへ →</a></div>'
            f'</div>'
        )
    return "\n".join(html_parts)


def generate_dashboard_html(schools, all_events, new_events, scrape_results):
    new_ids = {e["event_id"] for e in new_events}
    ok_count = sum(1 for r in scrape_results if r["ok"])
    error_schools = [r for r in scrape_results if not r["ok"]]

    errors_html = ""
    if error_schools:
        items = "".join(
            f'<li>{escape_html(r["school"]["name"])} - {escape_html(r.get("error", ""))}</li>'
            for r in error_schools
        )
        errors_html = f'<details style="margin-top:10px;"><summary style="cursor:pointer; color:#e67e22;">取得失敗 {len(error_schools)}校</summary><ul class="error-list">{items}</ul></details>'

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>高校受験ダッシュボード - {TODAY_HUMAN}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <h1>🎓 高校受験ダッシュボード</h1>
  <div class="subtitle">
    更新: {TODAY_HUMAN} | 対象校 {len(schools)} | 取得成功 {ok_count} | イベント検出 {len(all_events)} | 新着 {len(new_events)}
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="showTab('tab-new', this)">🆕 新着{' ('+str(len(new_events))+')' if new_events else ''}</button>
    <button class="tab-btn" onclick="showTab('tab-calendar', this)">📅 説明会カレンダー</button>
    <button class="tab-btn" onclick="showTab('tab-schools', this)">🏫 学校カード</button>
    <button class="tab-btn" onclick="showTab('tab-compare', this)">📊 比較表</button>
    <button class="tab-btn" onclick="showTab('tab-admissions', this)">📋 入試日程</button>
  </div>

  <div id="tab-new" class="tab-panel active">
    <div class="section">
      <h2>🆕 新着イベント</h2>
      {render_new_tab(new_events)}
    </div>
  </div>

  <div id="tab-calendar" class="tab-panel">
    <div class="section">
      <h2>📅 説明会・イベントカレンダー</h2>
      {render_calendar_tab(all_events, new_ids)}
    </div>
  </div>

  <div id="tab-schools" class="tab-panel">
    <div class="section">
      <h2>🏫 学校一覧（偏差値順）</h2>
      {render_schools_tab(schools, all_events)}
    </div>
  </div>

  <div id="tab-compare" class="tab-panel">
    <div class="section">
      <h2>📊 比較表（カラムクリックでソート）</h2>
      {render_compare_tab(schools, all_events)}
    </div>
  </div>

  <div id="tab-admissions" class="tab-panel">
    <div class="section">
      <h2>📋 入試日程</h2>
      {render_admissions_tab()}
    </div>
  </div>

  {errors_html}

  <p style="text-align:center; color:#95a5a6; font-size:0.8em; margin-top:20px;">
    高校受験ダッシュボード | 各校公式HPから自動収集 | 偏差値は手動メンテデータ
  </p>
</div>
<script>{TAB_JS}</script>
</body>
</html>
"""
    return html


# =====================================================================
# メイン
# =====================================================================

def main():
    print("=" * 60)
    print(f"  高校受験ダッシュボード - {TODAY_HUMAN}")
    print("=" * 60)

    config = load_config()
    schools = config["schools"]
    scrape_conf = config.get("scrape", {})
    timeout_ms = scrape_conf.get("timeout_ms", 20000)
    wait_ms = scrape_conf.get("wait_between_ms", 2000)

    print(f"\n対象校: {len(schools)}校\n")

    from playwright.sync_api import sync_playwright

    all_events = []
    scrape_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        page = context.new_page()

        for i, school in enumerate(schools):
            print(f"[{i+1}/{len(schools)}] {school['name']} ...")
            result = scrape_school(page, school, timeout_ms=timeout_ms)
            scrape_results.append(result)
            if result["ok"]:
                print(f"  → {len(result['events'])}イベント検出")
                all_events.extend(result["events"])
            else:
                print(f"  ✗ {result['error']}")
            if i < len(schools) - 1:
                wait = wait_ms / 1000 + random.uniform(0.3, 1.0)
                time.sleep(wait)

        browser.close()

    # 重複除外（同school_id+date+keywordで重複する場合あり）
    seen = set()
    unique_events = []
    for e in all_events:
        key = (e["school_id"], e["date"], e["keyword"])
        if key in seen:
            continue
        seen.add(key)
        unique_events.append(e)
    unique_events.sort(key=lambda e: e["date"])

    print(f"\n抽出イベント合計: {len(all_events)} → 重複除外後: {len(unique_events)}")

    # 差分検知
    first_run = not LATEST_JSON.exists()
    _, new_events = diff_detect(unique_events, LATEST_JSON)
    if first_run:
        print("[初回実行] すべて既存扱いでスナップショット保存のみ")
        new_events = []
    else:
        print(f"新着イベント: {len(new_events)}件")

    # スナップショット保存
    save_snapshot(unique_events, {"schools_count": len(schools), "today": TODAY})

    # HTML生成
    html = generate_dashboard_html(schools, unique_events, new_events, scrape_results)
    index_path = REPORTS_DIR / "index.html"
    archive_path = REPORTS_DIR / f"dashboard_{TODAY}.html"
    root_index_path = BASE_DIR / "index.html"  # GitHub Pages配信用（リポジトリルート）
    for p in (index_path, archive_path, root_index_path):
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
    print(f"\nHTML生成: {index_path}")
    print(f"GitHub Pages用: {root_index_path}")

    print(f"\n{'=' * 60}")
    print(f"  完了")
    print(f"{'=' * 60}")

    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open(str(index_path))


if __name__ == "__main__":
    main()
