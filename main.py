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
USER_AGENT = "468ForecastsBot/1.0"
YRNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
DATA_FILE = "data.json"
TIMEZONE = pytz.timezone("Europe/Moscow")

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"admin_id": None, "chat_id": None, "coords": None, "location_name": None, "enabled": True}, f)

# --- Data I/O ---
def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def is_admin(user_id):
    d = load_data()
    return d.get("admin_id") == user_id

# --- Commands ---
async def set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    d["admin_id"] = update.effective_user.id
    d["chat_id"] = update.effective_chat.id
    save_data(d)
    await update.message.reply_text("Вы назначены админом. Теперь задайте координаты через /setcoords <lat> <lon>")

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
        "/setadmin - назначить себя админом\n"
        "/setcoords <lat> <lon> - задать координаты\n"
        "/forecast - получить прогноз сейчас\n"
        "/stopforecast - остановить рассылку\n"
        "/startforecast - включить рассылку\n"
        "/help - помощь\n"
    )
    await update.message.reply_text(txt)

# --- Parsing YR.no ---
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
        for key in ["next_1_hours", "next_6_hours"]:
            if key in data and data[key].get("details"):
                precip += data[key]["details"].get("precipitation_amount", 0.0)

        candidates[date].append({
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

        results[str(d)] = {
            "temp_min": round(min(temps)) if temps else None,
            "temp_max": round(max(temps)) if temps else None,
            "wind_speed": round(mean(wind_speeds),1) if wind_speeds else None,
            "wind_dir_deg": wind_dirs[len(wind_dirs)//2] if wind_dirs else None,
            "precip_mm": round(total_precip,1)
        }
    return results

# --- Build Image ---
def build_image():
    d = load_data()
    if not d.get("coords"):
        return None
    lat, lon = d["coords"]["lat"], d["coords"]["lon"]
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

    scale = 1.5
    width, height = int(800 * scale), int(400 * scale)
    img = Image.new("RGB", (width, height), (220, 235, 255))
    draw = ImageDraw.Draw(img)

    try:
        font_b = ImageFont.truetype("DejaVuSans-Bold.ttf", int(18 * scale))
        font = ImageFont.truetype("DejaVuSans.ttf", int(14 * scale))
    except Exception:
        font_b = ImageFont.load_default()
        font = ImageFont.load_default()

    draw.text((20 * scale, 10 * scale), f"468 Forecasts: {location_name}", font=font_b, fill=(0,0,0))

    headers = ["Date", "Temp (°C)", "Wind (m/s)", "Rain (mm)", "Snow (cm)"]
    x_positions = [20*scale, 180*scale, 350*scale, 520*scale, 670*scale]
    y_start = 60 * scale
    line_height = 40 * scale

    for x, h in zip(x_positions, headers):
        draw.text((x, y_start), h, font=font_b, fill=(0,0,0))
    y_offset = y_start + line_height

    today = datetime.now(TIMEZONE).date()

    def text_size(draw, text, font):
        bbox = draw.textbbox((0,0), text, font=font)
        return bbox[2]-bbox[0], bbox[3]-bbox[1]

    for day in sorted(yr.keys()):
        info = yr[day]
        date_obj = datetime.fromisoformat(day)
        day_label = "Today" if date_obj.date() == today else date_obj.strftime("%a %d %b")

        temp_txt = f"{info['temp_max']}/{info['temp_min']}"
        color = (200,0,0) if info['temp_max'] > 0 else (0,0,200)
        wind_txt = f"{deg_to_compass(info['wind_dir_deg'])} {info['wind_speed']}"
        rain = info['precip_mm']
        snow = round(rain * 1.5) if info['temp_max'] <= 0 else 0

        row = [day_label, temp_txt, wind_txt, f"{rain:.1f}", f"{snow:.1f}"]

        for x, val in zip(x_positions, row):
            fill_color = color if val == temp_txt else (0,0,0)
            w, h = text_size(draw, val, font)
            draw.text((x + 50*scale - w/2, y_offset), val, font=font, fill=fill_color)

        # линия по центру между строками
        draw.line([(x_positions[0], y_offset + line_height/2),
                   (x_positions[-1] + 150*scale, y_offset + line_height/2)],
                  fill=(150,150,150), width=1)
        y_offset += line_height

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
        bot.send_photo(chat_id=d["chat_id"], photo=bio, caption=f"468 Forecasts — {d.get('location_name','')}")
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
    await update.message.reply_photo(photo=bio, caption=f"468 Forecasts — {d.get('location_name','')}")

def schedule_jobs():
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    for hour in [0,6,12,18]:
        scheduler.add_job(send_forecast, 'cron', hour=hour, minute=0)
    scheduler.start()
    return scheduler

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
