import os
import io
import requests
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_AGENT = "468ForecastsBot/1.0 (contact@example.com)"
YRNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
TIMEZONE = pytz.timezone("Europe/Moscow")

# --- Coordinates ---
COORDS = {"lat": 59.91, "lon": 10.75}  # Осло
LOCATION_NAME = "Oslo"

# --- Utilities ---
def precip_category_to_text(category):
    """Преобразуем категорию осадков YR.no в текст"""
    if category == "rain":
        return "rain"
    elif category == "snow":
        return "snow"
    elif category == "sleet":
        return "rain_snow"
    elif category == "fog":
        return "cloudy"
    else:
        return "sunny"

def parse_yr(json_data):
    """Парсим YR.no и возвращаем:
       - daily_forecast: ближайшее к полудню значение на 5 дней
       - daily_intervals: утро/день/вечер текущего дня"""
    tz = TIMEZONE
    timeseries = json_data.get("properties", {}).get("timeseries", [])
    now = datetime.now(tz)
    target_dates = [(now + timedelta(days=i)).date() for i in range(5)]
    
    # Сбор всех данных по дате
    candidates = {d: [] for d in target_dates}
    for item in timeseries:
        t_iso = item.get("time")
        if not t_iso: continue
        t = datetime.fromisoformat(t_iso.replace("Z","+00:00")).astimezone(tz)
        date = t.date()
        if date not in candidates: continue

        data = item.get("data", {})
        instant = data.get("instant", {}).get("details", {})
        precip = 0.0
        precip_cat = "sunny"
        if data.get("next_1_hours", {}).get("details"):
            precip = data["next_1_hours"]["details"].get("precipitation_amount", 0.0)
            precip_cat = data["next_1_hours"].get("summary", {}).get("symbol_code", "sunny")
        elif data.get("next_6_hours", {}).get("details"):
            precip = data["next_6_hours"]["details"].get("precipitation_amount", 0.0)
            precip_cat = data["next_6_hours"].get("summary", {}).get("symbol_code", "sunny")

        # Нормализуем категории
        if "snow" in precip_cat:
            cat_text = "snow"
        elif "sleet" in precip_cat:
            cat_text = "rain_snow"
        elif "rain" in precip_cat:
            cat_text = "rain"
        elif "cloudy" in precip_cat:
            cat_text = "cloudy"
        else:
            cat_text = "sunny"

        candidates[date].append({
            "time": t,
            "temp": instant.get("air_temperature"),
            "wind_speed": instant.get("wind_speed"),
            "precip_mm": precip,
            "precip_type": cat_text
        })

    # --- 5-day forecast ---
    daily_forecast = {}
    for d, lst in candidates.items():
        if not lst: continue
        target_dt = datetime.combine(d, datetime.min.time()).replace(tzinfo=tz) + timedelta(hours=12)
        best = min(lst, key=lambda x: abs(x["time"] - target_dt))
        daily_forecast[str(d)] = best

    # --- Detailed today intervals ---
    today = now.date()
    intervals = {"morning": [], "day": [], "evening": []}
    for entry in candidates.get(today, []):
        h = entry["time"].hour
        if 6 <= h < 12:
            intervals["morning"].append(entry)
        elif 12 <= h < 18:
            intervals["day"].append(entry)
        elif 18 <= h < 24:
            intervals["evening"].append(entry)
    daily_intervals = {}
    for k, lst in intervals.items():
        if not lst: continue
        avg_temp = round(sum(x["temp"] for x in lst if x["temp"] is not None)/len(lst))
        total_precip = round(sum(x["precip_mm"] for x in lst),1)
        # Берем преобладающий тип осадков
        types = [x["precip_type"] for x in lst]
        main_type = max(set(types), key=types.count)
        daily_intervals[k] = {"temp": avg_temp, "precip": total_precip, "type": main_type}

    return daily_forecast, daily_intervals

# --- Build image ---
def build_image():
    try:
        resp = requests.get(
            YRNO_URL,
            params={"lat": COORDS["lat"], "lon": COORDS["lon"]},
            headers={"User-Agent": USER_AGENT},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print("Error fetching YR.no:", e)
        return None

    daily_forecast, daily_intervals = parse_yr(data)

    # --- Image ---
    width, height = 800, 450
    img = Image.new("RGB", (width, height), "#f8f8f8")
    draw = ImageDraw.Draw(img)
    try:
        font_b = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except:
        font_b = ImageFont.load_default()
        font = ImageFont.load_default()

    draw.text((10,10), f"Weather forecast — {LOCATION_NAME}", font=font_b, fill=(0,0,0))

    # --- Today intervals ---
    y = 50
    draw.text((10,y), "Today:", font=font_b, fill=(0,0,0))
    y += 25
    for k in ["morning","day","evening"]:
        val = daily_intervals.get(k)
        if val:
            text = f"{k.capitalize()}: {val['temp']}°C, {val['type']}, {val['precip']} mm"
            draw.text((20,y), text, font=font, fill=(0,0,0))
            y += 20

    # --- 5-day forecast ---
    y += 20
    draw.text((10,y), "5-day forecast:", font=font_b, fill=(0,0,0))
    y += 25
    for date_str, val in daily_forecast.items():
        text = f"{date_str}: {val['temp']}°C, {val['precip_type']}, {val['precip_mm']} mm"
        draw.text((20,y), text, font=font, fill=(0,0,0))
        y += 20

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# --- Telegram bot ---
async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bio = build_image()
    if bio is None:
        await update.message.reply_text("Ошибка при получении прогноза.")
        return
    await update.message.reply_photo(photo=bio, caption=f"Forecast — {LOCATION_NAME}")

def main():
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN not set. Exiting.")
        return
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("forecast", forecast_command))
    app.run_polling()

if __name__ == "__main__":
    main()
