import os
import time
import requests
import schedule
from datetime import datetime, timezone
import json

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

sent_slugs = set()
last_reset_day = None
trades_today = 0
MAX_TRADES = 8

# ── Utils ─────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("No Telegram config")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        data = r.json()
        if data.get("ok"):
            print(f"Sent at {now_str()}")
            return True
        else:
            print(f"TG error: {data.get('description')}")
            return False
    except Exception as e:
        print(f"Exception: {e}")
        return False

def reset_daily():
    global sent_slugs, last_reset_day, trades_today
    today = datetime.now(timezone.utc).date()
    if last_reset_day != today:
        sent_slugs = set()
        trades_today = 0
        last_reset_day = today
        print(f"Daily reset — {today}")

# ── Score market ──────────────────────────────────────────────────────────────
def score_market(m):
    try:
        prices = m.get("outcomePrices")
        if not prices:
            return None
        p = json.loads(prices) if isinstance(prices, str) else prices
        yes = float(p[0])
        no  = float(p[1])
        edge = abs(yes - 0.5)

        # Need at least slight edge (55c+)
        if edge < 0.05:
            return None

        # Volume
        vol = float(m.get("volume") or 0) + float(m.get("volume24hr") or 0)
        if vol < 200:
            return None

        # Time — be very flexible, just needs to be active
        end_iso = m.get("endDateIso") or m.get("endDate")
        hours_left = 12  # default if no end date
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                hours_left = (end_dt - now_dt).total_seconds() / 3600
                # Skip if already ended or more than 7 days away
                if hours_left < 0 or hours_left > 168:
                    return None
            except:
                pass

        vol_score = min(vol / 20000, 1.0)
        time_score = 1.0 if hours_left <= 24 else (0.7 if hours_left <= 72 else 0.4)
        score = (edge * 0.6) + (vol_score * 0.25) + (time_score * 0.15)

        call = "YES" if yes >= 0.5 else "NO"
        call_odds = yes if call == "YES" else no
        confidence = min(int(40 + edge * 110 + vol_score * 20), 93)

        return {
            "slug":       m.get("slug", ""),
            "question":   m.get("question") or m.get("title") or "Unknown",
            "category":   m.get("category") or m.get("tag") or "General",
            "url":        f"https://polymarket.com/event/{m.get('slug','')}",
            "yes": yes, "no": no,
            "edge": edge, "score": score,
            "call": call, "call_odds": call_odds,
            "confidence": confidence,
            "hours_left": hours_left,
            "volume": vol,
        }
    except Exception as e:
        print(f"Score error: {e}")
        return None

# ── Fetch markets ─────────────────────────────────────────────────────────────
def fetch_top_markets():
    endpoints = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=liquidity&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100",
    ]
    scored = []
    seen = set()
    total_checked = 0
    for url in endpoints:
        try:
            r = requests.get(url, timeout=15)
            if not r.ok:
                print(f"Bad response from {url}: {r.status_code}")
                continue
            markets = r.json()
            if not isinstance(markets, list):
                print(f"Unexpected response type: {type(markets)}")
                continue
            print(f"Got {len(markets)} markets from endpoint")
            for m in markets:
                slug = m.get("slug")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                total_checked += 1
                result = score_market(m)
                if result:
                    scored.append(result)
        except Exception as e:
            print(f"Fetch error: {e}")

    print(f"Checked {total_checked} markets, found {len(scored)} with edge")
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

# ── Build message ─────────────────────────────────────────────────────────────
def build_message(m, trade_num):
    emoji = "🟢" if m["call"] == "YES" else "🔴"
    h = m["hours_left"]
    time_str = f"{round(h*60)}min" if h < 1 else f"{round(h)}h"
    vol_str = f"${round(m['volume']/1000)}K" if m["volume"] > 1000 else f"${round(m['volume'])}"

    return "\n".join([
        f"🎯 POLYMARKET SIGNAL #{trade_num}",
        "",
        f"{emoji} BET {m['call']}",
        "",
        f"📋 {m['question']}",
        "",
        f"💰 YES {round(m['yes']*100)}¢  /  NO {round(m['no']*100)}¢",
        f"🎯 Call: {m['call']} @ {round(m['call_odds']*100)}¢",
        f"📊 Confidence: {m['confidence']}%",
        f"⏱ Resolves in: ~{time_str}",
        f"💵 Volume: {vol_str}",
        f"🗂 Category: {m['category']}",
        "",
        f"🕐 {now_str()}",
        "",
        f"🔗 {m['url']}",
    ])

# ── Scan ──────────────────────────────────────────────────────────────────────
def scan():
    global trades_today
    reset_daily()

    if trades_today >= MAX_TRADES:
        print(f"Max {MAX_TRADES} trades reached for today.")
        return

    print(f"[{now_str()}] Scanning... ({trades_today}/{MAX_TRADES} trades today)")
    markets = fetch_top_markets()

    if not markets:
        print("Zero markets scored — API may be down or all near 50/50")
        send_telegram(f"⚠️ Bot scanned but found 0 markets with edge at {now_str()}. Will retry in 2hrs.")
        return

    # Pick best unsent market
    best = None
    for m in markets:
        if m["slug"] not in sent_slugs:
            best = m
            break

    if not best:
        print("All good markets already sent today.")
        return

    print(f"Sending: {best['question'][:60]} | {best['call']} @ {round(best['call_odds']*100)}c | conf {best['confidence']}%")
    trades_today += 1
    msg = build_message(best, trades_today)
    sent = send_telegram(msg)
    if sent:
        sent_slugs.add(best["slug"])

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Polymarket Signal Bot starting...")
    send_telegram("✅ Bot LIVE! Scanning every 2hrs for best Polymarket trades. Up to 8 signals/day.")

    scan()

    schedule.every(2).hours.do(scan)

    while True:
        schedule.run_pending()
        time.sleep(60)
