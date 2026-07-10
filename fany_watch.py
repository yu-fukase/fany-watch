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
  - 検索結果ページ（/search/event）に公演の全情報（出演者・受付期間）が載っているため、
    そこだけをパースする。詳細ページへの個別アクセスは行わない（ブロック回避＆高速）。
  - 公演の一意キーは reception リンク末尾の「公演ID」。
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

# 曜日カッコ内に (日・祝) 等の表記も許容
DOW = r"[日月火水木金土](?:・[^)）]{0,4})?"
DATETIME = r"20\d{2}/\d{1,2}/\d{1,2}\(" + DOW + r"\)\s*\d{1,2}:\d{2}"
RECEPT_RE = re.compile(
    r"受付期間[：:]\s*(" + DATETIME + r")\s*[～〜\-–]\s*(" + DATETIME + r")")
HEAD_RE = re.compile(
    r"^(20\d{2}/\d{1,2}/\d{1,2}\(" + DOW + r"\))"
    r"(?:開場\s*\d{1,2}:\d{2})?\s*(?:開演\s*\d{1,2}:\d{2})?\s*(.*)$")
PREF_RE = re.compile(r"（(北海道|東京都|大阪府|京都府|.{2,4}県)）\s*$")


# ========= 取得 =========
def get(url, params=None):
    r = session.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    time.sleep(REQUEST_INTERVAL)
    return r.text


# ========= 解析（検索結果ページを丸ごとパース） =========
def parse_search_html(html):
    """検索結果ページHTMLを公演単位にパースして dict のリストを返す。
    公演ID(reception/eventリンク末尾)をキーに、日時/公演名/都道府県/出演者/受付枠をまとめる。
    """
    soup = BeautifulSoup(html, "html.parser")
    events = {}

    def ev(eid):
        return events.setdefault(eid, {
            "id": eid, "url": f"{BASE}/event/{eid}",
            "date": "", "title_venue": "", "pref": "", "cast": "", "sales": []})

    # 1) 見出しリンク /event/<id> から 日時 / 公演名(会場込み) / 都道府県
    for a in soup.find_all("a", href=re.compile(r"/event/\d+")):
        m = re.search(r"/event/(\d+)", a["href"])
        if not m:
            continue
        eid = m.group(1)
        head = a.get_text(" ", strip=True)
        e = ev(eid)
        hm = HEAD_RE.match(head)
        if hm:
            e["date"] = hm.group(1)
            rest = hm.group(2).strip()
        else:
            rest = head
        if rest:
            e["title_venue"] = rest
        pm = PREF_RE.search(rest)
        if pm:
            e["pref"] = pm.group(1)

    # 2) reception リンクから 受付期間（先着/抽選）を抽出。公演IDでひも付け
    for a in soup.find_all("a", href=re.compile(r"/reception/\d+/\d+")):
        m = re.search(r"/reception/\d+/(\d+)", a["href"])
        if not m:
            continue
        eid = m.group(1)
        e = ev(eid)
        parent = a.find_parent(["li", "div", "p"])
        context = parent.get_text(" ", strip=True) if parent else ""
        atext = a.get_text(" ", strip=True)
        rm = RECEPT_RE.search(context) or RECEPT_RE.search(atext)
        if not rm:
            continue
        cat = "抽選" if "抽選" in (context or atext) else "先着"
        key = (cat, rm.group(1), rm.group(2))
        if key not in {(s["cat"], s["start"], s["end"]) for s in e["sales"]}:
            e["sales"].append({"cat": cat, "start": rm.group(1), "end": rm.group(2)})

    # 3) 出演者テキスト（"出演" ラベルを含むブロック）
    for lab in soup.find_all(string=re.compile(r"出演")):
        block = lab.find_parent(["div", "section", "article", "li"])
        if not block:
            continue
        link = block.find("a", href=re.compile(r"/event/\d+"))
        if not link:
            p = block.find_parent()
            link = p.find("a", href=re.compile(r"/event/\d+")) if p else None
        if not link:
            continue
        eid = re.search(r"/event/(\d+)", link["href"]).group(1)
        if eid not in events:
            continue
        cm = re.search(r"出演[者]?[：:]?\s*(.+)", block.get_text(" ", strip=True))
        if cm:
            events[eid]["cast"] = cm.group(1).strip()

    # 4) マッチ判定用テキスト（出演者＋公演名）
    for e in events.values():
        e["search_text"] = " ".join([e.get("cast", ""), e.get("title_venue", "")])
        e["sales_disp"] = sales_lines(e["sales"])

    return list(events.values())


def sales_lines(sales):
    lines = []
    for s in sales:
        if s["cat"] == "抽選":
            lines.append(f"抽選受付: {s['start']} 〜 {s['end']}")
        else:
            lines.append(f"先着受付開始: {s['start']}")
    return lines or ["（受付情報は公演ページをご確認ください）"]


def search_events(keyword):
    """キーワードで検索し、パース済み公演リストを返す。"""
    html = get(SEARCH, params={"keywords": keyword, "search_type": "search_string"})
    return parse_search_html(html)


# ========= 日時ユーティリティ =========
def parse_start_iso(s):
    m = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})\(" + DOW + r"\)\s*(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
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
    venue = ev.get("title_venue") or "-"
    body = (f"「{'／'.join(matched)}」が出演する公演が追加されました。\n\n"
            f"■ 公演: {venue}\n"
            f"■ 公演日: {ev.get('date') or '-'}\n"
            f"■ 出演者: {cast}\n"
            f"■ 申し込み日程:\n" + "\n".join("　- " + l for l in ev["sales_disp"]) +
            f"\n\n{ev['url']}")
    embed = {"title": f"🎫 新着公演: {venue[:200]}", "url": ev["url"], "color": 0x3D52D5,
             "fields": [
                 {"name": "公演日", "value": ev.get("date") or "-", "inline": True},
                 {"name": "都道府県", "value": ev.get("pref") or "-", "inline": True},
                 {"name": "出演者", "value": cast, "inline": False},
                 {"name": "申し込み日程", "value": "\n".join(ev["sales_disp"])[:1024], "inline": False},
                 {"name": "検出キーワード", "value": "／".join(matched), "inline": False}]}
    if not send_discord({"embeds": [embed]}):
        print("\n=== 通知(フォールバック) ===\n" + body + "\n")
    log(body)


def notify_more(events):
    n = len(events)
    lst = "\n".join(f"・{e.get('title_venue') or e['id']}（{e.get('date') or '日程未定'}） {e['url']}"
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
    venue = sale.get("title_venue") or sale.get("title") or "-"
    title = f"{emoji} 先着販売リマインド: {venue[:180]}"
    body = (f"{when} {sale['start']} に先着販売が開始します。\n\n"
            f"■ 公演: {venue}\n"
            f"■ 公演日: {sale.get('date') or '-'}\n"
            f"■ 出演: {sale.get('matched') or '-'}\n"
            f"■ 先着受付開始: {sale['start']}\n\n{sale['url']}")
    embed = {"title": title, "url": sale["url"], "color": 0xD4880A,
             "fields": [
                 {"name": "先着受付開始", "value": sale["start"], "inline": False},
                 {"name": "公演日", "value": sale.get("date") or "-", "inline": True},
                 {"name": "都道府県", "value": sale.get("pref") or "-", "inline": True}]}
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
            "event_id": ev["id"], "title_venue": ev.get("title_venue", ""),
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
                cur = by_id.get(ev["id"])
                if cur is None:
                    by_id[ev["id"]] = ev
                else:
                    # 情報を補完
                    for f in ("date", "title_venue", "pref", "cast"):
                        if not cur.get(f) and ev.get(f):
                            cur[f] = ev[f]
                    if ev["sales"]:
                        seen = {(s["cat"], s["start"], s["end"]) for s in cur["sales"]}
                        for s in ev["sales"]:
                            if (s["cat"], s["start"], s["end"]) not in seen:
                                cur["sales"].append(s)
                        cur["sales_disp"] = sales_lines(cur["sales"])
        except Exception as e:
            print(f"[error] 検索失敗 ({kw}):", e)

    now = datetime.now(JST)

    # キーワード一致する公演だけ残す（出演者 or 公演名にヒット）
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
                "title_venue": ev.get("title_venue", ""), "url": ev["url"],
                "pref": ev.get("pref", ""), "date": ev.get("date", ""),
                "cast": ev.get("cast", ""), "notified": False}
            newly.append(ev)
        else:
            rec.update({"title_venue": ev.get("title_venue", ""),
                        "pref": ev.get("pref", ""), "date": ev.get("date", ""),
                        "cast": ev.get("cast", "")})

    if not state["initialized"]:
        # 初回はベースライン登録のみ（通知しない）
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
            # 追加なし
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
