# Mark-it V2

Telegram reminder bot with Stars subscriptions.

## Setup on Render
1. Create a new **Web Service** from this repo
2. Environment variables to set (Render dashboard → Environment):
   - `BOT_TOKEN` — your bot token from BotFather
   - `ADMIN_IDS` — comma-separated Telegram user IDs: `7780761760,7702332537`
   - `WEBHOOK_URL` — your Render service URL, e.g. `https://markit-v2.onrender.com`
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`

## Commands
- `/start` — begin, set timezone
- `/add` — new reminder (once/daily/weekly, optional photo on Tier 1+)
- `/myreminders` — view & delete
- `/streak` — your Done streak
- `/upgrade` — buy Tier 1 (10 Stars) or Tier 2 (50 Stars)
- `/stats` — admin only, Stars revenue this month/year
