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
    # 教員・塾向けイベント（親・受験生向けではない。contextに出ると"中学校の情報"に見えてしまう）
    "塾対象", "塾および", "塾教員", "塾関係者", "塾・教員", "塾の先生",
    "教員対象", "教員向け", "教員・塾", "中学校教員", "中学校の先生",
    "学習塾", "塾・予備校",
    # 中学校（中学受験・中等部）の情報を除外。長男は高校受験生なので不要
    "中学受験", "中学入試", "中学校入試", "中学校説明会", "中学校体験",
    "中等部", "中学部", "中学校オープン", "中学受検", "中学校行事",
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
    earliest = today                          # 当日以降のみ抽出
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

            # 申込・締切系の日付を除外（開催日ではなく手続き期限）
            pre_start = max(0, m.start() - 20)
            pre_text = text[pre_start:m.start()]
            pre_text = re.sub(r"\s+", "", pre_text)
            if re.search(r"(申込|締切|期限|受付|〜|～|→|まで)", pre_text):
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

/* 固定ヘッダー */
.header-fixed {
    position: sticky;
    top: 0;
    z-index: 100;
    background: #f8f9fa;
    padding: 8px 0 0 0;
    margin: 0 -12px;
    padding-left: 12px;
    padding-right: 12px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06);
}
h1 { text-align: center; color: #2c3e50; font-size: 1.4em; margin-bottom: 2px; }
.subtitle { text-align: center; color: #7f8c8d; margin-bottom: 8px; font-size: 0.8em; }

/* タブ */
.tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 0;
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

/* カレンダーグリッド（月別ビュー） */
.cal-nav {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.cal-nav button {
    padding: 6px 14px;
    border: 1px solid #d1d8e0;
    background: #fff;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.95em;
}
.cal-nav button:hover { background: #f0f4f8; }
.cal-nav #cal-title {
    font-size: 1.15em;
    font-weight: 600;
    color: #2c3e50;
    min-width: 130px;
    text-align: center;
}
.cal-nav .cal-today {
    margin-left: auto;
    background: #3498db;
    color: #fff;
    border-color: #3498db;
}
.cal-legend {
    font-size: 0.78em;
    color: #7f8c8d;
    margin-bottom: 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}
.cal-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    vertical-align: middle;
    margin-right: 3px;
}
.cal-dot.d-top { background: #e74c3c; }
.cal-dot.d-hi  { background: #e67e22; }
.cal-dot.d-mid { background: #3498db; }
.cal-dot.d-low { background: #27ae60; }

.cal-weekdays {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
    margin-bottom: 2px;
}
.cal-wd {
    text-align: center;
    padding: 6px 0;
    font-size: 0.85em;
    font-weight: 600;
    color: #2c3e50;
    background: #f0f4f8;
    border-radius: 4px;
}
.cal-wd.sun { color: #e74c3c; }
.cal-wd.sat { color: #3498db; }

.cal-days {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 2px;
}
.cal-cell {
    min-height: 84px;
    background: #fff;
    border: 1px solid #ecf0f1;
    border-radius: 4px;
    padding: 4px;
    font-size: 0.78em;
    position: relative;
    overflow: hidden;
}
.cal-cell.empty { background: #fafbfc; border-color: transparent; }
.cal-cell.has-events { cursor: pointer; }
.cal-cell.has-events:hover { background: #f8fafc; box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
/* 日曜・祝日: 薄いピンク背景＋赤文字 */
.cal-cell.sun, .cal-cell.holiday { background: #fff5f5; }
.cal-cell.sun.has-events:hover, .cal-cell.holiday.has-events:hover { background: #ffe8e8; }
/* 土曜: 薄い水色背景＋青文字 */
.cal-cell.sat { background: #f4f9ff; }
.cal-cell.sat.has-events:hover { background: #e6f1fc; }
/* today / selected は土日祝より優先 */
.cal-cell.today { background: #fef9e7; border-color: #f1c40f; border-width: 2px; }
.cal-cell.selected { background: #e8f4fc; border-color: #3498db; border-width: 2px; }
.cal-cell .cal-day {
    font-weight: 600;
    color: #2c3e50;
    font-size: 0.95em;
    margin-bottom: 2px;
    display: flex;
    align-items: baseline;
    gap: 4px;
}
.cal-cell.sun .cal-day, .cal-cell.holiday .cal-day { color: #e74c3c; }
.cal-cell.sat .cal-day { color: #3498db; }
.cal-holiday-name {
    font-size: 0.7em;
    font-weight: 500;
    color: #c0392b;
    background: rgba(231, 76, 60, 0.1);
    padding: 1px 4px;
    border-radius: 3px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
}
.cal-items { display: flex; flex-direction: column; gap: 2px; }
.cal-item {
    padding: 2px 4px;
    border-radius: 3px;
    color: #fff;
    font-size: 0.72em;
    line-height: 1.25;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.cal-item.d-top { background: #e74c3c; }
.cal-item.d-hi  { background: #e67e22; }
.cal-item.d-mid { background: #3498db; }
.cal-item.d-low { background: #27ae60; }
.cal-item.is-new { box-shadow: 0 0 0 2px #ff3b30 inset; font-weight: 700; }
.cal-more {
    font-size: 0.7em;
    color: #7f8c8d;
    text-align: right;
    padding-right: 2px;
}

.cal-detail {
    margin-top: 16px;
    padding: 0;
    border-radius: 8px;
}
.cal-detail:not(:empty) {
    background: #f8fafc;
    padding: 14px 16px;
    border: 1px solid #e0e6ed;
}
.cal-detail h3 {
    font-size: 1em;
    margin-bottom: 10px;
    color: #2c3e50;
}
.cal-detail-item {
    background: #fff;
    border-left: 4px solid #bdc3c7;
    padding: 8px 10px;
    margin-bottom: 8px;
    border-radius: 4px;
}
.cal-detail-item.d-top { border-left-color: #e74c3c; }
.cal-detail-item.d-hi  { border-left-color: #e67e22; }
.cal-detail-item.d-mid { border-left-color: #3498db; }
.cal-detail-item.d-low { border-left-color: #27ae60; }
.cal-detail-item .cdi-top {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-bottom: 4px;
    font-size: 0.9em;
}
.cal-detail-item .ev-kw {
    background: #ecf0f1;
    color: #2c3e50;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 0.85em;
}
.cal-detail-item .cdi-link { font-size: 0.85em; }
.cal-detail-item .cdi-link a { color: #3498db; text-decoration: none; }

.cal-list-wrap summary {
    cursor: pointer;
    padding: 8px 10px;
    background: #ecf0f1;
    border-radius: 6px;
    font-size: 0.9em;
    color: #2c3e50;
    margin-bottom: 10px;
}
.cal-list-wrap[open] summary { background: #d1d8e0; }

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
    body { padding: 6px; font-size: 14px; }
    .header-fixed { margin: 0 -6px; padding: 6px 6px 0 6px; }
    h1 { font-size: 1.2em; }
    .subtitle { font-size: 0.72em; margin-bottom: 6px; }
    .section { padding: 12px; }
    .tab-btn { padding: 6px 8px; font-size: 0.8em; }
    .school-grid { grid-template-columns: 1fr; }
    table.compare { font-size: 0.82em; }
    table.compare th, table.compare td { padding: 6px 4px; }
    /* カレンダー: モバイルではセル最小高さ削減・文字縮小 */
    .cal-cell { min-height: 58px; padding: 2px; font-size: 0.72em; }
    .cal-cell .cal-day { font-size: 0.85em; margin-bottom: 1px; }
    .cal-wd { font-size: 0.75em; padding: 4px 0; }
    .cal-item { font-size: 0.62em; padding: 1px 3px; }
    .cal-nav #cal-title { font-size: 1em; min-width: 110px; }
    .cal-nav button { padding: 4px 10px; font-size: 0.85em; }
    /* 祝日名はスマホでは表示領域が狭いので小さくする（タップでツールチップ代わり） */
    .cal-holiday-name { font-size: 0.6em; padding: 0 2px; }
}
"""

TAB_JS = """
function showTab(id, btn) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    btn.classList.add('active');
    if (id === 'tab-map' && window._schoolMap) {
        setTimeout(function(){ window._schoolMap.invalidateSize(); }, 100);
    }
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

/* ===== カレンダーグリッド ===== */
var calCurrent = null;  // Date (1日)
var calSelectedKey = null; // 'YYYY-MM-DD'

// 日本の祝日（2026/2027 受験準備期間をカバー）
// 出典: 内閣府「国民の祝日」
var CAL_HOLIDAYS = {
    // 2026
    '2026-01-01': '元日',
    '2026-01-12': '成人の日',
    '2026-02-11': '建国記念の日',
    '2026-02-23': '天皇誕生日',
    '2026-03-20': '春分の日',
    '2026-04-29': '昭和の日',
    '2026-05-03': '憲法記念日',
    '2026-05-04': 'みどりの日',
    '2026-05-05': 'こどもの日',
    '2026-05-06': '振替休日',
    '2026-07-20': '海の日',
    '2026-08-11': '山の日',
    '2026-09-21': '敬老の日',
    '2026-09-22': '国民の休日',
    '2026-09-23': '秋分の日',
    '2026-10-12': 'スポーツの日',
    '2026-11-03': '文化の日',
    '2026-11-23': '勤労感謝の日',
    // 2027
    '2027-01-01': '元日',
    '2027-01-11': '成人の日',
    '2027-02-11': '建国記念の日',
    '2027-02-23': '天皇誕生日',
    '2027-03-21': '春分の日',
    '2027-03-22': '振替休日',
    '2027-04-29': '昭和の日',
    '2027-05-03': '憲法記念日',
    '2027-05-04': 'みどりの日',
    '2027-05-05': 'こどもの日'
};

function calDevClass(d) {
    d = Number(d) || 0;
    if (d >= 65) return 'd-top';
    if (d >= 58) return 'd-hi';
    if (d >= 50) return 'd-mid';
    return 'd-low';
}
function calShortName(name) {
    return (name || '').replace('都立', '').replace('高等学校', '').replace('高校', '');
}
function calPad(n) { return n < 10 ? '0' + n : '' + n; }

function initCalendar() {
    if (!window.CAL_INIT_MONTH) return;
    var parts = window.CAL_INIT_MONTH.split('-');
    calCurrent = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, 1);
    calSelectedKey = null;
    // クリック委譲（セルの data-dk にISO日付を埋め込む）
    var grid = document.getElementById('cal-grid');
    if (grid && !grid.dataset.bound) {
        grid.addEventListener('click', function (ev) {
            var cell = ev.target.closest('[data-dk]');
            if (cell && cell.classList.contains('has-events')) {
                showCalDetail(cell.getAttribute('data-dk'));
            }
        });
        grid.dataset.bound = '1';
    }
    renderCalendar();
}
function calPrev() { if (!calCurrent) return; calCurrent.setMonth(calCurrent.getMonth() - 1); renderCalendar(); }
function calNext() { if (!calCurrent) return; calCurrent.setMonth(calCurrent.getMonth() + 1); renderCalendar(); }
function calToday() {
    var t = new Date();
    calCurrent = new Date(t.getFullYear(), t.getMonth(), 1);
    renderCalendar();
}

function renderCalendar() {
    var grid = document.getElementById('cal-grid');
    var title = document.getElementById('cal-title');
    if (!grid || !calCurrent) return;
    var y = calCurrent.getFullYear();
    var m = calCurrent.getMonth();
    title.textContent = y + '年' + (m + 1) + '月';

    var events = (window.CAL_EVENTS || []).filter(function (e) {
        var p = e.date.split('-');
        return parseInt(p[0], 10) === y && parseInt(p[1], 10) === m + 1;
    });
    var byDay = {};
    events.forEach(function (e) {
        var d = parseInt(e.date.split('-')[2], 10);
        (byDay[d] = byDay[d] || []).push(e);
    });

    var today = new Date();
    var firstDow = new Date(y, m, 1).getDay();
    var daysInMonth = new Date(y, m + 1, 0).getDate();

    var wd = ['日', '月', '火', '水', '木', '金', '土'];
    var html = '<div class="cal-weekdays">';
    for (var i = 0; i < 7; i++) {
        var wdCls = i === 0 ? ' sun' : (i === 6 ? ' sat' : '');
        html += '<div class="cal-wd' + wdCls + '">' + wd[i] + '</div>';
    }
    html += '</div><div class="cal-days">';

    for (var s = 0; s < firstDow; s++) {
        html += '<div class="cal-cell empty"></div>';
    }

    for (var d = 1; d <= daysInMonth; d++) {
        var evs = byDay[d] || [];
        var isToday = (y === today.getFullYear() && m === today.getMonth() && d === today.getDate());
        var dow = (firstDow + d - 1) % 7;
        var dateKey = y + '-' + calPad(m + 1) + '-' + calPad(d);
        var holiday = CAL_HOLIDAYS[dateKey];
        var cls = 'cal-cell';
        if (isToday) cls += ' today';
        if (evs.length > 0) cls += ' has-events';
        if (dow === 0) cls += ' sun';
        if (dow === 6) cls += ' sat';
        if (holiday) cls += ' holiday';
        if (calSelectedKey === dateKey) cls += ' selected';
        var dayLabel = '' + d;
        if (holiday) {
            dayLabel += '<span class="cal-holiday-name" title="' +
                holiday.replace(/"/g, '&quot;') + '">' + holiday + '</span>';
        }
        var inner = '<div class="cal-day">' + dayLabel + '</div>';
        if (evs.length > 0) {
            inner += '<div class="cal-items">';
            var limit = Math.min(evs.length, 3);
            for (var k = 0; k < limit; k++) {
                var e = evs[k];
                var devCls = calDevClass(e.deviation);
                var newCls = e.is_new ? ' is-new' : '';
                inner += '<div class="cal-item ' + devCls + newCls + '" title="' +
                    (e.school_name + ' ' + e.keyword).replace(/"/g, '&quot;') + '">' +
                    calShortName(e.school_name) + '</div>';
            }
            if (evs.length > limit) {
                inner += '<div class="cal-more">+' + (evs.length - limit) + '</div>';
            }
            inner += '</div>';
        }
        html += '<div class="' + cls + '" data-dk="' + dateKey + '">' + inner + '</div>';
    }
    html += '</div>';
    grid.innerHTML = html;

    // 選択中の詳細があれば再描画（同月内なら維持）
    if (calSelectedKey && calSelectedKey.indexOf(y + '-' + calPad(m + 1) + '-') === 0) {
        renderCalDetail(calSelectedKey);
    } else {
        var detail = document.getElementById('cal-detail');
        if (detail) detail.innerHTML = '';
        calSelectedKey = null;
    }
}

function showCalDetail(dateKey) {
    calSelectedKey = dateKey;
    renderCalDetail(dateKey);
    // 選択セルのハイライト更新
    renderCalendar();
    var el = document.getElementById('cal-detail');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function renderCalDetail(dateKey) {
    var detail = document.getElementById('cal-detail');
    if (!detail) return;
    var evs = (window.CAL_EVENTS || []).filter(function (e) { return e.date === dateKey; });
    if (evs.length === 0) { detail.innerHTML = ''; return; }
    var parts = dateKey.split('-').map(Number);
    var dt = new Date(parts[0], parts[1] - 1, parts[2]);
    var wd = ['日', '月', '火', '水', '木', '金', '土'][dt.getDay()];
    var html = '<h3>📅 ' + parts[0] + '年' + parts[1] + '月' + parts[2] + '日(' + wd + ') のイベント ' + evs.length + '件</h3>';
    evs.forEach(function (e) {
        var devCls = calDevClass(e.deviation);
        var newBadge = e.is_new ? '<span class="new-badge">NEW</span>' : '';
        html += '<div class="cal-detail-item ' + devCls + '">' +
            '<div class="cdi-top">' +
            '<span class="cal-dot ' + devCls + '"></span>' +
            '<strong>' + e.school_name + (e.deviation ? '(' + e.deviation + ')' : '') + '</strong>' +
            '<span class="ev-kw">' + e.keyword + '</span>' + newBadge +
            '</div>' +
            '<div class="cdi-link"><a href="' + e.source_url + '" target="_blank" rel="noopener">詳細ページへ →</a></div>' +
            '</div>';
    });
    detail.innerHTML = html;
}

// タブ切替時、カレンダータブを開いたら初期化（未初期化の場合）
(function () {
    var _origShowTab = showTab;
    showTab = function (id, btn) {
        _origShowTab(id, btn);
        if (id === 'tab-calendar' && calCurrent === null) {
            initCalendar();
        }
    };
})();
"""


def render_calendar_tab(all_events, new_ids):
    if not all_events:
        return '<div class="empty">説明会データが見つかりませんでした。各校HPを直接ご確認ください。</div>'
    today = date.today()
    upcoming = [e for e in all_events if date.fromisoformat(e["date"]) >= today]
    upcoming.sort(key=lambda e: e["date"])

    # JS に渡すイベント配列（カレンダーグリッド用の最小データ）
    events_for_js = []
    for e in upcoming:
        events_for_js.append({
            "id": e["event_id"],
            "date": e["date"],
            "school_name": e["school_name"],
            "deviation": e.get("deviation") or 0,
            "keyword": e["keyword"],
            "source_url": e["source_url"],
            "is_new": e["event_id"] in new_ids,
        })
    events_json = json.dumps(events_for_js, ensure_ascii=False)

    # 初期表示月: 最初のイベント月（= 今月以降で最も近い）。無ければ今月
    if upcoming:
        init_month = upcoming[0]["date"][:7]
    else:
        init_month = today.strftime("%Y-%m")

    # 折りたたみリスト（従来表示を残す）
    list_parts = []
    for e in upcoming[:120]:
        is_new = e["event_id"] in new_ids
        css_class = "event-item is-new" if is_new else "event-item"
        new_badge = '<span class="new-badge">NEW</span>' if is_new else ""
        list_parts.append(
            f'<div class="{css_class}">'
            f'<span class="ev-date">{escape_html(e["date_human"])}</span>'
            f'<span class="ev-school">{escape_html(e["school_name"])}({e.get("deviation","")})</span>'
            f'<span class="ev-kw">{escape_html(e["keyword"])}</span>{new_badge}'
            f'<div class="ev-link"><a href="{escape_html(e["source_url"])}" target="_blank">詳細ページへ →</a></div>'
            f'</div>'
        )
    list_html = "\n".join(list_parts) if list_parts else '<div class="empty">直近のイベントはありません</div>'

    html = f"""
    <div class="cal-nav">
      <button type="button" onclick="calPrev()">◀</button>
      <span id="cal-title">-</span>
      <button type="button" onclick="calNext()">▶</button>
      <button type="button" class="cal-today" onclick="calToday()">今日</button>
    </div>
    <div class="cal-legend">
      <span><span class="cal-dot d-top"></span>挑戦(65+)</span>
      <span><span class="cal-dot d-hi"></span>実力(58-64)</span>
      <span><span class="cal-dot d-mid"></span>安全寄り(50-57)</span>
      <span><span class="cal-dot d-low"></span>安全(~49)</span>
      <span style="color:#e74c3c;">■日祝</span>
      <span style="color:#3498db;">■土</span>
      <span>※日付タップで詳細</span>
    </div>
    <div id="cal-grid"></div>
    <div id="cal-detail"></div>

    <details class="cal-list-wrap" style="margin-top:24px;">
      <summary>📋 全イベント一覧（{len(upcoming)}件・日付順リスト）</summary>
      {list_html}
    </details>

    <script>
      window.CAL_EVENTS = {events_json};
      window.CAL_INIT_MONTH = "{init_month}";
      if (typeof initCalendar === 'function') initCalendar();
    </script>
    """
    return html


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
        event_link = s.get("url_event") or s.get("url_top", "")
        af = s.get("admission_fee", 0)
        at = s.get("annual_tuition", 0)
        of_ = s.get("other_fees", 0)
        fee_html = f'<div class="meta" style="margin-top:4px;">💰 入学金 {af:,}円 / 年間 {at + of_:,}円</div>'
        html_parts.append(
            f'<div class="school-card">'
            f'<h3>{escape_html(s["name"])}</h3>'
            f'<span class="dev">偏差値 {s.get("deviation", "-")}</span>'
            f'<div class="meta">{escape_html(s.get("category", ""))} / {escape_html(s.get("ward", ""))}</div>'
            f'{fee_html}'
            f'{next_html}'
            f'<div style="margin-top:8px;"><a href="{escape_html(event_link)}" target="_blank">説明会ページへ →</a></div>'
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
        '<th onclick="sortTable(\'compareTable\',4,\'num\')">入学金</th>',
        '<th onclick="sortTable(\'compareTable\',5,\'num\')">年間費用</th>',
        '<th onclick="sortTable(\'compareTable\',6,\'str\')">次回イベント</th>',
        '<th onclick="sortTable(\'compareTable\',7,\'num\')">検出数</th>',
        '</tr></thead><tbody>',
    ]
    for s in sorted(schools, key=lambda x: x.get("deviation") or 0, reverse=True):
        next_e = next_event_by_school.get(s["id"])
        next_txt = f'{next_e["date_human"]} {next_e["keyword"]}' if next_e else "-"
        event_link = s.get("url_event") or s.get("url_top", "")
        af = s.get("admission_fee", 0)
        at = s.get("annual_tuition", 0)
        of_ = s.get("other_fees", 0)
        annual_total = at + of_
        html_parts.append(
            f'<tr>'
            f'<td><a href="{escape_html(event_link)}" target="_blank">{escape_html(s["name"])}</a></td>'
            f'<td>{escape_html(s.get("deviation","-"))}</td>'
            f'<td>{escape_html(s.get("category",""))}</td>'
            f'<td>{escape_html(s.get("ward",""))}</td>'
            f'<td>{af:,}</td>'
            f'<td>{annual_total:,}</td>'
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


def render_map_tab(schools, config):
    """Leaflet.js + OpenStreetMap の地図タブ"""
    home = config.get("home", {})
    home_lat = home.get("lat", 35.7170)
    home_lng = home.get("lng", 139.4698)

    # 偏差値→色（ヒートマップ風）
    # 65+: 赤(挑戦), 58-64: 橙(実力相応), 50-57: 青(安全寄り), ~49: 緑(安全)
    markers_js = ""
    for s in schools:
        lat = s.get("lat")
        lng = s.get("lng")
        if not lat or not lng:
            continue
        dev = s.get("deviation", 0)
        if dev >= 65:
            color = "#e74c3c"  # 赤
            label = "挑戦"
        elif dev >= 58:
            color = "#e67e22"  # 橙
            label = "実力圏"
        elif dev >= 50:
            color = "#3498db"  # 青
            label = "安全寄り"
        else:
            color = "#27ae60"  # 緑
            label = "安全"

        cat = escape_html(s.get("category", ""))
        ward = escape_html(s.get("ward", ""))
        name = escape_html(s["name"])
        note = escape_html(s.get("_note", ""))
        url = escape_html(s.get("url_event") or s.get("url_top", ""))
        af = s.get("admission_fee", 0)
        at = s.get("annual_tuition", 0)
        of_ = s.get("other_fees", 0)
        fee_line = f"入学金 {af:,}円 / 年間 {at + of_:,}円"

        popup = f"<b>{name}</b><br>"
        popup += f"偏差値 {dev}（{label}）<br>"
        popup += f"{cat} / {ward}<br>"
        popup += f"💰 {fee_line}<br>"
        if note:
            popup += f"{note}<br>"
        popup += f'<a href=\\"{url}\\" target=\\"_blank\\">公式HP →</a>'

        markers_js += f"""
      L.circleMarker([{lat}, {lng}], {{
        radius: 10, fillColor: '{color}', color: '#fff', weight: 2, fillOpacity: 0.85
      }}).addTo(map).bindPopup("{popup}");
      L.marker([{lat}, {lng}], {{
        icon: L.divIcon({{
          className: 'dev-label',
          html: '<span style="background:{color};color:#fff;padding:1px 5px;border-radius:8px;font-size:11px;font-weight:bold;white-space:nowrap;">{dev} {name[:6]}</span>',
          iconSize: [0, 0],
          iconAnchor: [-12, 4]
        }})
      }}).addTo(map);
"""

    return f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <div style="margin-bottom:10px;">
      <span style="margin-right:14px;"><span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#e74c3c;vertical-align:middle;"></span> 挑戦（65+）</span>
      <span style="margin-right:14px;"><span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#e67e22;vertical-align:middle;"></span> 実力圏（58-64）</span>
      <span style="margin-right:14px;"><span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#3498db;vertical-align:middle;"></span> 安全寄り（50-57）</span>
      <span><span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#27ae60;vertical-align:middle;"></span> 安全（~49）</span>
    </div>
    <div id="schoolMap" style="height:500px; border-radius:10px; border:1px solid #e0e6ed;"></div>
    <script>
    (function() {{
      var map = L.map('schoolMap').setView([{home_lat}, {home_lng}], 12);
      window._schoolMap = map;
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 18
      }}).addTo(map);
      // 自宅マーカー
      L.marker([{home_lat}, {home_lng}], {{
        icon: L.divIcon({{
          className: 'home-icon',
          html: '<div style="background:#2c3e50;color:#fff;padding:4px 10px;border-radius:14px;font-size:12px;font-weight:bold;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.3);white-space:nowrap;">🏠 自宅</div>',
          iconSize: [0, 0],
          iconAnchor: [30, 15]
        }})
      }}).addTo(map);
      {markers_js}
      // 全マーカーが見える範囲にフィット
      var bounds = L.latLngBounds([[{home_lat}, {home_lng}]]);
""" + "".join(
        f"      bounds.extend([{s['lat']}, {s['lng']}]);\n"
        for s in schools if s.get("lat") and s.get("lng")
    ) + """      map.fitBounds(bounds, {padding: [30, 30]});
    })();
    </script>
    """


def render_new_tab(new_events):
    if not new_events:
        return '<div class="empty">新着イベントはありません。</div>'
    html_parts = [f'<p style="color:#7f8c8d; font-size:0.85em; margin-bottom:12px;">前回収集からの新着 {len(new_events)}件</p>']
    for e in new_events:
        html_parts.append(
            f'<div class="event-item is-new">'
            f'<span class="ev-date">{escape_html(e["date_human"])}</span>'
            f'<span class="ev-school">{escape_html(e["school_name"])}({e.get("deviation","")})</span>'
            f'<span class="ev-kw">{escape_html(e["keyword"])}</span>'
            f'<div class="ev-context">{escape_html(e["context"])}</div>'
            f'<div class="ev-link"><a href="{escape_html(e["source_url"])}" target="_blank">詳細ページへ →</a></div>'
            f'</div>'
        )
    return "\n".join(html_parts)


def generate_dashboard_html(schools, all_events, new_events, scrape_results, config=None):
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
  <div class="header-fixed">
    <h1>🎓 高校受験ダッシュボード</h1>
    <div class="subtitle">
      更新: {TODAY_HUMAN} | 対象校 {len(schools)} | 取得成功 {ok_count} | イベント検出 {len(all_events)} | 新着 {len(new_events)}
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('tab-new', this)">🆕 新着{' ('+str(len(new_events))+')' if new_events else ''}</button>
      <button class="tab-btn" onclick="showTab('tab-calendar', this)">📅 カレンダー</button>
      <button class="tab-btn" onclick="showTab('tab-schools', this)">🏫 学校</button>
      <button class="tab-btn" onclick="showTab('tab-compare', this)">📊 比較</button>
      <button class="tab-btn" onclick="showTab('tab-admissions', this)">📋 入試</button>
      <button class="tab-btn" onclick="showTab('tab-map', this)">🗺 地図</button>
    </div>
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

  <div id="tab-map" class="tab-panel">
    <div class="section">
      <h2>🗺 学校マップ</h2>
      {render_map_tab(schools, config or {{}}) }
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
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
            ignore_https_errors=True,
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
    html = generate_dashboard_html(schools, unique_events, new_events, scrape_results, config)
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
