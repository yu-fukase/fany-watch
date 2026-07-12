#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FANYチケット 出演者ウォッチャー（新着通知 + 先着リマインド）
============================================================
「ドンデコルテ」または「渡辺銀次」または「CITY」または「素敵じゃないか」が
"出演"欄（または公演名）に含まれる公演を監視し、Discordに通知する。

モード（FANYへのアクセスは scrape のみ・2時間おき想定）:
  python fany_watch.py scrape   … FANYを巡回。新着公演を通知し、先着販売の日程を保存
  python fany_watch.py remind   … FANYには触れず、保存済みの先着日程だけを見てリマインド送信
  python fany_watch.py both     … 両方（既定）

仕組みの要点:
  - 検索結果ページ（/search/event）を BeautifulSoup でパースする。
    公演ブロックは class="fany_performanceListBox__outline"。
      日付   : .fany_performanceListBox__headerPerformanceDate
      タイトル: .fany_performanceListBox__headerTitle
      会場   : .fany_performanceListBox__headerVenue
      出演   : <dt>出演</dt> の隣の <dd>（p.preview_block）
      受付   : .fany_g-ticketInfo（.fany_g-ticket_lottery が付けば抽選、無ければ先着）
  - 公演の一意キーは reception リンク末尾の「公演ID」。
  - 詳細ページへの個別アクセスは行わない（ブロック回避＆高速）。
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
KEYWORDS = ["ドンデコルテ", "渡辺銀次", "CITY", "素敵じゃないか"]
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
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")
SALE_PRUNE_DAYS = 2            # 販売開始からこの日数を過ぎた先着枠は掃除
R1_GRACE_H = 4                # 前日22:00リマインドの発火猶予(時間)。過ぎたら未送信のまま既読化
# 「追加なし」通知を毎回送るか。True=毎回送る / False=送らない
NOTIFY_WHEN_NO_NEW = True

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})

# 曜日カッコ内に (日・祝) 等の表記、get_text由来のカッコ内スペースも許容
DOW = r"\s*[日月火水木金土](?:・[^)）]{0,4})?\s*"
DATETIME = r"20\d{2}/\d{1,2}/\d{1,2}\(" + DOW + r"\)\s*\d{1,2}:\d{2}"
RECEPT_RE = re.compile(
    r"受付期間[：:]\s*(" + DATETIME + r")\s*[～〜\-–]\s*(" + DATETIME + r")")
DATE_HEAD_RE = re.compile(r"(20\d{2}/\d{1,2}/\d{1,2}\(" + DOW + r"\))")


# ========= 取得 =========
def get(url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    time.sleep(REQUEST_INTERVAL)
    return r.text


# ========= 解析（検索結果ページを丸ごとパース） =========
def _txt(el):
    return el.get_text(" ", strip=True) if el else ""


def _norm_dt(s):
    """'2026/07/12( 日 ) 11:00' -> '2026/07/12(日)11:00' に正規化。"""
    return re.sub(r"\(\s*([^)]*?)\s*\)", r"(\1)", s).replace(" ", "")


def sales_lines(sales):
    lines = []
    for s in sales:
        if s["cat"] == "抽選":
            lines.append(f"抽選受付: {s['start']} 〜 {s['end']}")
        else:
            lines.append(f"先着受付開始: {s['start']}")
    return lines or ["（受付情報は公演ページをご確認ください）"]


def parse_search_html(html):
    """検索結果ページHTMLを公演単位にパースして dict のリストを返す。"""
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for box in soup.find_all(class_="fany_performanceListBox__outline"):
        # 日付
        date_el = box.find(class_="fany_performanceListBox__headerPerformanceDate")
        date_raw = _txt(date_el)
        dm = DATE_HEAD_RE.search(date_raw)
        date = _norm_dt(dm.group(1)) if dm else _norm_dt(date_raw)

        # タイトル・会場
        title = _txt(box.find(class_="fany_performanceListBox__headerTitle"))
        venue = _txt(box.find(class_="fany_performanceListBox__headerVenue"))
        pref = ""
        pm = re.search(r"（([^）]+?)）\s*$", venue)
        if pm:
            pref = pm.group(1)

        # 出演者: <dt>出演</dt> の隣の <dd>
        cast = ""
        for dt in box.find_all("dt"):
            if "出演" in _txt(dt):
                dd = dt.find_next("dd")
                if dd:
                    cast = _txt(dd)
                break
        if not cast:
            pb = box.find("p", class_="preview_block")
            if pb:
                cast = _txt(pb)

        # 公演ID: reception リンク末尾
        eid = ""
        a0 = box.find("a", href=re.compile(r"/reception/\d+/\d+"))
        if a0:
            m = re.search(r"/reception/\d+/(\d+)", a0["href"])
            if m:
                eid = m.group(1)

        # 受付枠
        sales, seen = [], set()
        for tinfo in box.find_all(class_=re.compile(r"fany_g-ticketInfo\b")):
            cls = " ".join(tinfo.get("class", []))
            context = _txt(tinfo)
            rm = RECEPT_RE.search(context)
            if not rm:
                continue
            cat = "抽選" if ("lottery" in cls or "抽選" in context) else "先着"
            start, end = _norm_dt(rm.group(1)), _norm_dt(rm.group(2))
            key = (cat, start, end)
            if key in seen:
                continue
            seen.add(key)
            sales.append({"cat": cat, "start": start, "end": end})

        events.append({
            "id": eid, "url": f"{BASE}/event/{eid}" if eid else BASE,
            "date": date, "title": title, "venue": venue, "pref": pref,
            "title_venue": (title + (" " + venue if venue else "")).strip(),
            "cast": cast, "sales": sales,
            "sales_disp": sales_lines(sales),
            "search_text": " ".join([cast, title, venue]),
        })
    return events


def search_events(keyword):
    """キーワードで検索し、パース済み公演リストを返す。"""
    html = get(SEARCH, params={"keywords": keyword, "search_type": "search_string"})
    return parse_search_html(html)


# ========= 日時ユーティリティ =========
def parse_start_iso(s):
    """'2026/07/20(月)10:00' や '... 10:00' -> JST aware isoformat 文字列。失敗時 None。"""
    m = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})\(\s*[日月火水木金土][^)]*\)\s*(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    y, mo, d, h, mi = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                       int(m.group(4)), int(m.group(5)))
    return datetime(y, mo, d, h, mi, tzinfo=JST).isoformat()


def parse_date_only(s):
    m = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})", s or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    return datetime(y, mo, d, 23, 59, tzinfo=JST)


# ========= 状態 =========
def default_state():
    return {"initialized": False, "matched_events": {}, "sales": {}}


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


def notify_event(ev):
    matched = ev["matched"]
    cast = ev.get("cast") or ev.get("title_venue") or "(出演者情報を取得できませんでした)"
    if len(cast) > 1000:
        cast = cast[:1000] + "…"
    title = ev.get("title") or ev.get("title_venue") or "-"
    body = (f"「{'／'.join(matched)}」が出演する公演が追加されました。\n\n"
            f"■ 公演タイトル: {title}\n"
            f"■ 公演日: {ev.get('date') or '-'}\n"
            f"■ 場所: {ev.get('venue') or '-'}\n"
            f"■ 出演者: {cast}\n"
            f"■ 申し込み日程:\n" + "\n".join("　- " + l for l in ev["sales_disp"]) +
            f"\n\n{ev['url']}")
    embed = {"title": f"🎫 新着公演: {title[:200]}", "url": ev["url"], "color": 0x3D52D5,
             "fields": [
                 {"name": "公演日", "value": ev.get("date") or "-", "inline": True},
                 {"name": "場所", "value": (ev.get("venue") or "-")[:1024], "inline": True},
                 {"name": "出演者", "value": cast, "inline": False},
                 {"name": "申し込み日程", "value": "\n".join(ev["sales_disp"])[:1024], "inline": False},
                 {"name": "検出キーワード", "value": "／".join(matched), "inline": False}]}
    if not send_discord({"embeds": [embed]}):
        print("\n=== 通知(フォールバック) ===\n" + body + "\n")
    log(body)


def notify_more(events):
    n = len(events)
    lst = "\n".join(f"・{e.get('title') or e['id']}（{e.get('date') or '日程未定'}） {e['url']}"
                    for e in events)
    content = f"📢 他{n}件の公演が追加されました\n{lst}"
    if not send_discord({"content": content[:1900]}):
        print("\n=== 通知(フォールバック) ===\n" + content + "\n")
    log(content)


def notify_no_new():
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    content = f"✅ 追加の公演はありませんでした（{now} 時点）"
    if not send_discord({"content": content}):
        print("\n=== 通知(フォールバック) ===\n" + content + "\n")
    log(content)


def notify_sale_reminder(sale, kind):
    when = "【前日リマインド】明日" if kind == "r1" else "【まもなく】約1時間後"
    emoji = "🔔" if kind == "r1" else "⏰"
    title = sale.get("title") or sale.get("title_venue") or "-"
    head = f"{emoji} 先着販売リマインド: {title[:180]}"
    body = (f"{when} {sale['start']} に先着販売が開始します。\n\n"
            f"■ 公演: {title}\n"
            f"■ 公演日: {sale.get('date') or '-'}\n"
            f"■ 場所: {sale.get('venue') or '-'}\n"
            f"■ 出演: {sale.get('matched') or '-'}\n"
            f"■ 先着受付開始: {sale['start']}\n\n{sale['url']}")
    embed = {"title": head, "url": sale["url"], "color": 0xD4880A,
             "fields": [
                 {"name": "先着受付開始", "value": sale["start"], "inline": False},
                 {"name": "公演日", "value": sale.get("date") or "-", "inline": True},
                 {"name": "場所", "value": (sale.get("venue") or "-")[:1024], "inline": True}]}
    if not send_discord({"embeds": [embed]}):
        print("\n=== リマインド(フォールバック) ===\n" + body + "\n")
    log(body)


# ========= scrape（FANY巡回） =========
def register_sales(state, ev, matched):
    now = datetime.now(JST)
    for s in ev["sales"]:
        if s["cat"] != "先着":
            continue
        iso = parse_start_iso(s["start"])
        if not iso:
            continue
        if datetime.fromisoformat(iso) < now - timedelta(days=SALE_PRUNE_DAYS):
            continue
        key = f"{ev['id']}|{s['start']}"
        cur = state["sales"].get(key, {})
        state["sales"][key] = {
            "event_id": ev["id"], "title": ev.get("title", ""),
            "title_venue": ev.get("title_venue", ""), "venue": ev.get("venue", ""),
            "pref": ev.get("pref", ""), "date": ev.get("date", ""), "url": ev["url"],
            "matched": "／".join(matched), "start": s["start"], "start_iso": iso,
            "r1_sent": cur.get("r1_sent", False),
            "r2_sent": cur.get("r2_sent", False),
        }


def scrape():
    state = load_state()
    matched_events = state["matched_events"]

    # 全キーワードで検索し、公演IDでマージ
    by_id = {}
    for kw in KEYWORDS:
        try:
            for ev in search_events(kw):
                if not ev["id"]:
                    continue
                cur = by_id.get(ev["id"])
                if cur is None:
                    by_id[ev["id"]] = ev
                else:
                    for f in ("date", "title", "venue", "pref", "cast"):
                        if not cur.get(f) and ev.get(f):
                            cur[f] = ev[f]
                    if ev["sales"]:
                        seen = {(s["cat"], s["start"], s["end"]) for s in cur["sales"]}
                        for s in ev["sales"]:
                            if (s["cat"], s["start"], s["end"]) not in seen:
                                cur["sales"].append(s)
                        cur["sales_disp"] = sales_lines(cur["sales"])
                    cur["search_text"] = " ".join([cur.get("cast", ""),
                                                   cur.get("title", ""),
                                                   cur.get("venue", "")])
        except Exception as e:
            print(f"[error] 検索失敗 ({kw}):", e)

    now = datetime.now(JST)

    # キーワード一致（出演者 or 公演名 or 会場）
    matched_list = []
    for ev in by_id.values():
        matched = [k for k in KEYWORDS if k in ev.get("search_text", "")]
        if matched:
            ev["matched"] = matched
            matched_list.append(ev)

    newly = []
    for ev in matched_list:
        register_sales(state, ev, ev["matched"])
        rec = matched_events.get(ev["id"])
        if rec is None:
            matched_events[ev["id"]] = {
                "title": ev.get("title", ""), "url": ev["url"],
                "venue": ev.get("venue", ""), "pref": ev.get("pref", ""),
                "date": ev.get("date", ""), "cast": ev.get("cast", ""),
                "notified": False}
            newly.append(ev)
        else:
            rec.update({"title": ev.get("title", ""), "venue": ev.get("venue", ""),
                        "pref": ev.get("pref", ""), "date": ev.get("date", ""),
                        "cast": ev.get("cast", "")})

    if not state["initialized"]:
        for ev in newly:
            matched_events[ev["id"]]["notified"] = True
        print(f"[init] 現在の該当 {len(newly)} 件、先着枠 {len(state['sales'])} 件を"
              f"ベースライン登録（通知なし）")
    else:
        to_notify = [ev for ev in newly if not matched_events[ev["id"]]["notified"]]
        if len(to_notify) == 1:
            notify_event(to_notify[0])
        elif len(to_notify) > 1:
            notify_event(to_notify[0])
            notify_more(to_notify[1:])
        else:
            if NOTIFY_WHEN_NO_NEW:
                notify_no_new()
        for ev in to_notify:
            matched_events[ev["id"]]["notified"] = True
        print(f"新着 {len(to_notify)} 件を通知。先着枠 {len(state['sales'])} 件を監視中。")

    # 掃除
    for eid in list(matched_events.keys()):
        pd = parse_date_only(matched_events[eid].get("date", ""))
        if pd is not None and pd < now - timedelta(days=1):
            matched_events.pop(eid, None)
    for k in list(state["sales"].keys()):
        sd = datetime.fromisoformat(state["sales"][k]["start_iso"])
        if sd < now - timedelta(days=SALE_PRUNE_DAYS):
            state["sales"].pop(k, None)

    state.update({"initialized": True, "matched_events": matched_events})
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
            upd["r1_sent"] = True
    if not sale.get("r2_sent"):
        if now < r2:
            pass
        elif now < start:
            send.append("r2"); upd["r2_sent"] = True
        else:
            upd["r2_sent"] = True
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
        sd = datetime.fromisoformat(sale["start_iso"])
        if sd < now - timedelta(days=SALE_PRUNE_DAYS):
            state["sales"].pop(key, None)
    save_state(state)
    print(f"[remind] {now.strftime('%Y-%m-%d %H:%M %Z')} 発火 {fired} 件 / "
          f"監視 {len(state['sales'])} 件")


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
