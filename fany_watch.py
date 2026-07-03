#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FANYチケット 出演者ウォッチャー（新着通知 + 先着リマインド）
============================================================
「ドンデコルテ」または「渡辺銀次」が"出演"欄に含まれる公演を監視し、Discordに通知する。

2つのモードがある（FANYへのアクセスは scrape のみ・2時間おき想定）:
  python fany_watch.py scrape   … FANYを巡回。新着公演を通知し、先着販売の日程を保存
  python fany_watch.py remind   … FANYには触れず、保存済みの先着日程だけを見てリマインド送信
  python fany_watch.py both     … 両方（ローカル単発実行や手動実行用。既定）

新着通知の内容 : 公演タイトル / 公演日 / 場所 / 出演者 / 申し込み日程
  - 抽選販売 → 受付期間（開始〜終了）
  - 先着販売 → 受付開始時間
複数同時追加時 : 1公演の詳細を送ったあと「他〇件の公演が追加されました」を送信

先着リマインド（日本時間で判定）:
  - 販売開始【前日の22:00】
  - 販売開始の【1時間前】
  ※ remind を十分な頻度（例: 45分おき）で回すことで上記タイミングに発火する
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ========= 設定 =========
KEYWORDS = ["ドンデコルテ", "渡辺銀次"]
BASE = "https://ticket.fany.lol"
SEARCH = BASE + "/search/event"
JST = ZoneInfo("Asia/Tokyo")

# 通知先Discord Webhook（環境変数があれば優先）。
# ※このURLは秘密情報です。公開リポジトリに置くと第三者が悪用できます。
#   公開運用ではSecretに入れて下の既定値は空にし、漏れたらDiscordで再生成してください。
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or \
    ""

STATE_FILE = Path(os.environ.get("FANY_STATE_FILE", "fany_seen.json"))
REQUEST_INTERVAL = 1.5
TIMEOUT = 20
USER_AGENT = "fany-personal-watch/2.0 (personal use)"
MAX_DETAIL_FETCH = 60          # 1回のscrapeで詳細取得する上限
R1_GRACE_H = 4                 # 前日22:00リマインドの発火猶予(時間)。過ぎたら未送信のまま既読化
SALE_PRUNE_DAYS = 2            # 販売開始からこの日数を過ぎた先着枠は掃除

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})

DATE = r"20\d{2}/\d{1,2}/\d{1,2}\([日月火水木金土]\)\s*\d{1,2}:\d{2}"
SALE_RE = re.compile(
    r"(抽選販売|先着販売|一般発売)\s*([^\n]*?)受付期間[：:]\s*"
    r"(" + DATE + r")\s*[～〜\-–]\s*(" + DATE + r")\s*"
    r"(受付前|受付中|受付終了|販売中|発売中|発売前)?"
)


# ========= 取得 =========
def get(url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    time.sleep(REQUEST_INTERVAL)
    return r.text


def search_event_ids(keyword):
    html = get(SEARCH, params={"keywords": keyword, "search_type": "search_string"})
    ids, seen = [], set()
    for i in re.findall(r"/event/detail/(\d+)", html):
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


# ========= 解析 =========
def _field(text, label, nexts):
    pat = re.compile(r"(?:^|\n)" + re.escape(label) + r"\n(.*?)(?=\n(?:" +
                     "|".join(map(re.escape, nexts)) + r")\n|\Z)", re.S)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


def parse_fields(soup, text):
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = re.split(r"\s*[|｜]\s*", og["content"].strip())[0].strip()
    dt = _field(text, "日時", ["会場名", "出演者", "注意事項"])
    venue = _field(text, "会場名", ["出演者", "注意事項", "問合せ先"])
    cast = _field(text, "出演者", ["注意事項", "問合せ先", "その他", "受付", "料金"])
    date = ""
    dm = re.search(r"(20\d{2}/\d{1,2}/\d{1,2}\([日月火水木金土]\)(?:\s*\d{1,2}:\d{2})?)\s*(.*?)\s*(?:開場|開演|$)", dt)
    if dm:
        date = dm.group(1).strip()
        if not title:
            title = dm.group(2).strip()
    if not title and soup.title:
        title = re.split(r"\s*[|｜]\s*", soup.title.get_text(strip=True))[0].strip()
    return {"title": title or "(タイトル取得できず)", "date": date, "venue": venue, "cast": cast}


def parse_sales(text):
    """受付欄を解析。[{cat:抽選/先着, phase, start, end, status}] を返す。"""
    out, seen = [], set()
    for m in SALE_RE.finditer(text):
        kind, name, start, end, status = m.groups()
        cat = "抽選" if ("抽選" in kind or "抽選" in name) else "先着"
        key = (cat, start, end)
        if key in seen:
            continue
        seen.add(key)
        phase = ""
        pm = re.search(r"(一次|二次|三次|四次)", name or "")
        if pm:
            phase = pm.group(1) + "先行"
        out.append({"cat": cat, "phase": phase, "start": start, "end": end,
                    "status": (status or "").strip()})
    return out


def sales_lines(sales):
    lines = []
    for s in sales:
        st = f"（{s['status']}）" if s["status"] else ""
        if s["cat"] == "抽選":
            lbl = "抽選受付" + (f"[{s['phase']}]" if s["phase"] else "")
            lines.append(f"{lbl}: {s['start']} 〜 {s['end']}{st}")
        else:
            lines.append(f"先着受付開始: {s['start']}{st}")
    return lines or ["（受付情報は詳細ページをご確認ください）"]


def fetch_event(event_id):
    url = f"{BASE}/event/detail/{event_id}"
    html = get(url)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    f = parse_fields(soup, text)
    sales = parse_sales(text)
    f.update({"id": event_id, "url": url, "sales": sales,
              "sales_disp": sales_lines(sales),
              "cast_or_full": f["cast"] if f["cast"] else text})
    return f


# ========= 日時ユーティリティ =========
def parse_start_iso(s):
    """'2026/07/20(月) 10:00' -> JST aware isoformat 文字列。失敗時 None。"""
    m = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})\([日月火水木金土]\)\s*(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=JST).isoformat()


def parse_date_only(s):
    m = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})", s or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return datetime(y, mo, d, 23, 59, tzinfo=JST)


# ========= 状態 =========
def default_state():
    return {"initialized": False, "checked": [], "matched_events": {}, "sales": {}}


def load_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for k, v in default_state().items():
                st.setdefault(k, v)
            return st
        except Exception:
            pass
    return default_state()


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ========= 通知 =========
def send_discord(payload):
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=TIMEOUT)
        if r.status_code >= 300:
            print("[warn] Discord応答:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("[warn] Discord通知失敗:", e)
        return False


def log(msg):
    with open("fany_notifications.log", "a", encoding="utf-8") as fp:
        fp.write(msg + "\n" + "-" * 40 + "\n")


def notify_event(ev, matched):
    cast = ev["cast"] or "(出演者情報を取得できませんでした)"
    if len(cast) > 1000:
        cast = cast[:1000] + "…"
    body = (f"「{'／'.join(matched)}」が出演する公演が追加されました。\n\n"
            f"■ 公演タイトル: {ev['title']}\n"
            f"■ 公演日: {ev['date'] or '-'}\n"
            f"■ 場所: {ev['venue'] or '-'}\n"
            f"■ 出演者: {cast}\n"
            f"■ 申し込み日程:\n" + "\n".join("　- " + l for l in ev["sales_disp"]) +
            f"\n\n{ev['url']}")
    embed = {"title": f"🎫 新着公演: {ev['title']}", "url": ev["url"], "color": 0x3D52D5,
             "fields": [
                 {"name": "公演日", "value": ev["date"] or "-", "inline": True},
                 {"name": "場所", "value": (ev["venue"] or "-")[:1024], "inline": True},
                 {"name": "出演者", "value": cast, "inline": False},
                 {"name": "申し込み日程", "value": "\n".join(ev["sales_disp"])[:1024], "inline": False},
                 {"name": "検出キーワード", "value": "／".join(matched), "inline": False}]}
    if not send_discord({"embeds": [embed]}):
        print("\n=== 通知(フォールバック) ===\n" + body + "\n")
    log(body)


def notify_more(events):
    n = len(events)
    lst = "\n".join(f"・{e['title']}（{e['date'] or '日程未定'}） {e['url']}" for e in events)
    content = f"📢 他{n}件の公演が追加されました\n{lst}"
    if not send_discord({"content": content[:1900]}):
        print("\n=== 通知(フォールバック) ===\n" + content + "\n")
    log(content)


def notify_sale_reminder(sale, kind):
    when = "【前日リマインド】明日" if kind == "r1" else "【まもなく】約1時間後"
    emoji = "🔔" if kind == "r1" else "⏰"
    title = f"{emoji} 先着販売リマインド: {sale['title']}"
    body = (f"{when} {sale['start']} に先着販売が開始します。\n\n"
            f"■ 公演: {sale['title']}\n"
            f"■ 公演日: {sale.get('date') or '-'}\n"
            f"■ 場所: {sale.get('venue') or '-'}\n"
            f"■ 出演: {sale.get('matched') or '-'}\n"
            f"■ 先着受付開始: {sale['start']}\n\n{sale['url']}")
    embed = {"title": title, "url": sale["url"], "color": 0xD4880A,
             "fields": [
                 {"name": "先着受付開始", "value": sale["start"], "inline": False},
                 {"name": "公演日", "value": sale.get("date") or "-", "inline": True},
                 {"name": "場所", "value": (sale.get("venue") or "-")[:1024], "inline": True}]}
    if not send_discord({"embeds": [embed]}):
        print("\n=== リマインド(フォールバック) ===\n" + body + "\n")
    log(body)


# ========= scrape（FANY巡回） =========
def register_sales(state, ev, matched):
    """matched公演の『先着』枠を state['sales'] に登録（既存の送信済みフラグは保持）。"""
    now = datetime.now(JST)
    for s in ev["sales"]:
        if s["cat"] != "先着":
            continue
        iso = parse_start_iso(s["start"])
        if not iso:
            continue
        start_dt = datetime.fromisoformat(iso)
        if start_dt < now - timedelta(days=SALE_PRUNE_DAYS):
            continue
        key = f"{ev['id']}|{s['start']}"
        cur = state["sales"].get(key, {})
        state["sales"][key] = {
            "event_id": ev["id"], "title": ev["title"], "venue": ev["venue"],
            "date": ev["date"], "url": ev["url"], "matched": "／".join(matched),
            "start": s["start"], "start_iso": iso,
            "r1_sent": cur.get("r1_sent", False),
            "r2_sent": cur.get("r2_sent", False),
        }


def scrape():
    state = load_state()
    checked = set(state["checked"])
    matched_events = state["matched_events"]        # id -> {..., notified}
    prev_matched_ids = set(matched_events.keys())

    # 検索結果ID収集（出現順）
    ordered_ids, all_ids = [], set()
    for kw in KEYWORDS:
        try:
            for eid in search_event_ids(kw):
                if eid not in all_ids:
                    all_ids.add(eid)
                    ordered_ids.append(eid)
        except Exception as e:
            print(f"[error] 検索失敗 ({kw}):", e)

    now = datetime.now(JST)

    # 取得対象: 新規候補 + 既知matched（先の販売日程を更新するため再取得）
    def is_active_matched(eid):
        info = matched_events.get(eid, {})
        pd = parse_date_only(info.get("date", ""))
        return (pd is None) or (pd >= now - timedelta(days=1))

    to_fetch = []
    for eid in ordered_ids:
        if eid in matched_events:
            if is_active_matched(eid):
                to_fetch.append(eid)
        elif eid in checked:
            continue
        else:
            to_fetch.append(eid)

    newly_matched, fetched = [], 0
    for eid in to_fetch:
        if fetched >= MAX_DETAIL_FETCH:
            break
        try:
            ev = fetch_event(eid)
            fetched += 1
        except Exception as e:
            print(f"[warn] 詳細取得失敗 (id={eid}):", e)
            continue
        matched = [kw for kw in KEYWORDS if kw in ev["cast_or_full"]]
        if matched:
            register_sales(state, ev, matched)
            if eid not in matched_events:
                matched_events[eid] = {"title": ev["title"], "url": ev["url"],
                                       "venue": ev["venue"], "date": ev["date"],
                                       "cast": ev["cast"], "notified": False}
                ev["matched"] = matched
                newly_matched.append(ev)
            else:
                matched_events[eid].update({"title": ev["title"], "venue": ev["venue"],
                                            "date": ev["date"], "cast": ev["cast"]})
        else:
            checked.add(eid)

    # 新着通知（初回はベースライン登録のみ）
    if not state["initialized"]:
        for ev in newly_matched:
            matched_events[ev["id"]]["notified"] = True
        print(f"[init] 現在の該当 {len(newly_matched)} 件、先着枠 {len(state['sales'])} 件をベースライン登録（通知なし）")
    else:
        to_notify = [ev for ev in newly_matched if not matched_events[ev["id"]]["notified"]]
        if len(to_notify) == 1:
            notify_event(to_notify[0], to_notify[0]["matched"])
        elif len(to_notify) > 1:
            notify_event(to_notify[0], to_notify[0]["matched"])
            notify_more(to_notify[1:])
        for ev in to_notify:
            matched_events[ev["id"]]["notified"] = True
        print(f"新着 {len(to_notify)} 件を通知。先着枠 {len(state['sales'])} 件を監視中。")

    # 掃除（終了した公演・古い先着枠）
    for eid in list(matched_events.keys()):
        pd = parse_date_only(matched_events[eid].get("date", ""))
        if pd is not None and pd < now - timedelta(days=1):
            matched_events.pop(eid, None)
    for k in list(state["sales"].keys()):
        sd = datetime.fromisoformat(state["sales"][k]["start_iso"])
        if sd < now - timedelta(days=SALE_PRUNE_DAYS):
            state["sales"].pop(k, None)

    state.update({"initialized": True, "checked": sorted(checked),
                  "matched_events": matched_events})
    save_state(state)


# ========= remind（保存済み先着日程のみ・FANY非アクセス） =========
def evaluate_sale(sale, now):
    start = datetime.fromisoformat(sale["start_iso"])
    r1 = (start - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    r2 = start - timedelta(hours=1)
    send, upd = [], {}
    if not sale.get("r1_sent"):
        if now < r1:
            pass
        elif now < start and (now - r1) <= timedelta(hours=R1_GRACE_H):
            send.append("r1"); upd["r1_sent"] = True
        else:
            upd["r1_sent"] = True          # 猶予超過 → 未送信のまま既読化
    if not sale.get("r2_sent"):
        if now < r2:
            pass
        elif now < start:
            send.append("r2"); upd["r2_sent"] = True
        else:
            upd["r2_sent"] = True          # 開始後 → 未送信のまま既読化
    return send, upd


def remind():
    state = load_state()
    now = datetime.now(JST)
    fired = 0
    for key, sale in list(state["sales"].items()):
        try:
            send, upd = evaluate_sale(sale, now)
        except Exception as e:
            print("[warn] リマインド判定失敗:", key, e)
            continue
        for kind in send:
            notify_sale_reminder(sale, kind)
            fired += 1
        sale.update(upd)
        # 古い枠を掃除
        sd = datetime.fromisoformat(sale["start_iso"])
        if sd < now - timedelta(days=SALE_PRUNE_DAYS):
            state["sales"].pop(key, None)
    save_state(state)
    print(f"[remind] {now.strftime('%Y-%m-%d %H:%M %Z')} 発火 {fired} 件 / 監視 {len(state['sales'])} 件")


# ========= エントリポイント =========
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode == "scrape":
        scrape()
    elif mode == "remind":
        remind()
    else:
        scrape()
        remind()
