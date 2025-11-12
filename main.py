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
from collections import Counter

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

def parse_yr_web(json_data):
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
        if date not in candidates:
            continue

        data = item.get("data", {})
        instant = data.get("instant", {}).get("details", {})
        temp = instant.get("air_temperature")
        wind_speed = instant.get("wind_speed")
        wind_dir_deg = instant.get("wind_from_direction")

        precip = 0.0
        for key in ["next_1_hours","next_6_hours","next_12_hours"]:
            if key in data and data[key].get("details"):
                precip += data[key]["details"].get("precipitation_amount",0.0)

        candidates[date].append({
            "time": t,
            "temp": temp,
            "wind_speed": wind_speed,
            "wind_dir_deg": wind_dir_deg,
            "precip_mm": precip
        })

    for d, lst in candidates.items():
        if not lst:
            continue
        temps = [x["temp"] for x in lst if x["temp"] is not None]
        wind_speeds = [x["wind_speed"] for x in lst if x["wind_speed"] is not None]
        wind_dirs = [x["wind_dir_deg"] for x in lst if x["wind_dir_deg"] is not None]
        total_precip = sum(x["precip_mm"] for x in lst)

        # доминирующее направление ветра
        wind_dir = None
        if wind_dirs:
            wind_dir = Counter(wind_dirs).most_common(1)[0][0]

        results[str(d)] = {
            "temp_min": round(min(temps),1) if temps else None,
            "temp_max": round(max(temps),1) if temps else None,
            "wind_speed": round(sum(wind_speeds)/len(wind_speeds),1) if wind_speeds else None,
            "wind_dir_deg": wind_dir,
            "precip_mm": round(total_precip,1)
        }
    return results

# --- Build forecast image ---
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
        yr = parse_yr_web(yr_raw)
    except Exception as e:
        logger.error(f"Ошибка при получении прогноза: {e}")
        return None

    left_margin = 12
    top_margin = 50
    width = 700
    height = 280
    row_height = 35
    img = Image.new("RGB", (width, height), "#E0F7FF")  # нежно-голубой фон
    draw = ImageDraw.Draw(img)
    try:
        font_b = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        font_b = ImageFont.load_default()
        font = ImageFont.load_default()

    draw.text((left_margin, 10), f"5-day forecast — {location_name}", font=font_b, fill=(0,0,0))

    y_offset = top_margin
    headers = ["Date", "Temp (Max/Min °C)", "Wind (m/s)", "Precip (mm)"]
    x_positions = [left_margin, 160, 340, 500]
    col_widths = [150, 160, 160, 160]
    prev_y = y_offset

    # Заголовки
    for i, h in enumerate(headers):
        x = x_positions[i]
        draw.text((x + col_widths[i]//2, y_offset), h, font=font_b, fill=(0,0,0), anchor="mm")
    y_offset += row_height

    today_date = datetime.now(TIMEZONE).date()

    for day in sorted(yr.keys()):
        info = yr[day]
        dt = datetime.fromisoformat(day)
        date_str = "Today" if dt.date() == today_date else dt.strftime("%a %d %b")
        temp = f"{info['temp_max']}/{info['temp_min']}" if info['temp_min'] is not None else "?"
        wind_dir = deg_to_compass(info["wind_dir_deg"])
        wind_speed = f"{info['wind_speed']}" if info["wind_speed"] is not None else "?"
        wind = f"{wind_dir} {wind_speed}"
        precip = info["precip_mm"]
        row = [date_str, temp, wind, f"{precip}"]

        # линия по центру между строками
        line_y = prev_y + row_height // 2
        draw.line((left_margin, line_y, width-left_margin, line_y), fill="gray", width=1)
        prev_y = y_offset

        for i, val in enumerate(row):
            x = x_positions[i]
            draw.text((x + col_widths[i]//2, y_offset), str(val), font=font, fill=(0,0,0), anchor="mm")
        y_offset += row_height

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --- Send forecast ---
def send_forecast():
    d = load_data()
    if not d.get("coords") or not d.get("chat_id") or not d.get("enabled", True):
        return
    bio = build_image()
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
    bio = build_image()
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
