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
# No max trades — minimum 8 per day, no limit

# Track previous odds to detect movement
# { slug: { "yes": float, "vol": float, "seen_at": timestamp } }
odds_history = {}

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
        send_telegram("🌅 New day! Bot scanning for smart money movements today.")

# ── Analyze market — track movement and smart money ──────────────────────────
def analyze_market(m):
    try:
        import json as j
        prices = m.get("outcomePrices")
        if not prices:
            return None
        p = j.loads(prices) if isinstance(prices, str) else prices
        yes = float(p[0])
        no  = float(p[1])
        slug = m.get("slug", "")

        # Must end within 12 hours
        end_iso = m.get("endDateIso") or m.get("endDate")
        if not end_iso:
            return None
        try:
            end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            hours_left = (end_dt - now_dt).total_seconds() / 3600
            if hours_left < 0.1 or hours_left > 24:
                return None
        except:
            return None

        vol24 = float(m.get("volume24hr") or 0)
        vol_total = float(m.get("volume") or 0)

        # Minimum liquidity — lowered so more markets qualify
        if vol_total < 500:
            return None

        # ── Detect odds movement ──────────────────────────────────────────────
        odds_move = 0
        odds_direction = None
        vol_spike = 1.0

        if slug in odds_history:
            prev = odds_history[slug]
            prev_yes = prev["yes"]
            prev_vol = prev["vol"]
            odds_move = yes - prev_yes
            new_vol = vol_total - prev_vol
            if prev_vol > 0:
                vol_spike = max(new_vol / (prev_vol * 0.1 + 1), 1.0)
            if abs(odds_move) > 0.01:
                odds_direction = "YES" if odds_move > 0 else "NO"

        odds_history[slug] = {"yes": yes, "vol": vol_total, "seen_at": time.time()}

        # ── Score based on activity + conviction ──────────────────────────────
        movement_score = min(abs(odds_move) * 20, 1.0)
        vol_spike_score = min((vol_spike - 1) / 5, 1.0)
        activity_ratio = vol24 / (vol_total + 1)
        activity_score = min(activity_ratio * 3, 1.0)
        time_score = 1.0 if hours_left <= 3 else (0.8 if hours_left <= 6 else 0.6)

        # Conviction score — how far from 50/50 (always usable signal even with no history)
        conviction_score = min(abs(yes - 0.5) * 3, 1.0)

        score = (movement_score * 0.25) + (vol_spike_score * 0.15) + \
                (activity_score * 0.20) + (time_score * 0.15) + (conviction_score * 0.25)

        # Only requirement: must have SOME volume and SOME edge (not exactly 50/50)
        # This guarantees markets pass even on first scan with no history yet
        if conviction_score < 0.15 and activity_score < 0.15:
            return None

        # ── Determine which side to bet ───────────────────────────────────────
        # Follow the smart money direction if detected
        # Otherwise follow the recent activity trend
        if odds_direction:
            call = odds_direction
        elif vol24 > vol_total * 0.3:
            # Heavy recent activity — follow the favored side
            call = "YES" if yes >= no else "NO"
        else:
            call = "YES" if yes >= no else "NO"

        call_odds = yes if call == "YES" else no

        # Payout
        payout_multiplier = round(1 / max(call_odds, 0.05), 2)
        potential_profit = round((payout_multiplier - 1) * 100, 1)

        # Build why explanation
        reasons = []
        if abs(odds_move) > 0.03:
            direction_word = "rising" if odds_move > 0 else "falling"
            reasons.append(f"YES odds {direction_word} {round(abs(odds_move)*100)}¢ since last scan")
        if vol_spike > 2:
            reasons.append(f"Volume spike {round(vol_spike, 1)}x — smart money moving in")
        if activity_ratio > 0.4:
            reasons.append(f"{round(activity_ratio*100)}% of volume is from last 24hrs — fresh activity")
        if hours_left <= 2:
            reasons.append(f"Only {round(hours_left*60)}min left — odds sharpening")
        if not reasons:
            reasons.append(f"High activity market — ${round(vol24/1000)}K traded in 24hrs")

        confidence = min(int(50 + movement_score*25 + vol_spike_score*15 + activity_score*10), 91)

        return {
            "slug": slug,
            "question": m.get("question") or m.get("title") or "Unknown",
            "category": m.get("category") or m.get("tag") or "General",
            "url": f"https://polymarket.com/event/{slug}",
            "yes": yes, "no": no,
            "call": call, "call_odds": call_odds,
            "score": score,
            "confidence": confidence,
            "hours_left": hours_left,
            "volume": vol_total,
            "vol24": vol24,
            "potential": potential_profit,
            "multiplier": payout_multiplier,
            "odds_move": odds_move,
            "vol_spike": vol_spike,
            "reasons": reasons,
        }
    except Exception as e:
        print(f"Analyze error: {e}")
        return None

# ── Fetch and analyze all markets ─────────────────────────────────────────────
def fetch_and_analyze():
    endpoints = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume24hr&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=liquidity&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100",
    ]
    analyzed = []
    seen = set()
    total = 0
    for url in endpoints:
        try:
            r = requests.get(url, timeout=15)
            if not r.ok:
                continue
            markets = r.json()
            if not isinstance(markets, list):
                continue
            print(f"Got {len(markets)} markets")
            for m in markets:
                slug = m.get("slug")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                total += 1
                result = analyze_market(m)
                if result:
                    analyzed.append(result)
        except Exception as e:
            print(f"Fetch error: {e}")

    print(f"Analyzed {total} markets, {len(analyzed)} have smart money signals")
    analyzed.sort(key=lambda x: x["score"], reverse=True)
    return analyzed

# ── Build message ─────────────────────────────────────────────────────────────
def build_message(m, trade_num):
    emoji = "🟢" if m["call"] == "YES" else "🔴"
    h = m["hours_left"]
    time_str = f"{round(h*60)}min" if h < 1 else f"{round(h, 1)}h"
    vol_str = f"${round(m['volume']/1000)}K" if m["volume"] > 1000 else f"${round(m['volume'])}"
    vol24_str = f"${round(m['vol24']/1000)}K" if m["vol24"] > 1000 else f"${round(m['vol24'])}"

    move_str = ""
    if abs(m["odds_move"]) > 0.02:
        move_str = f"📉 Odds moved: {'+' if m['odds_move']>0 else ''}{round(m['odds_move']*100)}¢ toward {'YES' if m['odds_move']>0 else 'NO'}"

    spike_str = ""
    if m["vol_spike"] > 1.5:
        spike_str = f"⚡ Volume spike: {round(m['vol_spike'],1)}x normal — smart money in"

    lines = [
        f"🧠 SMART MONEY SIGNAL #{trade_num}",
        "",
        f"{emoji} BET {m['call']}",
        "",
        f"📋 {m['question']}",
        "",
        f"📊 YES {round(m['yes']*100)}¢  /  NO {round(m['no']*100)}¢",
        f"🎯 Bet: {m['call']} @ {round(m['call_odds']*100)}¢",
        f"💵 Payout: {m['multiplier']}x",
        f"📈 Profit on $100: +${m['potential']}",
        f"📈 Profit on $500: +${round(m['potential']*5)}",
        "",
        "🔍 Why this trade:",
    ]
    for r in m["reasons"]:
        lines.append(f"  • {r}")
    if move_str:
        lines.append(move_str)
    if spike_str:
        lines.append(spike_str)
    lines += [
        "",
        f"👥 Total volume: {vol_str} | 24h: {vol24_str}",
        f"⏱ Resolves in: ~{time_str}",
        f"🗂 {m['category']}",
        "",
        f"🕐 {now_str()}",
        "",
        f"🔗 {m['url']}",
    ]
    return "\n".join(lines)

# ── Scan ──────────────────────────────────────────────────────────────────────
def scan():
    global trades_today
    reset_daily()

    print(f"[{now_str()}] Scanning smart money... ({trades_today} trades today)")
    markets = fetch_and_analyze()

    if not markets:
        print("No smart money signals found this scan")
        return

    signals_sent = 0
    max_per_scan = 3

    for m in markets:
        if signals_sent >= max_per_scan:
            break
        if m["slug"] in sent_slugs:
            continue

        print(f"Signal: {m['question'][:55]} | {m['call']} | {m['multiplier']}x | score {round(m['score'],3)}")
        trades_today += 1
        signals_sent += 1
        msg = build_message(m, trades_today)
        sent = send_telegram(msg)
        if sent:
            sent_slugs.add(m["slug"])
        time.sleep(3)

# ── Boot ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Polymarket Smart Money Bot starting...")
    send_telegram(
        "🧠 Smart Money Bot LIVE!\n\n"
        "Tracks:\n"
        "• Odds movement — smart money shifting\n"
        "• Volume spikes — big money coming in\n"
        "• Fresh activity — what traders are doing NOW\n"
        "• Time pressure — sharpening odds near resolution\n\n"
        "Only trades ending within 12 hours.\n"
        "Minimum 8 signals per day, no cap."
    )
    # First scan immediately
    scan()
    # Then every 90 mins to catch fresh movement
    schedule.every(60).minutes.do(scan)
    while True:
        schedule.run_pending()
        time.sleep(60)
