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
        rain = f"{rain_val:.1f}" if rain_val else "-"  # прочерк если 0
        snow_val = round(rain_val * 1.5, 1) if (tmax is not None and tmax <= 0) else 0.0
        snow = f"{snow_val:.1f}" if snow_val else "-"  # прочерк если 0

        cells = [label, t_text, wind_txt, rain, snow]

        draw.line((12, y - row_h/2, width - 12, y - row_h/2), fill=(160,160,160), width=1)

        for i, (cx, txt) in enumerate(zip(col_centers, cells)):
            fill_color = (0,0,0)  # черный по умолчанию
            if i == 3 and txt != "-":  # rain
                fill_color = (200, 0, 0)
            elif i == 4 and txt != "-":  # snow
                fill_color = (0, 0, 200)

            if txt == t_text and tmax is not None and tmin is not None:
                max_txt, min_txt = str(tmax), str(tmin)
                sep = "/"
                w_max, _ = text_size(max_txt, font_value)
                w_sep, _ = text_size(sep, font_value)
                w_min, _ = text_size(min_txt, font_value)
                total_w = w_max + w_sep + w_min
                x0 = cx - total_w/2
                draw.text((x0, y), max_txt, font=font_value, fill=temp_color(tmax))
                x0 += w_max
                draw.text((x0, y), sep, font=font_value, fill=(0,0,0))
                x0 += w_sep
                draw.text((x0, y), min_txt, font=font_value, fill=temp_color(tmin))

            elif i == 2:  # wind column → align digits by right edge
                parts = txt.split()
                if len(parts) == 2:
                    dir_txt, speed_txt = parts
                else:
                    dir_txt, speed_txt = "?", txt

                w_dir, _ = text_size(dir_txt, font_value)
                w_speed, _ = text_size(speed_txt, font_value)
                gap = 4

                x_speed_right = cx + 35
                x_speed = x_speed_right - w_speed
                x_dir = x_speed - gap - w_dir

                draw.text((x_dir, y), dir_txt, font=font_value, fill=fill_color)
                draw.text((x_speed, y), speed_txt, font=font_value, fill=fill_color)

            else:
                w, _ = text_size(txt, font_value)
                draw.text((cx - w/2, y), txt, font=font_value, fill=fill_color)

        y += int(row_h * 1.1)

    draw.text((width - 40, height - 22), "yr.no", font=font_value, fill=(80,80,80))

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio
