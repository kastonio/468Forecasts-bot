# main.py
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

# --- Ensure data file exists ---
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"admin_id": None, "chats": {}}, f, ensure_ascii=False, indent=2)

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

def get_chat_data(chat_id):
    d = load_data()
    return d.get("chats", {}).get(str(chat_id), {})

def save_chat_data(chat_id, chat_data):
    d = load_data()
    if "chats" not in d:
        d["chats"] = {}
    d["chats"][str(chat_id)] = chat_data
    save_data(d)

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
        await update.message.reply_text("Не удалось найти пользователя. Передайте ID или @username (пользователь должен быть видим боту).")
        return
    d["admin_id"] = new_admin_id
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
    await update.message.reply_text("Введите название места (для отображения в карточке прогноза):")
    return NAME

async def save_location_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    coords = context.user_data.get("coords")
    chat_id = update.effective_chat.id
    chat_data = {
        "coords": coords,
        "location_name": name,
        "enabled": True
    }
    save_chat_data(chat_id, chat_data)
    await update.message.reply_text(f"Сохранено для этого чата: {coords['lat']}, {coords['lon']} ({name})")
    return ConversationHandler.END

async def stop_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может управлять рассылкой.")
        return
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    chat_data["enabled"] = False
    save_chat_data(chat_id, chat_data)
    await update.message.reply_text("Рассылка остановлена для этого чата.")

async def start_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может управлять рассылкой.")
        return
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    chat_data["enabled"] = True
    save_chat_data(chat_id, chat_data)
    await update.message.reply_text("Рассылка включена для этого чата.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/setadmin [@username|id] - назначить админа\n"
        "/setcoords <lat> <lon> - задать координаты (только админ)\n"
        "/forecast - получить свежий прогноз\n"
        "/stopforecast - остановить рассылку (только админ)\n"
        "/startforecast - включить рассылку (только админ)\n"
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

def parse_yr(json_data):
    tz = TIMEZONE
    props = json_data.get("properties", {})
    timeseries = props.get("timeseries", [])
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).date() for i in range(5)]
    candidates = {d: [] for d in target_dates}

    for item in timeseries:
        t_iso = item.get("time")
        if not t_iso:
            continue
        try:
            t = datetime.fromisoformat(t_iso.replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        date = t.date()
        if date not in candidates:
            continue

        data = item.get("data", {})
        instant = data.get("instant", {}).get("details", {})
        temp = instant.get("air_temperature")
        wind_speed = instant.get("wind_speed")
        wind_dir = instant.get("wind_from_direction")
        precip = 0.0
        if "next_1_hours" in data and data["next_1_hours"].get("details"):
            precip = data["next_1_hours"]["details"].get("precipitation_amount", 0.0)
        candidates[date].append({
            "temp": temp,
            "wind_speed": wind_speed,
            "wind_dir": wind_dir,
            "precip_mm": precip
        })

    results = {}
    for d, lst in candidates.items():
        if not lst:
            continue
        temps = [x["temp"] for x in lst if x["temp"] is not None]
        wind_speeds = [x["wind_speed"] for x in lst if x["wind_speed"] is not None]
        wind_dirs = [x["wind_dir"] for x in lst if x["wind_dir"] is not None]
        total_precip = sum(x["precip_mm"] for x in lst)
        wind_dir_rep = wind_dirs[len(wind_dirs)//2] if wind_dirs else None
        results[str(d)] = {
            "temp_min": round(min(temps)) if temps else None,
            "temp_max": round(max(temps)) if temps else None,
            "wind_speed": round(mean(wind_speeds),1) if wind_speeds else None,
            "wind_dir_deg": wind_dir_rep,
            "precip_mm": round(total_precip, 1)
        }
    return results

# --- Build image ---
def build_image(chat_data):
    if not chat_data.get("coords"):
        return None
    lat = chat_data["coords"]["lat"]
    lon = chat_data["coords"]["lon"]
    location_name = chat_data.get("location_name") or "unknown"
    try:
        yr_raw = requests.get(
            YRNO_URL, params={"lat": lat, "lon": lon},
            headers={"User-Agent": USER_AGENT}, timeout=15
        ).json()
        yr = parse_yr(yr_raw)
    except Exception as e:
        logger.exception("Ошибка получения данных от Yr:")
        return None

    # --- Drawing image ---
    width, height = 700, 300
    img = Image.new("RGB", (width, height), (220, 235, 255))
    draw = ImageDraw.Draw(img)
    font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    font_header = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    font_value = ImageFont.truetype("DejaVuSans.ttf", 13)
    draw.text((11, 7), f"468 Forecasts: {location_name}", font=font_title, fill=(0,0,0))
    headers = ["Date", "Temp (°C)", "Wind (m/s)", "Rain (mm)", "Snow (cm)"]
    col_centers = [80, 240, 400, 540, 650]
    y_start = 44
    row_h = 36
    for cx, h in zip(col_centers, headers):
        w = draw.textbbox((0,0), h, font=font_header)[2]
        draw.text((cx - w/2, y_start), h, font=font_header, fill=(0,0,0))
    y = y_start + int(1.2 * row_h)
    today = datetime.now(TIMEZONE).date()
    def text_size(txt, f):
        b = draw.textbbox((0,0), txt, font=f)
        return b[2]-b[0], b[3]-b[1]
    for day_str in sorted(yr.keys()):
        info = yr[day_str]
        dt = datetime.fromisoformat(day_str)
        label = "Today" if dt.date() == today else dt.strftime("%a %d %b")
        tmax = info.get("temp_max")
        tmin = info.get("temp_min")
        t_text = f"{tmax}/{tmin}" if (tmax is not None and tmin is not None) else "?"
        def temp_color(v):
            if v is None: return (0,0,0)
            if v > 0: return (200,0,0)
            if v < 0: return (0,0,200)
            return (0,0,0)
        wind_dir = deg_to_compass(info.get("wind_dir_deg"))
        wind_speed = info.get("wind_speed")
        wind_txt = f"{wind_dir} {wind_speed if wind_speed is not None else '?'}"
        rain_val = info.get("precip_mm", 0.0)
        rain = f"{rain_val:.1f}" if rain_val else "-"
        snow_val = round(rain_val * 1.5, 1) if (tmax is not None and tmax <= 0) else 0.0
        snow = f"{snow_val:.1f}" if snow_val else "-"
        cells = [label, t_text, wind_txt, rain, snow]
        draw.line((12, y - row_h/2, width - 12, y - row_h/2), fill=(160,160,160), width=1)
        for i, (cx, txt) in enumerate(zip(col_centers, cells)):
            fill_color = (0,0,0)
            if i == 3 and txt != "-": fill_color = (200,0,0)
            if i == 4 and txt != "-": fill_color = (0,0,200)
            w, _ = text_size(txt, font_value)
            draw.text((cx - w/2, y), txt, font=font_value, fill=fill_color)
        y += int(row_h * 1.1)
    draw.text((width - 40, height - 22), "yr.no", font=font_value, fill=(80,80,80))
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --- Sending forecast ---
def send_forecast():
    d = load_data()
    chats = d.get("chats", {})
    bot = Bot(token=TELEGRAM_TOKEN)
    for chat_id, chat_data in chats.items():
        if not chat_data.get("coords") or not chat_data.get("enabled", True):
            continue
        bio = build_image(chat_data)
        if bio is None:
            continue
        try:
            bot.send_photo(chat_id=chat_id, photo=bio, caption=f"468 Forecasts — {chat_data.get('location_name','')}")
        except Exception as e:
            logger.error(f"Ошибка при отправке прогноза в чат {chat_id}: {e}")

async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    if not chat_data.get("coords"):
        await update.message.reply_text("Координаты не заданы для этого чата.")
        return
    bio = build_image(chat_data)
    if bio is None:
        await update.message.reply_text("Ошибка при получении прогноза.")
        return
    await update.message.reply_photo(photo=bio, caption=f"468 Forecasts — {chat_data.get('location_name','')}")

# --- Scheduler ---
def schedule_jobs():
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    for hour in [8, 12, 16, 22]:
        scheduler.add_job(send_forecast, 'cron', hour=hour, minute=0)
    scheduler.start()
    logger.info("Scheduler started with jobs at 08:00, 12:00, 16:00, 22:00 Moscow time")
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
    app.add_handler(CommandHandler('setadmin', set_admin_cmd))
    app.add_handler(CommandHandler('forecast', forecast_command))
    app.add_handler(CommandHandler('stopforecast', stop_forecast))
    app.add_handler(CommandHandler('startforecast', start_forecast))
    app.add_handler(CommandHandler('help', help_command))
    schedule_jobs()
    app.run_polling()

if __name__ == '__main__':
    main()
