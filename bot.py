import os
import time
import requests
import schedule
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

sent_slugs = set()  # track all sent markets today
last_reset_day = None

# ── Utils ─────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.utcnow().strftime("%H:%M:%S UTC")

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("No Telegram config")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        data = r.json()
        if data.get("ok"):
            print(f"✅ Sent at {now_str()}")
            return True
        else:
            print(f"❌ TG error: {data.get('description')}")
            return False
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

def reset_daily():
    global sent_slugs, last_reset_day
    today = datetime.utcnow().date()
    if last_reset_day != today:
        sent_slugs = set()
        last_reset_day = today
        print(f"Daily reset — {today}")

# ── Score market ──────────────────────────────────────────────────────────────
def score_market(m):
    try:
        import json
        prices = m.get("outcomePrices")
        if not prices:
            return None
        p = json.loads(prices) if isinstance(prices, str) else prices
        yes = float(p[0])
        no  = float(p[1])
        edge = abs(yes - 0.5)

        # Edge between 0.05 and 0.45 (not too obvious, not coin flip)
        # This gives us ~55c to ~95c markets
        if edge < 0.05 or edge > 0.45:
            return None

        # Volume — need at least some liquidity
        vol = float(m.get("volume") or 0) + float(m.get("volume24hr") or 0)
        if vol < 500:  # minimum $500 traded
            return None

        # Time remaining
        end_iso = m.get("endDateIso") or m.get("endDate")
        if not end_iso:
            return None
        try:
            end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            hours_left = (end_dt - now_dt).total_seconds() / 3600
        except:
            return None

        # 30 mins to 24 hours remaining
        if hours_left < 0.5 or hours_left > 24:
            return None

        # Scoring
        vol_score  = min(vol / 30000, 1.0)
        # Sweet spot: 2-8 hours left (enough time but not too far)
        if 2 <= hours_left <= 8:
            time_score = 1.0
        elif hours_left < 2:
            time_score = 0.5
        else:
            time_score = 0.7

        score = (edge * 0.5) + (vol_score * 0.3) + (time_score * 0.2)

        call      = "YES" if yes >= 0.5 else "NO"
        call_odds = yes if call == "YES" else no
        confidence = min(int(35 + edge * 100 + vol_score * 25), 92)

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
    except:
        return None

# ── Fetch top markets ─────────────────────────────────────────────────────────
def fetch_top_markets():
    endpoints = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=liquidity&ascending=false",
    ]
    scored = []
    seen = set()
    for url in endpoints:
        try:
            r = requests.get(url, timeout=10)
            if not r.ok:
                continue
            markets = r.json()
            if not isinstance(markets, list):
                continue
            for m in markets:
                slug = m.get("slug")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                result = score_market(m)
                if result:
                    scored.append(result)
        except Exception as e:
            print(f"Fetch error: {e}")

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

# ── Build message ─────────────────────────────────────────────────────────────
def build_message(m, trade_num):
    emoji = "🟢" if m["call"] == "YES" else "🔴"
    h = m["hours_left"]
    time_str = f"{round(h*60)}min" if h < 1 else f"{round(h)}h"
    vol_str  = f"${round(m['volume']/1000)}K" if m["volume"] > 1000 else f"${round(m['volume'])}"

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

# ── Main scan ─────────────────────────────────────────────────────────────────
trades_today = 0
MAX_TRADES = 8  # cap at 8 per day

def scan():
    global trades_today
    reset_daily()

    if trades_today >= MAX_TRADES:
        print(f"Max trades reached ({MAX_TRADES}) — done for today.")
        return

    print(f"[{now_str()}] Scanning… ({trades_today}/{MAX_TRADES} trades today)")
    markets = fetch_top_markets()

    # Pick best market not already sent
    best = None
    for m in markets:
        if m["slug"] not in sent_slugs:
            best = m
            break

    if not best:
        print("No new markets with edge found.")
        return

    print(f"Signal: {best['question'][:60]} | {best['call']} @ {round(best['call_odds']*100)}¢ | {best['confidence']}%")
    trades_today += 1
    msg = build_message(best, trades_today)
    sent = send_telegram(msg)
    if sent:
        sent_slugs.add(best["slug"])

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🎯 Polymarket Signal Bot starting…")
    send_telegram("✅ Polymarket Signal Bot LIVE! Sending 5-8 best trades per day.")

    scan()  # immediate first scan

    # Scan every 2 hours — gives ~5-8 trades across a 16hr day
    schedule.every(2).hours.do(scan)

    while True:
        schedule.run_pending()
        time.sleep(60)
