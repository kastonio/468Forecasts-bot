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
    """/setadmin <@username|id>  — assign admin. If admin not set, anyone can set."""
    args = context.args
    d = load_data()
    current_admin = d.get("admin_id")

    # If there's an existing admin, only they can change it
    if current_admin and update.effective_user.id != current_admin:
        await update.message.reply_text("Только текущий админ может назначать нового.")
        return

    if not args:
        # If no args — assign the user who invoked as admin
        new_admin_id = update.effective_user.id
        d["admin_id"] = new_admin_id
        d["chat_id"] = update.effective_chat.id
        save_data(d)
        await update.message.reply_text(f"Назначен админ: {new_admin_id}")
        return

    target = args[0]
    try:
        if target.startswith("@"):
            # Try to resolve username via get_chat (works if user is in the chat)
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
        "/setadmin [@username|id] - назначить админа (если админ уже есть — только он может назначать)\n"
        "/setcoords <lat> <lon> - задать координаты (только админ)\n"
        "/forecast - получить прогноз сейчас (доступно всем)\n"
        "/stopforecast - остановить рассылку (только админ)\n"
        "/startforecast - включить рассылку (только админ)\n"
        "/help - помощь\n"
    )
    await update.message.reply_text(txt)

# --- Forecast parsing (Yr.no) ---
def deg_to_compass(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    ix = int((deg + 11.25) / 22.5) % 16
    return dirs[ix]

def parse_yr(json_data):
    """
    Parse YR LocationForecast compact JSON into daily aggregates.
    - Temps: min/max across all times of the day (rounded to int)
    - Precip: sum only of next_1_hours for each timestamp (to avoid double counting)
    - Wind: average speed, pick median-direction-like (middle of list)
    """
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

        # IMPORTANT: take only next_1_hours precipitation (if present) to avoid summing larger intervals repeatedly
        precip = 0.0
        if "next_1_hours" in data and data["next_1_hours"].get("details"):
            precip = data["next_1_hours"]["details"].get("precipitation_amount", 0.0)

        candidates[date].append({
            "time": t,
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

        # choose a representative wind direction: pick middle element if list exists
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
        yr = parse_yr(yr_raw)
    except Exception as e:
        logger.exception("Ошибка получения данных от Yr:")
        return None

    # scale everything by 1.5
    scale = 1.5
    base_w, base_h = 700, 300
    width, height = int(base_w * scale), int(base_h * scale)
    img = Image.new("RGB", (width, height), "#E0F7FF")  # нежно-голубой фон
    draw = ImageDraw.Draw(img)

    # fonts scaled
    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", int(18 * scale))
        font_header = ImageFont.truetype("DejaVuSans-Bold.ttf", int(14 * scale))
        font_value = ImageFont.truetype("DejaVuSans.ttf", int(13 * scale))
    except Exception:
        # fallback to default (size will not scale, but ok)
        font_title = ImageFont.load_default()
        font_header = ImageFont.load_default()
        font_value = ImageFont.load_default()

    # Title
    draw.text((12 * scale, 8 * scale), f"468 Forecasts: {location_name}", font=font_title, fill=(0,0,0))

    # Columns config
    headers = ["Date", "Temp (°C)", "Wind (m/s)", "Rain (mm)", "Snow (cm)"]
    # Column centers
    col_centers = [
        12 * scale + 75 * scale,   # Date
        12 * scale + 75 * scale + 160 * scale,  # Temp
        12 * scale + 75 * scale + 320 * scale,  # Wind
        12 * scale + 75 * scale + 480 * scale,  # Rain
        12 * scale + 75 * scale + 620 * scale   # Snow
    ]
    y_start = 44 * scale
    row_h = int(36 * scale)

    # draw headers centered
    for cx, h in zip(col_centers, headers):
        bbox = draw.textbbox((0,0), h, font=font_header)
        w = bbox[2] - bbox[0]
        draw.text((cx - w/2, y_start), h, font=font_header, fill=(0,0,0))

    y = y_start + int(1.2 * row_h)
    today = datetime.now(TIMEZONE).date()

    # helper: text width using textbbox
    def text_size(txt, f):
        b = draw.textbbox((0,0), txt, font=f)
        return b[2]-b[0], b[3]-b[1]

    # iterate days in chronological order
    for day_str in sorted(yr.keys()):
        info = yr[day_str]
        dt = datetime.fromisoformat(day_str)
        label = "Today" if dt.date() == today else dt.strftime("%a %d %b")

        # Temps: Max/Min (rounded ints already)
        tmax = info.get("temp_max")
        tmin = info.get("temp_min")
        t_text = f"{tmax}/{tmin}" if (tmax is not None and tmin is not None) else "?"

        # Colors: positive red, negative blue, zero black
        def temp_color(v):
            if v is None:
                return (0,0,0)
            if v > 0:
                return (200,0,0)
            if v < 0:
                return (0,0,200)
            return (0,0,0)  # exactly 0 -> black

        # Wind: direction then speed
        wind_dir = deg_to_compass(info.get("wind_dir_deg"))
        wind_speed = info.get("wind_speed")
        wind_txt = f"{wind_dir} {wind_speed if wind_speed is not None else '?'}"

        rain = info.get("precip_mm", 0.0)
        # Snow: if temp_max <= 0 then snow = rain * 1.5 else 0
        snow = round(rain * 1.5, 1) if (tmax is not None and tmax <= 0) else 0.0

        # Prepare cell texts
        cells = [
            label,
            t_text,
            wind_txt,
            f"{rain:.1f}",
            f"{snow:.1f}"
        ]

        # draw horizontal center line between previous and this row: line at y - row_h/2
        line_y = y - row_h / 2
        left_x = 12 * scale
        right_x = width - 12 * scale
        draw.line((left_x, line_y, right_x, line_y), fill=(160,160,160), width=1)

        # render each cell centered
        for cx, txt in zip(col_centers, cells):
            # For temps we must color parts separately (Max red/blue and Min red/blue).
            if txt == t_text and tmax is not None and tmin is not None:
                # render Max and Min separately with color
                max_txt = str(tmax)
                min_txt = str(tmin)
                sep = "/"
                # compute widths
                w_max, _ = text_size(max_txt, font_value)
                w_sep, _ = text_size(sep, font_value)
                w_min, _ = text_size(min_txt, font_value)
                total_w = w_max + w_sep + w_min
                x0 = cx - total_w/2
                # Max
                draw.text((x0, y), max_txt, font=font_value, fill=temp_color(tmax))
                x0 += w_max
                # sep
                draw.text((x0, y), sep, font=font_value, fill=(0,0,0))
                x0 += w_sep
                # Min
                draw.text((x0, y), min_txt, font=font_value, fill=temp_color(tmin))
            else:
                # normal centered single text
                w, h = text_size(txt, font_value)
                draw.text((cx - w/2, y), txt, font=font_value, fill=(0,0,0))

        y += int(row_h * 1.1)  # next row

    # Bottom small "yr.no" signature
    signature = "yr.no"
    sw, sh = text_size(signature, font_value)
    draw.text((width - sw - 12*scale, height - sh - 8*scale), signature, font=font_value, fill=(80,80,80))

    # Save to bytes
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

# /forecast available to all
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
    app.add_handler(CommandHandler('setadmin', set_admin_cmd))
    app.add_handler(CommandHandler('forecast', forecast_command))
    app.add_handler(CommandHandler('stopforecast', stop_forecast))
    app.add_handler(CommandHandler('startforecast', start_forecast))
    app.add_handler(CommandHandler('help', help_command))

    schedule_jobs()
    app.run_polling()

if __name__ == '__main__':
    main()
