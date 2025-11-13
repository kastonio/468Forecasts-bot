import os
import json
import io
from datetime import datetime, timedelta
import pytz
import requests
from PIL import Image, ImageDraw, ImageFont
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import logging
from statistics import mean

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.error("TELEGRAM_TOKEN not set in environment")
USER_AGENT = "468ForecastsBot/1.0 (contact@example.com)"
YRNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
DATA_FILE = "data.json"
TIMEZONE = pytz.timezone("Europe/Moscow")

# Ensure data file exists
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"admin_id": None, "chat_id": None, "coords": None, "location_name": None, "enabled": True}, f)

# --- Data helpers ---
def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def is_admin(user_id):
    d = load_data()
    return d.get("admin_id") == user_id

# --- Bot commands ---
async def set_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    d = load_data()
    current_admin = d.get("admin_id")

    if current_admin and update.effective_user.id != current_admin:
        await update.message.reply_text("Только текущий админ может назначать нового.")
        return

    if not args:
        new_admin_id = update.effective_user.id
        d["admin_id"] = new_admin_id
        d["chat_id"] = update.effective_chat.id
        save_data(d)
        await update.message.reply_text(f"Назначен админ: {new_admin_id}")
        return

    target = args[0]
    try:
        if target.startswith("@"):
            user_chat = await context.bot.get_chat(target)
            new_admin_id = user_chat.id
        else:
            new_admin_id = int(target)
    except Exception as e:
        logger.warning(f"Cannot resolve target {target}: {e}")
        await update.message.reply_text("Не удалось найти пользователя. Передайте ID или @username (пользователь должен быть видим боту).")
        return

    d["admin_id"] = new_admin_id
    d["chat_id"] = update.effective_chat.id
    save_data(d)
    await update.message.reply_text(f"Назначен админ: {new_admin_id}")

COORDS, NAME = range(2)

async def set_coords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может задать координаты.")
        return ConversationHandler.END
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /setcoords <lat> <lon>")
        return ConversationHandler.END
    try:
        lat = float(args[0])
        lon = float(args[1])
    except ValueError:
        await update.message.reply_text("Координаты должны быть числами. Пример: /setcoords 55.75 37.62")
        return ConversationHandler.END
    context.user_data["coords"] = {"lat": lat, "lon": lon}
    await update.message.reply_text("Введите название места (это будет отображаться в карточке прогноза):")
    return NAME

async def save_location_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    coords = context.user_data.get("coords")
    d = load_data()
    d["coords"] = coords
    d["location_name"] = name
    d["enabled"] = True
    save_data(d)
    await update.message.reply_text(f"Сохранено: {coords['lat']}, {coords['lon']} ({name})")
    return ConversationHandler.END

async def stop_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может управлять рассылкой.")
        return
    d = load_data()
    d["enabled"] = False
    save_data(d)
    await update.message.reply_text("Рассылка остановлена.")

async def start_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может управлять рассылкой.")
        return
    d = load_data()
    d["enabled"] = True
    save_data(d)
    await update.message.reply_text("Рассылка включена.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/setadmin [@username|id] - назначить админа\n"
        "/setcoords <lat> <lon> - задать координаты (только админ)\n"
        "/forecast - получить прогноз сейчас\n"
        "/stopforecast - остановить рассылку\n"
        "/startforecast - включить рассылку\n"
        "/help - помощь\n"
    )
    await update.message.reply_text(txt)

# --- Forecast parsing ---
def deg_to_compass(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    ix = int((deg + 11.25) / 22.5) % 16
    return dirs[ix]

# ... parse_yr и parse_current_conditions без изменений ...

# --- Build image ---
def build_image():
    d = load_data()
    if not d.get("coords"):
        return None
    lat = d["coords"]["lat"]
    lon = d["coords"]["lon"]
    location_name = d.get("location_name") or "unknown"

    try:
        yr_raw = requests.get(
            YRNO_URL, params={"lat": lat, "lon": lon},
            headers={"User-Agent": USER_AGENT}, timeout=15
        ).json()
        forecast = parse_yr(yr_raw)
        current = parse_current_conditions(yr_raw)
    except Exception as e:
        logger.exception("Ошибка получения данных от Yr:")
        return None

    num_days = len(forecast)
    row_h = 36
    height = 140 + int(num_days * row_h * 1.1)
    width = 700
    img = Image.new("RGB", (width, height), (220, 235, 255))
    draw = ImageDraw.Draw(img)

    font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    font_title_small = ImageFont.truetype("DejaVuSans-Bold.ttf", 9)  # уменьшенный шрифт
    font_header = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    font_value = ImageFont.truetype("DejaVuSans.ttf", 13)

    # Шапка
    now_str = datetime.now(TIMEZONE).strftime("%H:%M %d.%m.%Y")
    draw.text((11, 7), f"468 Forecasts: {now_str}", font=font_title_small, fill=(0,0,0))
    draw.text((11, 30), location_name, font=font_title, fill=(0,0,0))

    # Current conditions
    cc_y = 55
    draw.text((11, cc_y), "Current conditions:", font=font_header, fill=(0,0,0))
    cc_txt = f" Temp: {current.get('temp','?')}°C | Wind: {current.get('wind','?')} | Precip: {current.get('precip')}"
    draw.text((11 + draw.textbbox((0,0), "Current conditions:", font=font_header)[2], cc_y), cc_txt, font=font_value, fill=(0,0,0))

    # ... остальной код построения таблицы без изменений ...

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --- Остальной код (send_forecast, forecast_command, schedule_jobs, main) без изменений ---

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not set. Exiting.")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('setcoords', set_coords)],
        states={NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_location_name)]},
        fallbacks=[]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('setadmin', set_admin_cmd))
    app.add_handler(CommandHandler('forecast', forecast_command))
    app.add_handler(CommandHandler('stopforecast', stop_forecast))
    app.add_handler(CommandHandler('startforecast', start_forecast))
    app.add_handler(CommandHandler('help', help_command))

    schedule_jobs()
    app.run_polling()

if __name__ == '__main__':
    main()
