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

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_AGENT = "468ForecastsBot/1.0 (contact@example.com)"
YRNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
DATA_FILE = "data.json"
TIMEZONE = pytz.timezone("Europe/Moscow")

# --- Ensure data file exists ---
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"admin_id": None, "chat_id": None, "coords": None, "location_name": None, "enabled": True}, f)

# --- Utility functions ---
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
async def set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    d["admin_id"] = update.effective_user.id
    d["chat_id"] = update.effective_chat.id
    save_data(d)
    await update.message.reply_text(
        "Вы назначены админом. Теперь задайте координаты через /setcoords <lat> <lon>"
    )

COORDS, NAME = range(2)

async def set_coords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
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
    await update.message.reply_text(
        "Введите название места (это будет отображаться в карточке прогноза):"
    )
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
    await update.message.reply_text("Рассылка остановлена. Чтобы включить снова — /startforecast")

async def start_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может управлять рассылкой.")
        return
    d = load_data()
    d["enabled"] = True
    save_data(d)
    await update.message.reply_text("Рассылка включена. Бот будет слать прогнозы по расписанию.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/setadmin - назначить себя админом\n"
        "/setcoords <lat> <lon> - задать координаты (после ввода бот попросит название места)\n"
        "/forecast - получить прогноз сейчас\n"
        "/stopforecast - остановить автоматическую рассылку\n"
        "/startforecast - включить автоматическую рассылку\n"
        "/help - показать эту справку\n"
    )
    await update.message.reply_text(txt)

# --- Forecast utilities ---
def deg_to_compass(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    ix = int((deg + 11.25) / 22.5) % 16
    return dirs[ix]

def parse_yr(json_data):
    tz = TIMEZONE
    props = json_data.get("properties", {})
    timeseries = props.get("timeseries", [])
    results = {}
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).date() for i in range(5)]
    candidates = {d: [] for d in target_dates}
    for item in timeseries:
        t_iso = item.get("time")
        if not t_iso:
            continue
        t = datetime.fromisoformat(t_iso.replace("Z", "+00:00")).astimezone(tz)
        date = t.date()
        if date in candidates:
            data = item.get("data", {})
            instant = data.get("instant", {}).get("details", {})
            precip = 0.0
            if data.get("next_1_hours") and data["next_1_hours"].get("details"):
                precip = data["next_1_hours"]["details"].get("precipitation_amount", 0.0)
            candidates[date].append({
                "time": t,
                "temp": instant.get("air_temperature"),
                "wind_speed": instant.get("wind_speed"),
                "wind_dir_deg": instant.get("wind_from_direction"),
                "precip_mm": precip
            })
    for d, lst in candidates.items():
        if not lst:
            continue
        target_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=tz) + timedelta(hours=12)
        best = min(lst, key=lambda x: abs(x["time"] - target_dt))
        results[str(d)] = best
    return results

# --- Build forecast image ---
def build_image_yr_only():
    d = load_data()
    if not d.get("coords"):
        return None

    lat = d["coords"]["lat"]
    lon = d["coords"]["lon"]
    location_name = d.get("location_name") or "unknown"

    # Получаем данные YR.no
    try:
        yr_resp = requests.get(
            YRNO_URL,
            params={"lat": lat, "lon": lon},
            headers={"User-Agent": USER_AGENT},
            timeout=15
        )
        yr_resp.raise_for_status()
        yr = parse_yr(yr_resp.json())
        if not yr:
            raise ValueError("Нет свежих данных YR.no")
    except Exception as e:
        logger.error(f"Ошибка получения данных YR.no: {e}")
        return None

    width, height = 1100, 450
    img = Image.new("RGB", (width, height), "#f8f8f8")
    draw = ImageDraw.Draw(img)

    # Шрифты
    try:
        font_b = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font_b = ImageFont.load_default()
        font = ImageFont.load_default()

    draw.text((12, 10), f"5-day forecast — {location_name}", font=font_b, fill=(0,0,0))

    headers = ["Date", "Wind", "Temp", "Precip"]
    x_positions = [12, 120, 250, 400]
    y_start = 50
    y_step = 60

    for i, header in enumerate(headers):
        draw.text((x_positions[i], y_start), header, font=font_b, fill=(0,0,0))

    def temp_color(temp):
        if temp is None:
            return (180,180,180)
        if temp <= 0: return (0,128,255)
        if temp <= 10: return (100,200,255)
        if temp <= 20: return (255,200,100)
        return (255,50,50)

    for i, date_str in enumerate(sorted(yr.keys())):
        y = y_start + y_step*(i+1)
        yr_data = yr[date_str]

        dt = yr_data["time"]
        date_fmt = dt.strftime("%a %d %b")
        draw.text((x_positions[0], y), date_fmt, font=font, fill=(0,0,0))

        wind_dir = deg_to_compass(yr_data.get("wind_dir_deg"))
        wind_speed = yr_data.get("wind_speed") or 0
        draw.text((x_positions[1], y), f"{wind_dir} {wind_speed}", font=font, fill=(0,0,0))

        temp = yr_data.get("temp","?")
        draw.text((x_positions[2], y), f"{temp}", font=font, fill=temp_color(temp))

        # Осадки текстом
        precip = yr_data.get("precip_mm") or 0
        if precip == 0:
            cond = "sunny"
        elif temp <= 0:
            cond = "snow"
        elif 0 < temp <= 5 and precip > 0:
            cond = "rain_snow"
        else:
            cond = "rain"
        draw.text((x_positions[3], y), f"{cond} {precip}mm", font=font, fill=(0,0,255))

        # Рамка блока
        draw.rectangle([0,y-5,width,y+25], outline="#cccccc", width=1)

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --- Send forecast ---
def send_forecast():
    d = load_data()
    if not d.get("coords") or not d.get("chat_id") or not d.get("enabled", True):
        return
    bio = build_image_yr_only()
    if bio is None:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        bot.send_photo(chat_id=d["chat_id"], photo=bio, caption=f"Прогноз на 5 дней ({d.get('location_name','')})")
    except Exception as e:
        logger.error(f"Ошибка при отправке прогноза: {e}")

async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    if not d.get("coords"):
        await update.message.reply_text("Координаты не заданы. Админ должен задать через /setcoords.")
        return
    bio = build_image_yr_only()
    if bio is None:
        await update.message.reply_text("Ошибка при получении прогноза.")
        return
    await update.message.reply_photo(photo=bio, caption=f"Прогноз на 5 дней ({d.get('location_name','')})")

# --- Scheduler ---
def schedule_jobs():
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    for hour in [0,6,12,18]:
        scheduler.add_job(send_forecast, 'cron', hour=hour, minute=0)
    scheduler.start()
    return scheduler

# --- Main ---
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
    app.add_handler(CommandHandler('setadmin', set_admin))
    app.add_handler(CommandHandler('forecast', forecast_command))
    app.add_handler(CommandHandler('stopforecast', stop_forecast))
    app.add_handler(CommandHandler('startforecast', start_forecast))
    app.add_handler(CommandHandler('help', help_command))

    schedule_jobs()
    app.run_polling()

if __name__ == '__main__':
    main()
