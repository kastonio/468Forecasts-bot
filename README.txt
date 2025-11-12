
468 Forecasts - Telegram bot
============================

Files:
- main.py        : bot code
- requirements.txt
- Procfile       : for Railway (worker entry)
- data.json      : persistent settings (admin, coords, etc.)

Setup (GitHub -> Railway):
1) Create a GitHub repo and push these files or upload the ZIP contents.
2) On Railway, new project -> Deploy from GitHub, select the repo.
3) Add environment variables (Railway > Variables / Settings):
   - TELEGRAM_TOKEN = your bot token from BotFather
   - WINDY_API_KEY = bJ9332OxaveREpWc8rYUVPKXGAbr2rT4
4) Deploy. Railway will install requirements and run the Procfile worker.
5) In Telegram: add the bot to a group or chat, then:
   - /setadmin   -> to set yourself as admin (must be performed in target chat)
   - /setcoords <lat> <lon>  -> then input a place name when prompted
   - /forecast  -> get current 5-day comparison image
   - /stopforecast /startforecast /help available

Notes:
- The bot uses Yr.no Locationforecast and Windy Point Forecast APIs.
- Yr.no requires a proper User-Agent; adjust USER_AGENT in main.py if desired.
- The code attempts to parse typical Windy responses; depending on Windy plan/model you might need to adjust parameter names.
- Signature on image: 468Forecasts by Kastonio
