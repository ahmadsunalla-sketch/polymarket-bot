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
MAX_TRADES = 10

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
        print(f"Daily reset - {today}")
        send_telegram(f"🌅 New day! Bot reset. Targeting 8-10 best trades today.")

# ── Score market ──────────────────────────────────────────────────────────────
# Strategy: find the BEST value trades
# Best value = you are betting on something at low odds that has real chance
# We look at BOTH sides - maybe YES is 30c but NO is 70c
# We pick whichever side gives best value
def score_market(m):
    try:
        prices = m.get("outcomePrices")
        if not prices:
            return None
        p = json.loads(prices) if isinstance(prices, str) else prices
        yes = float(p[0])
        no  = float(p[1])

        # Skip near-certain outcomes (above 90c) - tiny payout
        # Skip near-impossible (below 5c) - too risky
        if yes > 0.92 or yes < 0.05:
            return None
        if no > 0.92 or no < 0.05:
            return None

        # Volume
        vol = float(m.get("volume") or 0) + float(m.get("volume24hr") or 0)
        if vol < 1000:  # need at least $1K traded for reliability
            return None

        # Time remaining
        end_iso = m.get("endDateIso") or m.get("endDate")
        hours_left = 24
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                hours_left = (end_dt - now_dt).total_seconds() / 3600
                if hours_left < 0 or hours_left > 168:
                    return None
            except:
                pass

        # ── Pick the best side to bet ─────────────────────────────────────────
        # "Value" side = the underdog with real chance
        # If YES=40c NO=60c → bet YES (underdog, bigger payout)
        # If YES=65c NO=35c → bet NO (underdog, bigger payout)
        # If YES=50c NO=50c → pick YES (coin flip, still worth if vol high)

        # We always pick the LOWER odds side (bigger payout) 
        # but only if it's not below 15c (too risky)
        if yes <= no:
            call = "YES"
            call_odds = yes
            other_odds = no
        else:
            call = "NO"
            call_odds = no
            other_odds = yes

        # Floor: dont bet on anything below 15c (too unlikely)
        if call_odds < 0.15:
            # Try the other side instead
            call = "YES" if call == "NO" else "NO"
            call_odds, other_odds = other_odds, call_odds
            if call_odds < 0.15:
                return None

        # Payout if you win: bet $100 at 30c = get $333 back = +$233
        payout_multiplier = round(1 / call_odds, 2)
        potential_profit = round((payout_multiplier - 1) * 100, 1)

        # ── Value score ───────────────────────────────────────────────────────
        # Best trades: 25-55c range on call side = good payout + reasonable chance
        # Payout score peaks at 25c (4x money) and decreases toward 50c (2x)
        if call_odds <= 0.30:
            payout_score = 1.0   # huge payout potential
        elif call_odds <= 0.45:
            payout_score = 0.85  # great payout
        elif call_odds <= 0.55:
            payout_score = 0.65  # decent payout
        else:
            payout_score = 0.4   # smaller payout

        vol_score = min(vol / 50000, 1.0)
        time_score = 1.0 if hours_left <= 24 else (0.75 if hours_left <= 72 else 0.5)

        score = (payout_score * 0.45) + (vol_score * 0.35) + (time_score * 0.2)

        # Risk label
        if call_odds < 0.25:
            risk = "HIGH RISK / HIGH REWARD"
        elif call_odds < 0.45:
            risk = "MEDIUM RISK"
        else:
            risk = "LOW RISK"

        confidence = min(int(45 + vol_score * 30 + time_score * 15), 90)

        return {
            "slug":       m.get("slug", ""),
            "question":   m.get("question") or m.get("title") or "Unknown",
            "category":   m.get("category") or m.get("tag") or "General",
            "url":        f"https://polymarket.com/event/{m.get('slug','')}",
            "yes": yes, "no": no,
            "call": call, "call_odds": call_odds,
            "score": score,
            "confidence": confidence,
            "hours_left": hours_left,
            "volume": vol,
            "potential": potential_profit,
            "multiplier": payout_multiplier,
            "risk": risk,
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
                continue
            markets = r.json()
            if not isinstance(markets, list):
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

    print(f"Checked {total_checked} total, found {len(scored)} good value trades")
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

# ── Build message ─────────────────────────────────────────────────────────────
def build_message(m, trade_num):
    emoji = "🟢" if m["call"] == "YES" else "🔴"
    h = m["hours_left"]
    time_str = f"{round(h*60)}min" if h < 1 else f"{round(h)}h"
    vol_str = f"${round(m['volume']/1000)}K" if m["volume"] > 1000 else f"${round(m['volume'])}"
    risk_emoji = "🔥" if "HIGH" in m["risk"] else "⚡" if "MEDIUM" in m["risk"] else "✅"

    return "\n".join([
        f"💰 SIGNAL #{trade_num} — {risk_emoji} {m['risk']}",
        "",
        f"{emoji} BET {m['call']}",
        "",
        f"📋 {m['question']}",
        "",
        f"📊 YES {round(m['yes']*100)}¢  /  NO {round(m['no']*100)}¢",
        f"🎯 Your bet: {m['call']} @ {round(m['call_odds']*100)}¢",
        f"💵 Payout: {m['multiplier']}x your money",
        f"📈 Profit on $100: +${m['potential']}",
        f"📈 Profit on $500: +${round(m['potential']*5)}",
        f"⏱ Resolves: ~{time_str}",
        f"📉 Volume: {vol_str}",
        f"🗂 {m['category']}",
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
        print("No good value markets found this scan")
        return

    signals_sent = 0
    max_per_scan = 2

    for m in markets:
        if signals_sent >= max_per_scan:
            break
        if trades_today >= MAX_TRADES:
            break
        if m["slug"] in sent_slugs:
            continue

        print(f"Signal: {m['question'][:55]} | BET {m['call']} @ {round(m['call_odds']*100)}c | {m['multiplier']}x | {m['risk']}")
        trades_today += 1
        signals_sent += 1
        msg = build_message(m, trades_today)
        sent = send_telegram(msg)
        if sent:
            sent_slugs.add(m["slug"])
        time.sleep(3)

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Polymarket Signal Bot starting...")
    send_telegram(
        "✅ Bot LIVE!\n\n"
        "Strategy: Best value trades across ALL odds\n"
        "Targets 15c-85c range — picks highest payout with real chance\n"
        "8-10 signals per day · Scans every 3 hours"
    )

    scan()
    schedule.every(3).hours.do(scan)

    while True:
        schedule.run_pending()
        time.sleep(60)
