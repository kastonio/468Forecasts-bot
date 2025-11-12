import os
import json
import io
import math
from datetime import datetime, timedelta
import pytz
import requests
from PIL import Image, ImageDraw, ImageFont
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext, ConversationHandler, MessageHandler, Filters

# Configuration (environment variables)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WINDY_API_KEY = os.getenv("WINDY_API_KEY")  # provided in secrets
USER_AGENT = "468ForecastsBot/1.0 (contact@example.com)"
YRNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
WINDY_URL = "https://api.windy.com/api/point-forecast/v2"
DATA_FILE = "data.json"
TIMEZONE = pytz.timezone("Europe/Moscow")

# Ensure data file exists
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w") as f:
        json.dump({"admin_id": None, "chat_id": None, "coords": None, "location_name": None, "enabled": True}, f)

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def is_admin(user_id):
    d = load_data()
    return d.get("admin_id") == user_id

# Commands
def set_admin(update: Update, context: CallbackContext):
    d = load_data()
    d["admin_id"] = update.effective_user.id
    d["chat_id"] = update.effective_chat.id
    save_data(d)
    update.message.reply_text("Вы назначены админом. Теперь задайте координаты через /setcoords <lat> <lon>")

COORDS, NAME = range(2)
def set_coords(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        update.message.reply_text("Только админ может задать координаты.")
        return ConversationHandler.END
    if len(context.args) != 2:
        update.message.reply_text("Использование: /setcoords <lat> <lon>")
        return ConversationHandler.END
    try:
        lat = float(context.args[0])
        lon = float(context.args[1])
    except ValueError:
        update.message.reply_text("Координаты должны быть числами. Пример: /setcoords 55.75 37.62")
        return ConversationHandler.END
    context.user_data["coords"] = {"lat": lat, "lon": lon}
    update.message.reply_text("Введите название места (это будет отображаться в карточке прогноза):")
    return NAME

def save_location_name(update: Update, context: CallbackContext):
    name = update.message.text.strip()
    coords = context.user_data.get("coords")
    d = load_data()
    d["coords"] = coords
    d["location_name"] = name
    d["enabled"] = True
    save_data(d)
    update.message.reply_text(f"Сохранено: {coords['lat']}, {coords['lon']} ({name})")
    return ConversationHandler.END

def stop_forecast(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Только админ может управлять рассылкой.")
        return
    d = load_data()
    d["enabled"] = False
    save_data(d)
    update.message.reply_text("Рассылка остановлена. Чтобы включить снова — /startforecast")

def start_forecast(update: Update, context: CallbackContext):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Только админ может управлять рассылкой.")
        return
    d = load_data()
    d["enabled"] = True
    save_data(d)
    update.message.reply_text("Рассылка включена. Бот будет слать прогнозы по расписанию.")

def help_command(update: Update, context: CallbackContext):
    txt = (
        "/setadmin - назначить себя админом\n"
        "/setcoords <lat> <lon> - задать координаты (после ввода бот попросит название места)\n"
        "/forecast - получить прогноз сейчас\n"
        "/stopforecast - остановить автоматическую рассылку\n"
        "/startforecast - включить автоматическую рассылку\n"
        "/help - показать эту справку\n"
    )
    update.message.reply_text(txt)

# Utility: parse yr.no compact and pick daily values near 12:00
def parse_yr(json_data):
    tz = TIMEZONE
    props = json_data.get("properties", {})
    timeseries = props.get("timeseries", [])
    results = {}
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).date() for i in range(0,5)]
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
            elif data.get("next_6_hours") and data["next_6_hours"].get("details"):
                precip = data["next_6_hours"]["details"].get("precipitation_amount", 0.0)
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

# Utility: parse Windy point-forecast response
def parse_windy(json_data):
    tz = TIMEZONE
    results = {}
    # Windy uses 'forecast' -> 'hours' possibly; models vary. Try common keys.
    forecast = json_data.get("forecast") or json_data.get("data") or {}
    hours = forecast.get("hours") or forecast.get("timeSeries") or forecast.get("hoursData") or []
    if not hours and isinstance(json_data.get("hours"), list):
        hours = json_data.get("hours")
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).date() for i in range(0,5)]
    candidates = {d: [] for d in target_dates}
    for entry in hours:
        t_str = entry.get("time") or entry.get("dt") or entry.get("timestamp")
        if not t_str:
            continue
        try:
            t = datetime.fromisoformat(t_str.replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        d = t.date()
        if d not in candidates:
            continue
        temp = entry.get("temperature_2m") or entry.get("temp") or entry.get("t")
        wind_speed = entry.get("wind_speed_10m") or entry.get("wind_speed") or entry.get("ws")
        wind_dir = entry.get("wind_from_direction_10m") or entry.get("wind_dir") or entry.get("wd")
        precip = entry.get("precipitation") or entry.get("precip") or entry.get("p") or 0.0
        new_snow = entry.get("new_snow") or entry.get("new_snow_1h") or entry.get("snow") or 0.0
        candidates[d].append({
            "time": t,
            "temp": temp,
            "wind_speed": wind_speed,
            "wind_dir_deg": wind_dir,
            "precip_mm": precip,
            "new_snow_cm": new_snow
        })
    for d, lst in candidates.items():
        if not lst:
            continue
        target_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=tz) + timedelta(hours=12)
        best = min(lst, key=lambda x: abs(x["time"] - target_dt))
        results[str(d)] = best
    return results

def deg_to_compass(deg):
    if deg is None:
        return "?"
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    ix = int((deg + 11.25) / 22.5) % 16
    return dirs[ix]

# Build image with mini-table
def build_image():
    d = load_data()
    if not d.get("coords"):
        return None
    lat = d["coords"]["lat"]
    lon = d["coords"]["lon"]
    location_name = d.get("location_name") or "unknown"
    try:
        yr_raw = requests.get(YRNO_URL, params={"lat": lat, "lon": lon}, headers={"User-Agent": USER_AGENT}, timeout=15).json()
        windy_raw = requests.post(WINDY_URL, json={
            "lat": lat, "lon": lon, "model": "gfs",
            "parameters": ["temperature_2m","wind_speed_10m","wind_from_direction_10m","precipitation","new_snow"],
            "key": WINDY_API_KEY
        }, timeout=20).json()
        yr = parse_yr(yr_raw)
        windy = parse_windy(windy_raw)
    except Exception as e:
        print("Error fetching/parsing:", e)
        return None

    # Create image
    width, height = 1000, 420
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font_b = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
        font_mono = ImageFont.truetype("DejaVuSans.ttf", 13)
    except Exception:
        font_b = ImageFont.load_default()
        font = ImageFont.load_default()
        font_mono = ImageFont.load_default()

    header = f"5-day forecast comparison — {location_name}"
    draw.text((12, 10), header, font=font_b, fill=(0,0,0))

    # Table columns for 5 days
    left = 12
    top = 50
    col_w = (width - 2*left) // 5
    for i in range(5):
        x = left + i*col_w
        draw.rectangle([x, top, x+col_w-6, top+300], outline=(200,200,200))
        # date label
        day = (datetime.now(TIMEZONE) + timedelta(days=i)).date().isoformat()
        draw.text((x+6, top+6), day, font=font_b, fill=(0,0,0))

        yr_v = yr.get(day, {})
        windy_v = windy.get(day, {})

        yy = top+36
        # Yr temp
        t_yr = yr_v.get("temp")
        draw.text((x+6, yy), f"Temp (yr): {t_yr if t_yr is not None else '?'} °C", font=font, fill=(0,0,0))
        yy += 22
        # Wind from Windy
        ws_w = windy_v.get("wind_speed")
        wd_w = deg_to_compass(windy_v.get("wind_dir_deg"))
        draw.text((x+6, yy), f"Wind (windy): {wd_w} {ws_w if ws_w is not None else '?'} m/s", font=font, fill=(0,0,0))
        yy += 22
        # Precip
        p_yr = yr_v.get("precip_mm", 0.0)
        p_w = windy_v.get("precip_mm", 0.0)
        draw.text((x+6, yy), f"Precip yr: {p_yr:.1f} mm", font=font, fill=(0,0,0))
        yy += 18
        draw.text((x+6, yy), f"Precip windy: {p_w:.1f} mm", font=font, fill=(0,0,0))
        yy += 20
        # Snow handling
        snow_texts = []
        temp_for_snow = t_yr if t_yr is not None else windy_v.get("temp")
        if temp_for_snow is not None and temp_for_snow < 0:
            # yr snow estimate in cm = mm * 1.5
            yr_snow_cm = p_yr * 1.5 if p_yr else 0.0
            wind_new_snow = windy_v.get("new_snow_cm", 0.0) or 0.0
            if yr_snow_cm > 0:
                snow_texts.append(f"Yr est snow: {yr_snow_cm:.1f} cm")
            if wind_new_snow > 0:
                snow_texts.append(f"Windy new snow: {wind_new_snow:.1f} cm")
            for s in snow_texts:
                draw.text((x+6, yy), s, font=font, fill=(0,100,0))
                yy += 16

    # signature
    sig = "468Forecasts by Kastonio"
    draw.text((width-220, height-24), sig, font=font_mono, fill=(100,100,100))

    # save to BytesIO and return
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# Sending forecast: scheduled or manual
def send_forecast():
    d = load_data()
    if not d.get("coords") or not d.get("chat_id") or not d.get("enabled", True):
        return
    bio = build_image()
    if bio is None:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    bot.send_photo(chat_id=d["chat_id"], photo=bio, caption=f"Прогноз на 5 дней ({d.get('location_name','')})")

def forecast_command(update: Update, context: CallbackContext):
    d = load_data()
    if not d.get("coords"):
        update.message.reply_text("Координаты не заданы. Админ должен задать через /setcoords.")
        return
    bio = build_image()
    if bio is None:
        update.message.reply_text("Ошибка при получении прогноза.")
        return
    update.message.reply_photo(photo=bio, caption=f"Прогноз на 5 дней ({d.get('location_name','')})")

def schedule_jobs(scheduler=None):
    if scheduler is None:
        scheduler = BackgroundScheduler(timezone=TIMEZONE)
    for hour in [0,6,12,18]:
        scheduler.add_job(send_forecast, 'cron', hour=hour, minute=0)
    scheduler.start()
    return scheduler

def main():
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN not set. Exiting.")
        return
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('setcoords', set_coords, pass_args=True)],
        states={ NAME: [MessageHandler(Filters.text & ~Filters.command, save_location_name)] },
        fallbacks=[]
    )

    dp.add_handler(conv_handler)
    dp.add_handler(CommandHandler('setadmin', set_admin))
    dp.add_handler(CommandHandler('forecast', forecast_command))
    dp.add_handler(CommandHandler('stopforecast', stop_forecast))
    dp.add_handler(CommandHandler('startforecast', start_forecast))
    dp.add_handler(CommandHandler('help', help_command))

    scheduler = schedule_jobs()
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
