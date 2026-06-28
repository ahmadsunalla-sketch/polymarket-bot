# Polymarket Signal Bot

Scans Polymarket every 15 minutes and sends best trade signal to Telegram.

## Deploy on Railway (free, 5 minutes)

1. Go to https://railway.app and sign up free
2. Click "New Project" → "Deploy from GitHub"
3. Upload these files or connect GitHub repo
4. Set environment variables:
   - TG_TOKEN = your bot token
   - TG_CHAT  = your chat ID
5. Click Deploy — runs 24/7, PC can be off

## Environment Variables

| Variable  | Value                        |
|-----------|------------------------------|
| TG_TOKEN  | Your Telegram bot token      |
| TG_CHAT   | Your Telegram chat ID        |

## How it works

- Scans top Polymarket markets every 15 minutes
- Scores each market by: edge from 50/50, volume, time remaining
- Only signals when one side is 58¢+ (strong conviction)
- Sends signal + link directly to your Telegram
- Never sends the same market twice
