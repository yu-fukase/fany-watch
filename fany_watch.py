#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FANYチケット 出演者ウォッチャー（新着通知 + 先着リマインド）
============================================================
「ドンデコルテ」または「渡辺銀次」または「CITY」または「素敵じゃないか」が
"出演"欄（または公演名・会場名）に含まれる公演を監視し、Discordに通知する。

モード（FANYへのアクセスは scrape のみ・2時間おき想定）:
  python fany_watch.py scrape   … FANYを巡回。新着公演を通知し、先着販売の日程を保存
  python fany_watch.py remind   … FANYには触れず、保存済みの先着日程だけを見てリマインド送信
  python fany_watch.py both     … 両方（既定）

取得方式（重要）:
  FANYの検索は無限スクロール型で、HTMLには先頭10件しか含まれない。
  実データは JSON API  GET /search/event_more?keywords=..&search_type=search_string&offset=N
  から取得できる（レスポンスは {"performances":[...], "load_count":N}）。
  offset を 0,10,20,... と増やし load_count が尽きるまで呼ぶことで全件取得する。
  各公演の performer_detail（全出演者）・performance_sales（受付情報）を直接読むため、
  HTMLスクレイピングは行わない（省略表示や曜日カッコ等の問題が原理的に発生しない）。
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

# ========= 設定 =========
KEYWORDS = ["ドンデコルテ", "渡辺銀次", "CITY", "素敵じゃないか"]
BASE = "https://ticket.fany.lol"
SEARCH_MORE = BASE + "/search/event_more"
JST = ZoneInfo("Asia/Tokyo")

# 通知先Discord Webhook（環境変数があれば優先）。
# ※このURLは秘密情報です。公開リポジトリに置くと第三者が悪用できます。
#   公開運用ではSecretに入れて下の既定値は空にし、漏れたらDiscordで再生成してください。
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or \
    ""

STATE_FILE = Path(os.environ.get("FANY_STATE_FILE", "fany_seen.json"))
REQUEST_INTERVAL = 1.5         # 各APIリクエスト後の待機秒（FANYへの負荷軽減）
TIMEOUT = 20
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")
PAGE_STEP = 10                 # offset の増分（1ページ=10件）
MAX_PAGES = 60                 # 安全上限（暴走防止。10*60=600件まで）
SALE_PRUNE_DAYS = 2            # 販売開始からこの日数を過ぎた先着枠は掃除
R1_GRACE_H = 4                # 前日22:00リマインドの発火猶予(時間)。過ぎたら未送信のまま既読化
# 「追加なし」通知を毎回送るか。True=毎回送る / False=送らない
NOTIFY_WHEN_NO_NEW = True

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT,
                        "Accept-Language": "ja,en;q=0.8",
                        "X-Requested-With": "XMLHttpRequest"})


# ========= 取得 =========
def get_json(url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    time.sleep(REQUEST_INTERVAL)
    return r.json()


def fetch_all_performances(keyword):
    """event_more を offset ページングして全公演(生JSON dict)を返す。"""
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        try:
            data = get_json(SEARCH_MORE, params={
                "keywords": keyword, "search_type": "search_string",
                "offset": offset})
        except Exception as e:
            print(f"[warn] 取得失敗 (kw={keyword}, offset={offset}):", e)
            break
        perfs = data.get("performances", []) or []
        load = data.get("load_count", len(perfs))
        out.extend(perfs)
        if not perfs or load < PAGE_STEP:
            break                      # 最終ページ
        offset += PAGE_STEP
    return out


# ========= 解析（JSONフィールドを読むだけ） =========
def _strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _raw_to_iso(raw):
    """'20260520100000' -> JST isoformat 文字列。失敗時 None。"""
    if not raw or not re.fullmatch(r"\d{14}", str(raw)):
        return None
    raw = str(raw)
    y, mo, d = int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
    h, mi, s = int(raw[8:10]), int(raw[10:12]), int(raw[12:14])
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=JST).isoformat()
    except ValueError:
        return None


def _raw_to_disp(raw):
    iso = _raw_to_iso(raw)
    if not iso:
        return ""
    return datetime.fromisoformat(iso).strftime("%Y/%m/%d %H:%M")


def sales_lines(sales):
    lines = []
    for s in sales:
        st = f"（{s['status']}）" if s.get("status") else ""
        if s["cat"] == "抽選":
            nm = f"[{s['name']}]" if s.get("name") else ""
            lines.append(f"抽選受付{nm}: {s['start']} 〜 {s['end']}{st}")
        else:
            lines.append(f"先着受付開始: {s['start']}{st}")
    return lines or ["（受付情報は公演ページをご確認ください）"]


def parse_performance(p):
    """event_more の1公演dictを、内部形式のdictに変換。"""
    eid = str(p.get("id", "") or "")
    name = p.get("name", "") or ""
    venue = _strip_tags(p.get("venue_name", ""))
    date = _strip_tags(p.get("performance_date", ""))
    cast = p.get("performer_detail", "") or ""
    pref = ""
    m = re.search(r"（([^）]+?)）\s*$", venue)
    if m:
        pref = m.group(1)

    sales = []
    for s in (p.get("performance_sales", []) or []):
        sname = s.get("sales_name", "") or ""
        status = s.get("display_sales_status", "") or ""
        cat = "抽選" if ("抽選" in sname or "抽選" in status) else "先着"
        start_raw = s.get("sales_start_datetime_raw", "")
        end_raw = s.get("sales_end_datetime_raw", "")
        sales.append({
            "cat": cat, "name": sname, "status": status,
            "start": _raw_to_disp(start_raw), "start_iso": _raw_to_iso(start_raw),
            "end": _raw_to_disp(end_raw),
            "url": s.get("destination_url", "") or "",
        })

    return {
        "id": eid, "url": f"{BASE}/event/{eid}" if eid else BASE,
        "name": name, "title": name, "venue": venue, "pref": pref,
        "date": date, "cast": cast, "sales": sales,
        "sales_disp": sales_lines(sales),
        "title_venue": (name + (" " + venue if venue else "")).strip(),
        "search_text": " ".join([cast, name, venue]),
    }


def search_events(keyword):
    """キーワードで全件取得し、パース済み公演リストを返す。"""
    return [parse_performance(p) for p in fetch_all_performances(keyword)]


# ========= 日時ユーティリティ =========
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
    cast = ev.get("cast") or "(出演者情報を取得できませんでした)"
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
    when = "【前日リマインド】明日" if kind == "r1" else "【本日】まもなく"
    emoji = "🔔" if kind == "r1" else "⏰"
    title = sale.get("title") or sale.get("title_venue") or "-"
    head = f"{emoji} 先着販売リマインド: {title[:180]}"
    url = sale.get("sale_url") or sale.get("url")
    body = (f"{when} {sale['start']} に先着販売が開始します。\n\n"
            f"■ 公演: {title}\n"
            f"■ 公演日: {sale.get('date') or '-'}\n"
            f"■ 場所: {sale.get('venue') or '-'}\n"
            f"■ 出演: {sale.get('matched') or '-'}\n"
            f"■ 先着受付開始: {sale['start']}\n\n{url}")
    embed = {"title": head, "url": url, "color": 0xD4880A,
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
        iso = s.get("start_iso")
        if not iso:
            continue
        if datetime.fromisoformat(iso) < now - timedelta(days=SALE_PRUNE_DAYS):
            continue
        key = f"{ev['id']}|{iso}"
        cur = state["sales"].get(key, {})
        state["sales"][key] = {
            "event_id": ev["id"], "title": ev.get("title", ""),
            "title_venue": ev.get("title_venue", ""), "venue": ev.get("venue", ""),
            "pref": ev.get("pref", ""), "date": ev.get("date", ""),
            "url": ev["url"], "sale_url": s.get("url") or ev["url"],
            "matched": "／".join(matched), "start": s["start"], "start_iso": iso,
            "r1_sent": cur.get("r1_sent", False),
            "r2_sent": cur.get("r2_sent", False),
        }


def scrape():
    state = load_state()
    matched_events = state["matched_events"]

    # 全キーワードで全件取得し、公演IDでマージ
    by_id = {}
    for kw in KEYWORDS:
        for ev in search_events(kw):
            if not ev["id"]:
                continue
            cur = by_id.get(ev["id"])
            if cur is None:
                by_id[ev["id"]] = ev
            else:
                # 情報補完（通常は同一だが念のため）
                for f in ("date", "title", "venue", "pref", "cast"):
                    if not cur.get(f) and ev.get(f):
                        cur[f] = ev[f]
                if ev["sales"] and not cur["sales"]:
                    cur["sales"] = ev["sales"]
                    cur["sales_disp"] = ev["sales_disp"]

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
        print(f"新着 {len(to_notify)} 件を通知。該当 {len(matched_list)} 件 / "
              f"先着枠 {len(state['sales'])} 件を監視中。")

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
    """r1=販売開始【前日22:00】頃 / r2=販売開始【当日9:00】頃 に発火。
    どちらも数分の遅延を許容するため、指定時刻を過ぎたら販売開始までの間に一度だけ送る。
    """
    start = datetime.fromisoformat(sale["start_iso"])
    r1 = (start - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    r2 = start.replace(hour=9, minute=0, second=0, microsecond=0)  # 販売開始"当日"の9:00
    send, upd = [], {}

    # r1: 前日22:00頃（発火猶予 R1_GRACE_H 時間以内、かつ販売開始前）
    if not sale.get("r1_sent"):
        if now < r1:
            pass
        elif now < start and (now - r1) <= timedelta(hours=R1_GRACE_H):
            send.append("r1"); upd["r1_sent"] = True
        else:
            upd["r1_sent"] = True          # 猶予超過 or 販売開始後 → 未送信のまま既読化

    # r2: 当日9:00頃（発火猶予 R1_GRACE_H 時間以内、かつ販売開始前）
    # ※9:00より前に販売開始する公演では r2 が販売開始後になるため、その場合は送らず既読化
    if not sale.get("r2_sent"):
        if now < r2:
            pass
        elif now < start and (now - r2) <= timedelta(hours=R1_GRACE_H):
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
