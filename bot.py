import os
import time
import requests
import schedule
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT", "")

last_sent_slug = None

# ── Utils ─────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.utcnow().strftime("%H:%M:%S UTC")

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("No Telegram config — set TG_TOKEN and TG_CHAT env vars")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": msg}, timeout=10)
        data = r.json()
        if data.get("ok"):
            print(f"✅ Telegram sent at {now_str()}")
            return True
        else:
            print(f"❌ Telegram error: {data.get('description')}")
            return False
    except Exception as e:
        print(f"❌ Telegram exception: {e}")
        return False

# ── Score a market ────────────────────────────────────────────────────────────
def score_market(m):
    try:
        prices = m.get("outcomePrices")
        if not prices:
            return None
        import json
        p = json.loads(prices) if isinstance(prices, str) else prices
        yes = float(p[0])
        no  = float(p[1])
        edge = abs(yes - 0.5)

        # Need meaningful edge
        if edge < 0.08:
            return None

        # Volume
        vol = float(m.get("volume") or 0) + float(m.get("volume24hr") or 0)

        # Time remaining
        end_iso = m.get("endDateIso") or m.get("endDate")
        if not end_iso:
            return None
        try:
            from datetime import timezone
            end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            hours_left = (end_dt - now_dt).total_seconds() / 3600
        except:
            return None

        if hours_left < 0.3 or hours_left > 28:
            return None

        # Scores
        vol_score  = min(vol / 50000, 1.0)
        time_score = 1.0 if 2 <= hours_left <= 12 else (0.7 if hours_left <= 24 else 0.4)
        score      = (edge * 0.55) + (vol_score * 0.25) + (time_score * 0.2)

        call      = "YES" if yes >= 0.5 else "NO"
        call_odds = yes if call == "YES" else no
        confidence = min(int(40 + edge * 120 + vol_score * 20), 94)

        return {
            "slug":       m.get("slug", ""),
            "question":   m.get("question") or m.get("title") or "Unknown",
            "category":   m.get("category") or m.get("tag") or "General",
            "url":        f"https://polymarket.com/event/{m.get('slug','')}",
            "yes":        yes, "no": no,
            "edge":       edge, "score": score,
            "call":       call, "call_odds": call_odds,
            "confidence": confidence,
            "hours_left": hours_left,
            "volume":     vol,
        }
    except Exception as e:
        return None

# ── Fetch best market ─────────────────────────────────────────────────────────
def fetch_best_market():
    endpoints = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=volume24hr&ascending=false",
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=volume&ascending=false",
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

    if not scored:
        return None

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[0]

# ── Build message ─────────────────────────────────────────────────────────────
def build_message(m):
    emoji = "🟢" if m["call"] == "YES" else "🔴"
    h = m["hours_left"]
    time_str = f"{round(h*60)}min" if h < 1 else f"{round(h)}h"
    vol_str  = f"${round(m['volume']/1000)}K" if m['volume'] > 1000 else f"${round(m['volume'])}"

    why_parts = [
        f"Odds: {round(m['call_odds']*100)}¢ on {m['call']} — strong crowd conviction",
        f"Resolves in ~{time_str}",
        f"Volume: {vol_str} traded",
    ]
    if m["edge"] > 0.2:
        why_parts.append(f"Edge: {round(m['edge']*100)}¢ from 50/50")

    return "\n".join([
        "🎯 POLYMARKET DAILY SIGNAL",
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
        "",
        f"🔍 Why: {' · '.join(why_parts)}",
        "",
        f"🕐 Signal at: {now_str()}",
        "",
        f"🔗 {m['url']}",
    ])

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan():
    global last_sent_slug
    print(f"[{now_str()}] Scanning markets…")
    best = fetch_best_market()
    if not best:
        print("No strong market found this scan.")
        return
    print(f"Best: {best['question'][:60]} | {best['call']} @ {round(best['call_odds']*100)}¢ | conf {best['confidence']}%")

    # Only send if it's a new market
    if best["slug"] == last_sent_slug:
        print("Same market as last signal — skipping.")
        return

    msg = build_message(best)
    sent = send_telegram(msg)
    if sent:
        last_sent_slug = best["slug"]

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🎯 Polymarket Signal Bot starting…")
    send_telegram("✅ Polymarket Signal Bot is LIVE! Scanning every 15 minutes for best trades.")
    scan()  # run immediately on start
    schedule.every(15).minutes.do(scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
