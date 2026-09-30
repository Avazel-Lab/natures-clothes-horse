"""
The Ministry of Meteorology, Laundry & Regional Atmospheric Affairs (MoMRAAA):
TRMNL serverless transform.

Input: the Open-Meteo /v1/forecast response (polled by TRMNL).
Output: a small, template-ready dict: current conditions, today/tomorrow,
a washing verdict (for a normal load) and dry-by times for each load size.

The drying model
----------------
Each forecast hour gets a drying rate: the FAO-56 reference
evapotranspiration (ET0, mm/h). That's the standard "how fast does water
evaporate from a surface" figure, combining sunshine, temperature, humidity
and wind. A load of washing is dry once the ET0 it has been exposed to adds
up to DRY_NEED[load]. The verdict is for a normal load; light and heavy loads
get their own dry-by times from the same start.

We calculate ET0 ourselves (FAO-56 eq. 53) rather than using Open-Meteo's,
so the garden can change its inputs: when the house shades the line, direct
sunshine is removed; when the wind blows over the house, wind speed is cut.
With no garden set, it matches Open-Meteo's ET0.

Open-Meteo's radiation, rain and ET0 values describe the hour *before* each
timestamp; temperature, humidity, wind and weather code are instants.

An hour is "wet" (washing must be in) if rain is forecast or likely.
Drying only counts during daylight: evenings bring dew, not drying.

All the tunable numbers are in the constants block below. They're a first
guess, to be calibrated against real washing.

Stdlib only: TRMNL's transform runtime has no pip installs.
"""
import math
from datetime import datetime, timedelta

# ---- Tunables -------------------------------------------------------------

# Cumulative ET0 (mm) a load needs to be dry.
DRY_NEED = {"light": 0.9, "normal": 1.3, "heavy": 1.9}
VERDICT_LOAD = "normal"

# Rain is graded, not a yes/no cliff:
# - an hour is "wet" (the washing must be in) if rain is likely: this chance
#   or more, or this much rain forecast;
WET_PROB = 60       # %
WET_MM = 0.2        # mm in the hour
# - below that, from this chance up, the washing stays out but the hour's
#   drying is scaled by the chance it stays dry (35% -> 65% of the drying);
SLOW_PROB = 20      # %
# - and a window with this much chance of a shower in it is "risky".
RISKY_PROB = 25     # %
# Overnight is a long exposure, so warn about rain tonight from a lower chance.
TONIGHT_PROB = 40   # %
# Finishing less than this long before rain or sunset is "risky".
MARGIN = timedelta(hours=1)
# Below this fraction dry by the end of the window, it's not worth it.
PARTIAL_OK = 0.6

# Drying rate that fills the timeline bar to 100%.
RATE_FULL = 0.45    # mm/h

# ---- Garden ---------------------------------------------------------------
# Up to three obstructions near the line (house, fence, shed, tree...), each
# with a direction *from the line*, a type and a distance, plus a general
# exposure setting. No obstructions and open exposure = no adjustment.

COMPASS8 = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315}
COMPASS_WORDS = {"north": "n", "north-east": "ne", "northeast": "ne", "east": "e",
                 "south-east": "se", "southeast": "se", "south": "s",
                 "south-west": "sw", "southwest": "sw", "west": "w",
                 "north-west": "nw", "northwest": "nw"}

# Obstruction types:
#   name       short name for the screen
#   profile    [(height m, extra depth m)]: a house is its eaves at the near
#              wall plus its ridge set back 4 m
#   half_width metres either side of the line-of-sight centre; None = runs
#              right across (a boundary fence, or a house: neighbouring
#              houses usually continue the building line)
#   shade      share of direct sun it blocks (a tree lets some through)
#   wind       share of the solid-wall wind reduction it gives (porous = less)
OBSTRUCTION_TYPES = {
    "fence":    {"name": "fence",    "profile": [(1.8, 0)],            "half_width": None, "shade": 1.0, "wind": 0.8},
    "hedge":    {"name": "hedge",    "profile": [(3.0, 0)],            "half_width": None, "shade": 0.9, "wind": 0.6},
    "shed":     {"name": "shed",     "profile": [(2.5, 0)],            "half_width": 1.5,  "shade": 1.0, "wind": 1.0},
    "bungalow": {"name": "bungalow", "profile": [(2.7, 0), (5.5, 4)],  "half_width": None,  "shade": 1.0, "wind": 1.0},
    "house2":   {"name": "house",    "profile": [(5.3, 0), (8.5, 4)],  "half_width": None,  "shade": 1.0, "wind": 1.0},
    "house3":   {"name": "house",    "profile": [(7.8, 0), (11.0, 4)], "half_width": None,  "shade": 1.0, "wind": 1.0},
    "tree":     {"name": "tree",     "profile": [(10.0, 0)],           "half_width": 3.0,  "shade": 0.7, "wind": 0.5},
}
# The washing is a band hanging from the line: the line itself is typically
# 5.5-7.5 ft up (take 2.0 m), and items hang about 0.8 m below it on average.
WASHING_TOP = 2.0     # m
WASHING_BOTTOM = 1.2  # m
DEFAULT_DISTANCE = 4

# Wind reduction behind a solid obstruction, by distance in obstruction
# heights, for wind blowing straight over it. Scaled by cos(angle).
WAKE = [(0, 0.6), (1, 0.6), (2, 0.45), (4, 0.25), (8, 0.05), (12, 0.0)]

# General exposure: extra wind reduction from everything not listed.
EXPOSURE = {"open": 1.0, "some": 0.85, "sheltered": 0.7, "enclosed": 0.7}


def obstruction_type(label):
    """Select value or label -> OBSTRUCTION_TYPES key (None for "none")."""
    t = label.strip().lower()
    if t in OBSTRUCTION_TYPES:
        return t
    for words, key in ((("fence", "wall"), "fence"), (("hedge",), "hedge"),
                       (("shed", "garage"), "shed"), (("tree",), "tree"),
                       (("bungalow", "1 storey"), "bungalow"),
                       (("loft", "3 storey", "3-storey", "11 m"), "house3"),
                       (("house", "storey"), "house2")):
        if any(w in t for w in words):
            return key
    return None


def compass_deg(label):
    c = label.strip().lower()
    c = COMPASS_WORDS.get(c, c)
    return COMPASS8.get(c), c


def parse_garden(fields):
    """Plugin settings -> {"obstructions": [...], "exposure": f}, or None.

    TRMNL may pass either a select's value or its label, so accept both.
    Also reads the older single-house settings (garden_faces etc.).
    """
    def pick(key):
        return str(fields.get(key) or "").strip()

    obstructions = []
    for n in (1, 2, 3):
        kind = obstruction_type(pick(f"obstruction_{n}_type"))
        deg, code = compass_deg(pick(f"obstruction_{n}_direction"))
        if not kind or deg is None:
            continue
        digits = "".join(c for c in pick(f"obstruction_{n}_distance") if c.isdigit())
        obstructions.append(dict(OBSTRUCTION_TYPES[kind], azimuth=deg, compass=code.upper(),
                                 distance=max(int(digits) if digits else DEFAULT_DISTANCE, 1)))

    # Older settings: back of house faces X -> a house on the opposite side.
    faces, _ = compass_deg(pick("garden_faces"))
    if not obstructions and faces is not None:
        height = pick("house_height").lower()
        kind = "house3" if "loft" in height or height.startswith("3") else \
               "bungalow" if height.startswith("1") else "house2"
        digits = "".join(c for c in pick("line_distance") if c.isdigit())
        opposite = (faces + 180) % 360
        code = next(k for k, v in COMPASS8.items() if v == opposite)
        obstructions.append(dict(OBSTRUCTION_TYPES[kind], azimuth=opposite, compass=code.upper(),
                                 distance=int(digits) if digits else DEFAULT_DISTANCE))

    exposure = EXPOSURE.get((pick("shelter").lower().split() or ["open"])[0], 1.0)
    if not obstructions and exposure == 1.0:
        return None
    return {"obstructions": obstructions, "exposure": exposure}


def sun_position(when, utc_offset, lat, lon):
    """(elevation, azimuth) in degrees for a local datetime. NOAA formulae."""
    utc = when - timedelta(seconds=utc_offset)
    doy = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    solar_minutes = hour * 60 + eqtime + 4 * lon
    ha = math.radians(solar_minutes / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    elev = 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))
    az = math.degrees(math.atan2(math.sin(ha),
                                 math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180
    return elev, az % 360


def _facing(ob, az):
    """cos of the angle between `az` and the obstruction's direction, or 0 if
    a line of sight towards `az` misses it (behind us, or past its side)."""
    rel = math.radians((az - ob["azimuth"] + 180) % 360 - 180)
    c = math.cos(rel)
    if c <= 0.01:
        return 0.0
    if ob["half_width"] is not None and abs(ob["distance"] * math.tan(rel)) > ob["half_width"]:
        return 0.0
    return c


def band_below(height):
    """Share of the washing band that's below `height`."""
    return min(max((height - WASHING_BOTTOM) / (WASHING_TOP - WASHING_BOTTOM), 0.0), 1.0)


def shadow_height(ob, elev, az):
    """How high up the washing the obstruction's shadow reaches (m), with
    the sun at this elevation and azimuth. 0 if it doesn't shade at all."""
    c = _facing(ob, az)
    if not c:
        return 0.0
    rise = math.tan(math.radians(elev))
    return max(0.0, max(h - (ob["distance"] + extra) / c * rise for h, extra in ob["profile"]))


def shading(elev, az, garden):
    """(share of direct sun on the washing blocked, name of what's blocking it).

    Obstructions can overlap, so take the one covering most, not the sum.
    """
    if elev <= 0:
        return 1.0, None
    blocked, name = 0.0, None
    for ob in garden["obstructions"]:
        share = band_below(shadow_height(ob, elev, az)) * ob["shade"]
        if share > blocked:
            blocked, name = share, ob["name"]
    return blocked, name


def shade_fraction(start, end, geo, garden, steps=4):
    step = (end - start) / steps
    samples = [start + step * (k + 0.5) for k in range(steps)]
    return sum(shading(*sun_position(t, *geo), garden)[0] for t in samples) / steps


def wind_factor(from_deg, garden):
    """Share of the forecast wind that reaches the line."""
    factor = garden["exposure"]
    for ob in garden["obstructions"]:
        c = _facing(ob, from_deg)
        if not c:
            continue
        top = max(h for h, _ in ob["profile"])
        x = ob["distance"] / top
        reduction = 0.0
        for (x0, r0), (x1, r1) in zip(WAKE, WAKE[1:]):
            if x <= x1:
                reduction = r0 + (r1 - r0) * (x - x0) / (x1 - x0)
                break
        # Washing above the obstruction's top catches the wind over it.
        factor *= 1 - reduction * c * ob["wind"] * band_below(top)
    return factor


def et0_hourly(temp, rh, u2, rs_w, elev, z):
    """FAO-56 hourly reference evapotranspiration (mm/h), eq. 53.

    temp C, rh %, u2 wind at 2 m (m/s), rs_w mean shortwave (W/m2),
    elev mid-hour sun elevation (deg), z station elevation (m).
    """
    rs = rs_w * 0.0036                                    # MJ/m2/h
    rso = (0.75 + 2e-5 * z) * 1367 * max(math.sin(math.radians(elev)), 0) * 0.0036
    ratio = min(max(rs / rso, 0.25), 1.0) if rso > 0.05 else 0.6
    es = 0.6108 * math.exp(17.27 * temp / (temp + 237.3))
    ea = es * rh / 100
    delta = 4098 * es / (temp + 237.3) ** 2
    pressure = 101.3 * ((293 - 0.0065 * z) / 293) ** 5.26
    gamma = 0.000665 * pressure
    rnl = 2.043e-10 * (temp + 273.16) ** 4 * (0.34 - 0.14 * math.sqrt(ea)) * (1.35 * ratio - 0.35)
    rn = 0.77 * rs - rnl
    g = (0.1 if rs > 0 else 0.5) * rn
    num = 0.408 * delta * (rn - g) + gamma * 37 / (temp + 273) * u2 * (es - ea)
    return max(0.0, num / (delta + gamma * (1 + 0.34 * u2)))

# ---- Weather codes (WMO, as used by Open-Meteo) --------------------------

# code: (text, day icon, night icon); icons are Weather Icons classes.
WMO = {
    0: ("Clear", "wi-day-sunny", "wi-night-clear"),
    1: ("Mostly clear", "wi-day-sunny-overcast", "wi-night-alt-partly-cloudy"),
    2: ("Partly cloudy", "wi-day-cloudy", "wi-night-alt-cloudy"),
    3: ("Overcast", "wi-cloudy", "wi-cloudy"),
    45: ("Fog", "wi-day-fog", "wi-night-fog"),
    48: ("Freezing fog", "wi-fog", "wi-fog"),
    51: ("Light drizzle", "wi-day-sprinkle", "wi-night-alt-sprinkle"),
    53: ("Drizzle", "wi-sprinkle", "wi-sprinkle"),
    55: ("Heavy drizzle", "wi-sprinkle", "wi-sprinkle"),
    56: ("Freezing drizzle", "wi-sleet", "wi-sleet"),
    57: ("Freezing drizzle", "wi-sleet", "wi-sleet"),
    61: ("Light rain", "wi-day-rain", "wi-night-alt-rain"),
    63: ("Rain", "wi-rain", "wi-rain"),
    65: ("Heavy rain", "wi-rain", "wi-rain"),
    66: ("Freezing rain", "wi-rain-mix", "wi-rain-mix"),
    67: ("Freezing rain", "wi-rain-mix", "wi-rain-mix"),
    71: ("Light snow", "wi-day-snow", "wi-night-alt-snow"),
    73: ("Snow", "wi-snow", "wi-snow"),
    75: ("Heavy snow", "wi-snow", "wi-snow"),
    77: ("Snow grains", "wi-snow", "wi-snow"),
    80: ("Showers", "wi-day-showers", "wi-night-alt-showers"),
    81: ("Showers", "wi-showers", "wi-showers"),
    82: ("Heavy showers", "wi-showers", "wi-showers"),
    85: ("Snow showers", "wi-day-snow", "wi-night-alt-snow"),
    86: ("Snow showers", "wi-snow", "wi-snow"),
    95: ("Thunderstorm", "wi-thunderstorm", "wi-thunderstorm"),
    96: ("Thunderstorm", "wi-storm-showers", "wi-storm-showers"),
    99: ("Thunderstorm", "wi-storm-showers", "wi-storm-showers"),
}

# Icon for each washing verdict.
VERDICT_ICON = {"go": "wi-day-sunny", "risky": "wi-day-cloudy-gusts",
                "wait": "wi-time-3", "tomorrow": "wi-time-9", "no": "wi-umbrella"}

# Short names for rain types, for "Partly cloudy, then showers".
def wet_word(code):
    if code >= 95:
        return "storms"
    if code >= 85:
        return "snow showers"
    if code >= 80:
        return "showers"
    if code >= 71:
        return "snow"
    if code >= 61:
        return "rain"
    return "drizzle"


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


DAY_TEXT = {0: "Sunny", 1: "Mostly sunny"}


def describe(code, is_day=True):
    text, day, night = WMO.get(code, ("Unknown", "wi-na", "wi-na"))
    if is_day:
        text = DAY_TEXT.get(code, text)
    return text, (day if is_day else night)


# Sky types for "Overcast, then sunny": code -> (bucket, text)
def sky(code):
    if code <= 1:
        return 0, "sunny"
    if code == 2:
        return 1, "partly cloudy"
    if code == 3:
        return 2, "overcast"
    return 3, "foggy"


def compass(degrees):
    return COMPASS[round(degrees / 22.5) % 16]


def parse(ts):
    return datetime.fromisoformat(ts)


def hhmm(dt):
    return dt.strftime("%H:%M")


def quarter(dt, up=False):
    """Round to a quarter hour, down (or up)."""
    q = dt.replace(minute=dt.minute // 15 * 15, second=0, microsecond=0)
    return q + timedelta(minutes=15) if up and q < dt else q


def rnd(x):
    return None if x is None else int(round(x))


def shown(x):
    return "–" if x is None else rnd(x)


def fill_gaps(values, interpolate=True):
    """Fill missing (None) values from their neighbours, so a gap in the
    forecast doesn't break the calculations. Numbers are interpolated
    linearly; otherwise (or at the ends) the nearest known value is used.
    A column with nothing in it is returned unchanged."""
    known = [i for i, v in enumerate(values) if v is not None]
    if not known or len(known) == len(values):
        return values
    out = list(values)
    for i, v in enumerate(values):
        if v is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is None or after is None:
            out[i] = values[before if after is None else after]
        elif interpolate:
            out[i] = values[before] + (values[after] - values[before]) * (i - before) / (after - before)
        else:
            out[i] = values[before if i - before <= after - i else after]
    return out


def most_common(codes):
    """Most frequent code; ties go to the higher (worse) code."""
    return max(set(codes), key=lambda c: (codes.count(c), c))


def duration(td):
    """4h20m -> 'about 4½ hours'."""
    halves = round(td.total_seconds() / 1800)
    if halves < 2:
        return "under an hour"
    whole, half = divmod(halves, 2)
    n = f"{whole}½" if half else str(whole)
    return f"about {n} hour{'' if n == '1' else 's'}"


# ---- Hours ---------------------------------------------------------------

MPH = 0.44704           # m/s per mph
U10_TO_U2 = 0.748       # FAO-56 eq. 47: 10 m wind to 2 m


def build_hours(data, garden=None):
    """One dict per hour, for the hour ending at each Open-Meteo timestamp.

    Radiation, rain and ET0 are already "preceding hour" values. Instant
    values (temperature, humidity, wind) are averaged over the two ends.
    With radiation data we calculate ET0 ourselves (and apply the garden);
    without it we fall back to Open-Meteo's ET0.
    """
    hourly = data["hourly"]
    n = len(hourly["time"])

    def col(name, interpolate=True):
        return fill_gaps(hourly.get(name) or [None] * n, interpolate)

    def mid(values, i, j):
        a, b = values[j], values[i]
        return b if a is None else a if b is None else (a + b) / 2

    temp, rh, wind = col("temperature_2m"), col("relative_humidity_2m"), col("wind_speed_10m")
    sw, direct = col("shortwave_radiation"), col("direct_radiation")
    # Directions and codes can't be averaged: take the nearest known value.
    wdir, code, is_day = (col(k, interpolate=False)
                          for k in ("wind_direction_10m", "weather_code", "is_day"))
    own_et0 = all(v is not None for v in (sw[0], temp[0], rh[0]))
    geo = (data.get("utc_offset_seconds", 0), data.get("latitude", 0), data.get("longitude", 0))
    z = data.get("elevation") or 0

    hours = []
    for i, ts in enumerate(hourly["time"]):
        j = max(i - 1, 0)
        end = parse(ts)
        start = end - timedelta(hours=1)
        prob = hourly["precipitation_probability"][i] or 0
        mm = hourly["precipitation"][i] or 0
        wind_mph = open_wind = mid(wind, i, j) or 0
        shade = 0.0

        if own_et0:
            t, h = mid(temp, i, j), mid(rh, i, j)
            elev, _ = sun_position(start + timedelta(minutes=30), *geo)
            # Open ground (the lawn), then the line with the garden applied.
            open_rate = et0_hourly(t, h, wind_mph * MPH * U10_TO_U2, sw[i] or 0, elev, z)
            rate = open_rate
            if garden:
                shade = shade_fraction(start, end, geo, garden)
                if wdir[i] is not None:
                    wind_mph *= wind_factor(wdir[i], garden)
                rs = (sw[i] or 0) - (direct[i] or 0) * shade
                rate = et0_hourly(t, h, wind_mph * MPH * U10_TO_U2, rs, elev, z)
        else:
            rate = open_rate = col("et0_fao_evapotranspiration")[i] or 0

        hours.append({
            "start": start,
            "rate": rate,
            "open_rate": open_rate,
            "prob": prob,
            "mm": mm,
            "day": bool(is_day[j] or is_day[i]),
            "wet": prob >= WET_PROB or mm >= WET_MM,
            "rh": mid(rh, i, j),
            "temp": temp[j],
            "wind": wind_mph,
            "open_wind": open_wind,
            "code": code[j],
            "shade": shade,
        })
    return hours


def sun_times(daily):
    """{date: (sunrise, sunset)}"""
    return {
        parse(d).date(): (parse(r), parse(s))
        for d, r, s in zip(daily["time"], daily["sunrise"], daily["sunset"])
    }


def daylight(hours, rise, sset):
    first = rise.replace(minute=0)
    return [h for h in hours if first <= h["start"] < sset]


def day_summary(hours, rise, sset, fallback_code):
    """Conditions for the daylight part of a day.

    Open-Meteo's daily weather code is the worst weather in the whole 24h,
    so a sunny day with a drizzly night reads "Light drizzle". Instead: the
    usual sky, plus any rain that lasts two hours or more.
    """
    day = [h for h in daylight(hours, rise, sset) if h["code"] is not None]
    if not day:
        return describe(fallback_code)
    wet = [h for h in day if h["code"] >= 51]
    dry = [h["code"] for h in day if h["code"] < 51]
    noon = rise.replace(hour=12, minute=0)
    if len(wet) < 2 and dry:
        am = [sky(h["code"]) for h in day if h["code"] < 51 and h["start"] < noon]
        pm = [sky(h["code"]) for h in day if h["code"] < 51 and h["start"] >= noon]
        text, icon = describe(most_common(dry))
        if am and pm:
            am_sky, pm_sky = most_common(am), most_common(pm)
            if abs(am_sky[0] - pm_sky[0]) >= 2:  # e.g. overcast -> sunny, not a slight change
                text = f"{am_sky[1].capitalize()}, then {pm_sky[1]}"
        return text, icon

    wet_code = most_common([h["code"] for h in wet])
    if not dry or len(wet) >= 0.6 * len(day):
        return describe(wet_code)

    text, icon = describe(most_common(dry))
    if all(h["start"] < noon for h in wet):
        text = f"{text}, early {wet_word(wet_code)}"
    elif all(h["start"] >= noon for h in wet):
        text = f"{text}, then {wet_word(wet_code)}"
    else:
        text = f"{text}, {wet_word(wet_code)} at times"
    if len(wet) >= 3:
        icon = describe(wet_code)[1]
    return text, icon


# ---- Mowing ---------------------------------------------------------------
# The lawn is "wet" with some amount of water (mm-ish) that evaporation has
# to clear before it's worth mowing. Rain soaks it (more rain, softer ground,
# longer to recover); humid nights add dew. Open-ground ET0 dries it.
MOW_RAIN_BASE = 0.4     # water left by any rain, mm of ET0 to clear
MOW_RAIN_PER_MM = 0.15  # extra per mm of rain (soggy ground)
MOW_RAIN_MAX = 2.5      # cap: a very wet day takes the next day to recover
MOW_DEW = 0.3           # dew on a humid night
MOW_DEW_RH = 90         # % RH overnight that means dew by morning
MOW_RAIN_PROB = 50      # % chance that counts as rain for the lawn
MOW_MIN = timedelta(hours=2)          # shortest window worth showing

# Evening dew: grass cools as the sun drops and gets damp once the air is
# humid enough, sooner when it's calm and clear. We look at the last two
# hours before sunset and stop at the first one that crosses the humidity
# threshold for its wind and sky; otherwise mowing runs until sunset.
DEW_LOOKBACK = timedelta(hours=2)


def dew_rh(wind_mph, clear):
    """Humidity (%) at which evening dew forms, for this wind and sky."""
    if wind_mph < 5:
        return 75 if clear else 82
    if wind_mph < 10:
        return 82 if clear else 88
    return 92


def evening_stop(hours, sset):
    """When evening dew is likely to end mowing: sunset, or earlier."""
    for h in hours:
        if sset - DEW_LOOKBACK <= h["start"] < sset:
            clear = h["code"] is not None and h["code"] <= 1
            if (h["rh"] or 0) >= dew_rh(h["open_wind"], clear):
                return h["start"]
    return sset


def mow_window(hours, rise, sset, after):
    """Longest spell on this day when the grass is dry enough to mow.

    Returns (start, end) or None. Spells before `after` are ignored.
    """
    water = MOW_DEW
    best, cur = None, None
    stop = evening_stop(hours, sset)
    for h in hours:
        if h["start"] >= stop:
            break
        rained = h["mm"] >= 0.1 or h["prob"] >= MOW_RAIN_PROB
        dry_at_start = water <= 0
        if rained:
            water = max(water, min(MOW_RAIN_BASE + MOW_RAIN_PER_MM * h["mm"], MOW_RAIN_MAX))
        elif not h["day"] and (h["rh"] or 0) >= MOW_DEW_RH:
            water = max(water, MOW_DEW)
        elif h["day"]:
            water = max(0.0, water - h["open_rate"])

        mowable = (not rained and dry_at_start and h["day"]
                   and h["start"] >= rise.replace(minute=0) and h["start"] + timedelta(hours=1) > after)
        if mowable:
            cur = (cur[0], h["start"] + timedelta(hours=1)) if cur else (max(h["start"], after), h["start"] + timedelta(hours=1))
            if not best or cur[1] - cur[0] > best[1] - best[0]:
                best = cur
        else:
            cur = None
    if best:
        best = (best[0], min(best[1], stop))
    return best if best and best[1] - best[0] >= MOW_MIN else None


def night_low(hours, after, until):
    """Lowest temperature from `after` to `until` (sunset to next sunrise)."""
    temps = [h["temp"] for h in hours
             if h["temp"] is not None and after <= h["start"] + timedelta(hours=1) and h["start"] <= until]
    return min(temps) if temps else None


# ---- Washing engine ------------------------------------------------------

def simulate(hours, start, need, sunset):
    """Hang washing out at `start` and let it dry.

    Returns a dict with:
      dry_at   when it's dry (None if it isn't before it has to come in)
      stop_at  when it has to come in: first wet hour, or sunset
      stop_why "rain" or "sunset"
      done     fraction dry when it comes in (or 1.0)
      max_prob highest rain chance while it's out (max_prob_at: its hours)
    """
    got = 0.0
    probs = []  # (chance, hour start) while it's out
    humid = []
    winds = []
    for h in hours:
        h_end = h["start"] + timedelta(hours=1)
        if h_end <= start:
            continue
        if h["start"] >= sunset:
            return _result(None, sunset, "sunset", got / need, probs, humid, winds)
        if h["wet"]:
            stop = max(h["start"], start)
            return _result(None, stop, "rain", got / need, probs, humid, winds)

        # Only count the part of the hour that's after `start` and before sunset.
        seg_start = max(h["start"], start)
        seg_end = min(h_end, sunset)
        frac = (seg_end - seg_start).total_seconds() / 3600
        rate = h["rate"] if h["day"] else 0.0
        if h["prob"] >= SLOW_PROB:
            rate *= 1 - h["prob"] / 100
        probs.append((h["prob"], h["start"]))
        humid.append(h["rh"])
        winds.append(h["wind"])

        if rate > 0 and got + rate * frac >= need:
            dry_at = seg_start + timedelta(hours=(need - got) / rate)
            # Look ahead: when would it have to come in anyway?
            stop, why = next_stop(hours, dry_at, sunset)
            return _result(dry_at, stop, why, 1.0, probs, humid, winds)
        got += rate * frac
    return _result(None, sunset, "sunset", got / need, probs, humid, winds)


def next_stop(hours, after, sunset):
    for h in hours:
        if h["start"] + timedelta(hours=1) <= after:
            continue
        if h["start"] >= sunset:
            break
        if h["wet"]:
            return max(h["start"], after), "rain"
    return sunset, "sunset"


def _result(dry_at, stop_at, why, done, probs, humid, winds):
    max_prob = max((p for p, _ in probs), default=0)
    return {
        "dry_at": dry_at,
        "stop_at": stop_at,
        "stop_why": why,
        "done": min(done, 1.0),
        "max_prob": max_prob,
        "max_prob_at": [t for p, t in probs if p == max_prob] if max_prob else [],
        "avg_rh": sum(humid) / len(humid) if humid else None,
        "avg_wind": sum(winds) / len(winds) if winds else None,
    }


def why_text(sim):
    """Short explanation of the drying conditions."""
    bits = []
    rh, wind = sim["avg_rh"], sim["avg_wind"]
    if rh is not None:
        bits.append("dry air" if rh < 60 else "humid" if rh > 80 else None)
    if wind is not None:
        bits.append("breezy" if wind >= 8 else "still air" if wind < 4 else None)
    if sim["max_prob"] < 15 and sim["stop_why"] != "rain":
        bits.append("low rain risk")  # higher chances are in the headline or chart
    return ", ".join(b for b in bits if b)


def first_good_start(hours, after, need, sunset):
    """Earliest whole hour from `after` where a load would fully dry."""
    for h in hours:
        if h["start"] < after or h["start"] >= sunset or not h["day"] or h["wet"]:
            continue
        sim = simulate(hours, h["start"], need, sunset)
        if sim["dry_at"]:
            return h["start"], sim
    return None, None


def is_risky(sim):
    return (sim["max_prob"] >= RISKY_PROB
            or sim["stop_at"] - sim["dry_at"] < MARGIN)


# Each reason is (code, words). Logic keys off the code; only the screen
# sees the words, so rewording never changes behaviour.

def wait_reason(hours, now, start, raining_now):
    """Why wait until `start`? Describe the rain in between."""
    wet = [h for h in hours if h["wet"] and now < h["start"] + timedelta(hours=1) and h["start"] < start]
    if not wet:
        return "better_later", "Better drying later"
    until = hhmm(wet[-1]["start"] + timedelta(hours=1))
    if raining_now:
        return "clearing", f"Should clear by {until}"
    if wet[0]["start"] <= now:
        return "rain_until", f"Rain likely until {until}"
    return "rain_between", f"Rain {hhmm(wet[0]['start'])}–{until}, then dry"


def risky_reason(sim):
    if sim["stop_at"] - sim["dry_at"] < MARGIN:
        if sim["stop_why"] == "rain":
            return "rain_close", f"Rain due {hhmm(sim['stop_at'])}, cutting it fine"
        return "sunset_close", "Only just dry by sunset"
    at = sim["max_prob_at"]
    when = f" at {hhmm(at[0])}" if len(at) == 1 else ""
    return "shower_chance", f"{rnd(sim['max_prob'])}% chance of a shower{when}"


def not_today_reason(today_sim, now, sunset, raining_now):
    if now >= sunset:
        return "too_late", "Too late for today"
    if today_sim and today_sim["stop_why"] == "rain":
        if raining_now or today_sim["stop_at"] <= now:
            return "wet_until_dark", "Wet on and off until dark"
        return "rain_from", f"Rain from {hhmm(today_sim['stop_at'])}"
    return "not_enough_time", "Not enough drying time left today"


def washing(hours, now, suns, need, raining_now):
    """The verdict for one load size."""
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    sunrise, sunset = suns[today]
    sim = None

    if now < sunset:
        out = max(now, sunrise)
        sim = simulate(hours, out, need, sunset)
        if sim["dry_at"] and not raining_now:
            if is_risky(sim):
                return verdict("risky", "Risky, but go", *risky_reason(sim),
                               start=out, sim=sim)
            head = "Put it out now" if now >= sunrise else f"Put it out at {hhmm(sunrise)}"
            return verdict("go", head, "drying", f"Dry in {duration(sim['dry_at'] - out)}",
                           start=out, sim=sim)

        start, later = first_good_start(hours, now, need, sunset)
        if start:
            return verdict("wait", "Raining now" if raining_now else "Wait",
                           *wait_reason(hours, now, start, raining_now),
                           start=start, sim=later)

        if not raining_now and sim["done"] >= PARTIAL_OK and sim["stop_at"] > now:
            return verdict("risky", "Partly dry at best", "partial",
                           f"About {rnd(sim['done'] * 100)}% dry by {hhmm(sim['stop_at'])}",
                           start=out, sim=sim, partial=True)

    # Nothing today: try tomorrow.
    if tomorrow in suns:
        t_rise, t_set = suns[tomorrow]
        start, t_sim = first_good_start(hours, t_rise, need, t_set)
        if start:
            v = verdict("tomorrow", "Not today",
                        *not_today_reason(sim, now, sunset, raining_now),
                        start=start, sim=t_sim)
            v["when"] = "tomorrow"
            return v

    return verdict("no", "Dry it indoors", "no_window", "No drying window today or tomorrow")


def verdict(code, headline, reason, detail, start=None, sim=None, partial=False):
    v = {"code": code, "reason": reason, "icon": VERDICT_ICON[code], "when": "today",
         "headline": headline, "detail": detail,
         "out_at": None, "dry_by": None, "in_by": None, "in_why": None,
         "why": None, "window": None}
    if sim:
        v["out_at"] = hhmm(start)
        v["dry_by"] = None if partial else hhmm(sim["dry_at"])
        v["in_by"] = hhmm(sim["stop_at"])
        v["in_why"] = sim["stop_why"]
        v["why"] = why_text(sim)
        # Window on the timeline: from out_at to dry_by (or in_by if partial).
        end = sim["stop_at"] if partial else sim["dry_at"]
        v["window"] = {"start": start, "end": end}
    return v


# ---- Timeline ------------------------------------------------------------

def load_times(hours, out, sunset):
    """Dry-by time for each load size, all hung out at `out`."""
    loads = []
    for name, need in DRY_NEED.items():
        sim = simulate(hours, out, need, sunset)
        loads.append({"name": name.capitalize(),
                      "dry_by": hhmm(sim["dry_at"]) if sim["dry_at"] else None})
    return loads


def plan_row(name, hours, start, sim, sunset):
    """One row of the Today/Tomorrow table for a given start."""
    return {"name": name, "out": hhmm(start), "loads": load_times(hours, start, sunset),
            "in_by": hhmm(sim["stop_at"]), "in_why": sim["stop_why"], "note": None}


def day_plan(name, hours, rise, sset):
    """Earliest start on a day that dries a normal load (or failing that, a
    light one), with every load's dry-by time. Or a note saying why not."""
    for load in (VERDICT_LOAD, "light"):
        start, sim = first_good_start(hours, rise, DRY_NEED[load], sset)
        if start:
            return plan_row(name, hours, start, sim, sset)
    return {"name": name, "out": None, "loads": [], "in_by": None, "in_why": None,
            "note": no_drying_note(hours, rise, sset)}


def no_drying_note(hours, after, sset):
    wet = [h for h in hours if after <= h["start"] + timedelta(hours=1) and h["start"] < sset and h["wet"]]
    return "Rain most of the day" if len(wet) >= 4 else "Too little drying"


def rain_tonight(hours, sunset, next_sunrise):
    """First wet hour between sunset and the next sunrise, as a warning."""
    for h in hours:
        if sunset <= h["start"] < next_sunrise and (h["prob"] >= TONIGHT_PROB or h["mm"] >= WET_MM):
            return f"Rain from {hhmm(h['start'])} tonight, don't leave it out"
    return None


CHART_HOURS = 24


def timeline(hours, now, win):
    """One bar per hour for the next 24 hours, starting with the current one.

    Bar height is drying strength (zero at night); rain chance rides on top.
    """
    first = now.replace(minute=0, second=0, microsecond=0)
    bars = []
    for h in hours:
        if not first <= h["start"] < first + timedelta(hours=CHART_HOURS):
            continue
        in_win = bool(win) and win["start"] < h["start"] + timedelta(hours=1) and h["start"] < win["end"]
        bars.append({
            "label": h["start"].strftime("%H"),
            "tick": h["start"].hour % 3 == 0 or h["start"] == first,
            "pct": min(100, rnd(h["rate"] / RATE_FULL * 100)) if h["day"] else 0,
            "prob": rnd(h["prob"]),
            "wet": h["wet"],
            "win": in_win,
            "night": not h["day"],
            "now": h["start"] == first,
            "night_label": False,
        })
    # Label the middle of each run of night hours.
    run = []
    for b in bars + [{"night": False}]:
        if b["night"]:
            run.append(b)
        elif run:
            run[len(run) // 2]["night_label"] = True
            run = []
    return bars


def shade_text(rise, sset, geo, garden):
    """When obstructions shade the line during the day, in words."""
    step = timedelta(minutes=5)
    t, samples = rise, []
    while t < sset:
        elev, az = sun_position(t, *geo)
        if elev >= 3:  # ignore the sun skimming the horizon
            blocked, name = shading(elev, az, garden)
            samples.append((t, name if blocked >= 0.5 else None))
        t += step
    if not samples or not any(n for _, n in samples):
        return None
    if all(n for _, n in samples):
        return "line shaded all day"
    parts = []
    if samples[0][1]:
        end = next(t for t, n in samples if not n)
        parts.append(f"{samples[0][1]} shade until {hhmm(end)}")
    for (_, n0), (t1, n1) in zip(samples, samples[1:]):
        if n1 and not n0:
            parts.append(f"{n1} shade from {hhmm(t1)}")
    return ", ".join(parts)


def garden_label(garden):
    """Short summary for the title bar, e.g. "NW house 4 m, SW fence 2 m"."""
    if not garden:
        return None
    parts = [f"{ob['compass']} {ob['name']} {ob['distance']} m" for ob in garden["obstructions"]]
    if garden["exposure"] < 1:
        parts.append("sheltered")
    return ", ".join(parts)


# ---- Entry point ---------------------------------------------------------

def run(input):
    if "hourly" not in input:
        reason = input.get("reason") or "No forecast data"
        return {"error": reason}

    settings = (input.get("trmnl") or {}).get("plugin_settings") or {}
    garden = parse_garden(settings.get("custom_fields_values") or {})
    geo = (input.get("utc_offset_seconds", 0), input.get("latitude", 0), input.get("longitude", 0))

    cur = input["current"]
    now = parse(cur["time"])
    is_day = bool(cur["is_day"])
    hours = build_hours(input, garden)
    suns = sun_times(input["daily"])
    daily = input["daily"]

    days = []
    first = next((k for k, d in enumerate(daily["time"]) if parse(d).date() == now.date()), 0)
    for i in range(first, min(first + 2, len(daily["time"]))):
        date = parse(daily["time"][i]).date()
        rise, sset = suns[date]
        # "lo" is the coming night's low (sunset to next sunrise), which is
        # what you plan blankets and windows around, not the calendar day's.
        nxt = suns.get(date + timedelta(days=1))
        lo = night_low(hours, max(sset, now), nxt[0]) if nxt else None
        if lo is None:
            lo = daily["temperature_2m_min"][i]
        text, icon = day_summary(hours, rise, sset, daily["weather_code"][i])
        day_probs = [h["prob"] for h in daylight(hours, rise, sset)]
        rain = max(day_probs) if day_probs else daily["precipitation_probability_max"][i] or 0
        mow = mow_window(hours, rise, sset, max(now, rise))
        if mow:
            mow_text = f"Mow {hhmm(quarter(mow[0], up=True))}–{hhmm(quarter(mow[1]))}"
        elif now + MOW_MIN > evening_stop(hours, sset):
            mow_text = "Too late to mow"
        else:
            mow_text = "Too wet to mow"
        days.append({
            "name": "Today" if i == first else "Tomorrow",
            "text": text,
            "icon": icon,
            "hi": rnd(daily["temperature_2m_max"][i]),
            "lo": rnd(lo),
            "rain": rnd(rain),
            "sunrise": hhmm(rise),
            "sunset": hhmm(sset),
            "mow": mow_text,
        })

    raining_now = (cur.get("precipitation") or 0) > 0
    wash = washing(hours, now, suns, DRY_NEED[VERDICT_LOAD], raining_now)

    win = wash["window"]
    wash["loads"] = []
    wash["tonight"] = None
    if win:
        out_day = win["start"].date()
        sunset = suns[out_day][1]
        wash["loads"] = load_times(hours, win["start"], sunset)
        if out_day == now.date() and out_day + timedelta(days=1) in suns:
            wash["tonight"] = rain_tonight(hours, sunset, suns[out_day + timedelta(days=1)][0])
        if win["start"] == now:
            wash["out_at"] = "Now"
        if garden and wash["why"]:
            shade = shade_text(*suns[out_day], geo, garden)
            if shade:
                wash["why"] = f"{wash['why']}, {shade}"
        if out_day != now.date() and wash["why"]:
            wash["why"] = f"Tomorrow: {wash['why']}"

    # Today and tomorrow rows for the table under the verdict. A row the
    # verdict already covers uses the verdict's times.
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    rows = {}
    if win:
        rows[win["start"].date()] = {"name": "Today" if win["start"].date() == today else "Tomorrow",
                                     "out": wash["out_at"], "loads": wash["loads"],
                                     "in_by": wash["in_by"], "in_why": wash["in_why"], "note": None}
    if today not in rows:
        rise, sunset = suns[today]
        out = max(now, rise)
        if now < sunset and not raining_now:
            row = plan_row("Today", hours, out, simulate(hours, out, DRY_NEED[VERDICT_LOAD], sunset), sunset)
            if any(l["dry_by"] for l in row["loads"]):
                row["out"] = "Now" if out == now else row["out"]
                rows[today] = row
    if today not in rows:
        sunset = suns[today][1]
        if now + MARGIN >= sunset or wash["reason"] in ("too_late", "not_enough_time"):
            note = "Too late today"
        elif wash["reason"] == "rain_from":
            note = "Nothing dries before the rain"
        else:
            note = no_drying_note(hours, now, sunset)
        rows[today] = {"name": "Today", "out": None, "loads": [], "in_by": None, "in_why": None,
                       "note": note}
    if tomorrow not in rows and tomorrow in suns:
        rows[tomorrow] = day_plan("Tomorrow", hours, *suns[tomorrow])
    wash["plan"] = [rows[d] for d in sorted(rows)]

    bars = timeline(hours, now, wash["window"])
    wash["window"] = None  # datetimes aren't JSON; the bars carry it now

    text, icon = describe(cur.get("weather_code"), is_day)
    for d in days:
        d["icon"] = icon_svg(d["icon"])
    wash["icon"] = icon_svg(wash["icon"])
    return {
        "now": {
            "time": hhmm(now),
            "text": "Raining" if raining_now and (cur.get("weather_code") or 0) < 51 else text,
            "icon": icon_svg(icon),
            # A dash for anything missing, rather than a bare "°".
            "temp": shown(cur.get("temperature_2m")),
            "feels": shown(cur.get("apparent_temperature")),
            "humidity": shown(cur.get("relative_humidity_2m")),
            "wind": shown(cur.get("wind_speed_10m")),
            "gust": shown(cur.get("wind_gusts_10m")),
            "wind_dir": compass(cur["wind_direction_10m"]) if cur.get("wind_direction_10m") is not None else "",
        },
        "today": days[0],
        "tomorrow": days[1] if len(days) > 1 else None,
        "wash": wash,
        "garden": garden_label(garden),
        "chart": {"bars": bars},
        "icons": {"sunrise": icon_svg("wi-sunrise"), "sunset": icon_svg("wi-sunset")},
    }

# ---- Icons -----------------------------------------------------------------
# Weather Icons by Erik Flowers (https://erikflowers.github.io/weather-icons/),
# SIL Open Font License 1.1. Path data from the project's svg/ folder, on a
# 30x30 grid, embedded so rendering never depends on a CDN. Only the icons
# the plugin uses are included.

ICONS = {
    "wi-cloudy": "M3.89,17.6c0-0.99,0.31-1.88,0.93-2.65s1.41-1.27,2.38-1.49c0.26-1.17,0.85-2.14,1.78-2.88c0.93-0.75,2-1.12,3.22-1.12 c1.18,0,2.24,0.36,3.16,1.09c0.93,0.73,1.53,1.66,1.8,2.8h0.27c1.18,0,2.18,0.41,3.01,1.24s1.25,1.83,1.25,3 c0,1.18-0.42,2.18-1.25,3.01s-1.83,1.25-3.01,1.25H8.16c-0.58,0-1.13-0.11-1.65-0.34S5.52,21,5.14,20.62 c-0.38-0.38-0.68-0.84-0.91-1.36S3.89,18.17,3.89,17.6z M5.34,17.6c0,0.76,0.28,1.42,0.82,1.96s1.21,0.82,1.99,0.82h9.28 c0.77,0,1.44-0.27,1.99-0.82c0.55-0.55,0.83-1.2,0.83-1.96c0-0.76-0.27-1.42-0.83-1.96c-0.55-0.54-1.21-0.82-1.99-0.82h-1.39 c-0.1,0-0.15-0.05-0.15-0.15l-0.07-0.49c-0.1-0.94-0.5-1.73-1.19-2.35s-1.51-0.93-2.45-0.93c-0.94,0-1.76,0.31-2.46,0.94 c-0.7,0.62-1.09,1.41-1.18,2.34l-0.07,0.42c0,0.1-0.05,0.15-0.16,0.15l-0.45,0.07c-0.72,0.06-1.32,0.36-1.81,0.89 C5.59,16.24,5.34,16.87,5.34,17.6z M14.19,8.88c-0.1,0.09-0.08,0.16,0.07,0.21c0.43,0.19,0.79,0.37,1.08,0.55 c0.11,0.03,0.19,0.02,0.22-0.03c0.61-0.57,1.31-0.86,2.12-0.86c0.81,0,1.5,0.27,2.1,0.81c0.59,0.54,0.92,1.21,0.99,2l0.09,0.64h1.42 c0.65,0,1.21,0.23,1.68,0.7c0.47,0.47,0.7,1.02,0.7,1.66c0,0.6-0.21,1.12-0.62,1.57s-0.92,0.7-1.53,0.77c-0.1,0-0.15,0.05-0.15,0.16 v1.13c0,0.11,0.05,0.16,0.15,0.16c1.01-0.06,1.86-0.46,2.55-1.19s1.04-1.6,1.04-2.6c0-1.06-0.37-1.96-1.12-2.7 c-0.75-0.75-1.65-1.12-2.7-1.12h-0.15c-0.26-1-0.81-1.82-1.65-2.47c-0.83-0.65-1.77-0.97-2.8-0.97C16.28,7.29,15.11,7.82,14.19,8.88 z",
    "wi-day-cloudy": "M1.56,16.9c0,0.9,0.22,1.73,0.66,2.49s1.04,1.36,1.8,1.8c0.76,0.44,1.58,0.66,2.47,0.66h10.83c0.89,0,1.72-0.22,2.48-0.66 c0.76-0.44,1.37-1.04,1.81-1.8c0.44-0.76,0.67-1.59,0.67-2.49c0-0.66-0.14-1.33-0.42-2C22.62,13.98,23,12.87,23,11.6 c0-0.71-0.14-1.39-0.41-2.04c-0.27-0.65-0.65-1.2-1.12-1.67C21,7.42,20.45,7.04,19.8,6.77c-0.65-0.28-1.33-0.41-2.04-0.41 c-1.48,0-2.77,0.58-3.88,1.74c-0.77-0.44-1.67-0.66-2.7-0.66c-1.41,0-2.65,0.44-3.73,1.31c-1.08,0.87-1.78,1.99-2.08,3.35 c-1.12,0.26-2.03,0.83-2.74,1.73S1.56,15.75,1.56,16.9z M3.27,16.9c0-0.84,0.28-1.56,0.84-2.17c0.56-0.61,1.26-0.96,2.1-1.06 l0.5-0.03c0.12,0,0.19-0.06,0.19-0.18l0.07-0.54c0.14-1.08,0.61-1.99,1.41-2.71c0.8-0.73,1.74-1.09,2.81-1.09 c1.1,0,2.06,0.37,2.87,1.1c0.82,0.73,1.27,1.63,1.37,2.71l0.07,0.58c0.02,0.11,0.09,0.17,0.21,0.17h1.61c0.88,0,1.64,0.32,2.28,0.96 c0.64,0.64,0.96,1.39,0.96,2.27c0,0.91-0.32,1.68-0.95,2.32c-0.63,0.64-1.4,0.96-2.28,0.96H6.49c-0.88,0-1.63-0.32-2.27-0.97 C3.59,18.57,3.27,17.8,3.27,16.9z M9.97,4.63c0,0.24,0.08,0.45,0.24,0.63l0.66,0.64c0.25,0.19,0.46,0.27,0.64,0.25 c0.21,0,0.39-0.09,0.55-0.26s0.24-0.38,0.24-0.62c0-0.24-0.09-0.44-0.26-0.59l-0.59-0.66c-0.18-0.16-0.38-0.24-0.61-0.24 c-0.24,0-0.45,0.08-0.62,0.25C10.05,4.19,9.97,4.39,9.97,4.63z M15.31,9.06c0.69-0.67,1.51-1,2.45-1c0.99,0,1.83,0.34,2.52,1.03 c0.69,0.69,1.04,1.52,1.04,2.51c0,0.62-0.17,1.24-0.51,1.84C19.84,12.48,18.68,12,17.32,12H17C16.75,10.91,16.19,9.93,15.31,9.06z M16.94,3.78c0,0.26,0.08,0.46,0.23,0.62s0.35,0.23,0.59,0.23c0.26,0,0.46-0.08,0.62-0.23c0.16-0.16,0.23-0.36,0.23-0.62V1.73 c0-0.24-0.08-0.43-0.24-0.59s-0.36-0.23-0.61-0.23c-0.24,0-0.43,0.08-0.59,0.23s-0.23,0.35-0.23,0.59V3.78z M22.46,6.07 c0,0.26,0.07,0.46,0.22,0.62c0.21,0.16,0.42,0.24,0.62,0.24c0.18,0,0.38-0.08,0.59-0.24l1.43-1.43c0.16-0.18,0.24-0.39,0.24-0.64 c0-0.24-0.08-0.44-0.24-0.6c-0.16-0.16-0.36-0.24-0.59-0.24c-0.24,0-0.43,0.08-0.58,0.24l-1.47,1.43 C22.53,5.64,22.46,5.84,22.46,6.07z M23.25,17.91c0,0.24,0.08,0.45,0.25,0.63l0.65,0.63c0.15,0.16,0.34,0.24,0.58,0.24 s0.44-0.08,0.6-0.25c0.16-0.17,0.24-0.37,0.24-0.62c0-0.22-0.08-0.42-0.24-0.58l-0.65-0.65c-0.16-0.16-0.35-0.24-0.57-0.24 c-0.24,0-0.44,0.08-0.6,0.24C23.34,17.47,23.25,17.67,23.25,17.91z M24.72,11.6c0,0.23,0.09,0.42,0.26,0.58 c0.16,0.16,0.37,0.24,0.61,0.24h2.04c0.23,0,0.42-0.08,0.58-0.23s0.23-0.35,0.23-0.59c0-0.24-0.08-0.44-0.23-0.6 s-0.35-0.25-0.58-0.25h-2.04c-0.24,0-0.44,0.08-0.61,0.25C24.8,11.17,24.72,11.37,24.72,11.6z",
    "wi-day-cloudy-gusts": "M0.35,21.01c0,0.24,0.09,0.44,0.27,0.6c0.17,0.17,0.37,0.25,0.61,0.25h5.88c0.26,0,0.48,0.09,0.68,0.28 c0.2,0.19,0.3,0.42,0.3,0.68s-0.1,0.5-0.3,0.69c-0.2,0.19-0.42,0.29-0.68,0.29c-0.26,0-0.48-0.1-0.68-0.3 c-0.18-0.16-0.38-0.24-0.61-0.24c-0.24,0-0.44,0.08-0.6,0.24c-0.16,0.16-0.24,0.36-0.24,0.6c0,0.24,0.08,0.44,0.24,0.6 c0.53,0.53,1.16,0.8,1.89,0.8c0.74,0,1.37-0.26,1.88-0.78c0.52-0.52,0.78-1.15,0.78-1.89s-0.26-1.37-0.78-1.89 c-0.52-0.53-1.15-0.79-1.88-0.79H1.23c-0.24,0-0.44,0.09-0.62,0.26S0.35,20.78,0.35,21.01z M0.35,18c0,0.22,0.09,0.41,0.27,0.57 c0.17,0.17,0.37,0.25,0.61,0.25H12.2c0.74,0,1.37-0.26,1.89-0.78c0.52-0.52,0.78-1.15,0.78-1.89c0-0.74-0.26-1.36-0.78-1.88 c-0.52-0.51-1.15-0.77-1.89-0.77c-0.76,0-1.38,0.25-1.88,0.76c-0.16,0.16-0.23,0.37-0.23,0.61c0,0.24,0.08,0.44,0.23,0.6 c0.15,0.15,0.35,0.23,0.6,0.23c0.24,0,0.44-0.08,0.62-0.23c0.19-0.19,0.41-0.28,0.67-0.28c0.26,0,0.49,0.09,0.68,0.28 c0.19,0.19,0.29,0.42,0.29,0.68c0,0.27-0.1,0.5-0.29,0.69c-0.19,0.19-0.42,0.29-0.68,0.29H1.23c-0.24,0-0.44,0.09-0.62,0.26 C0.43,17.57,0.35,17.77,0.35,18z M3,15.65c0,0.08,0.06,0.12,0.17,0.12H4.6c0.07,0,0.13-0.05,0.2-0.14c0.22-0.54,0.57-0.99,1.05-1.35 c0.47-0.35,1-0.55,1.6-0.6l0.53-0.07c0.12,0,0.19-0.06,0.19-0.17l0.07-0.52c0.11-1.08,0.56-1.98,1.37-2.71 c0.81-0.73,1.76-1.09,2.85-1.09c1.1,0,2.05,0.36,2.86,1.08s1.27,1.63,1.38,2.71l0.07,0.58c0,0.12,0.06,0.18,0.18,0.18h1.63 c0.9,0,1.67,0.31,2.3,0.94c0.63,0.63,0.95,1.39,0.95,2.27c0,0.89-0.32,1.66-0.95,2.29c-0.63,0.63-1.4,0.95-2.3,0.95h-6.9 c-0.13,0-0.19,0.06-0.19,0.18v1.37c0,0.11,0.06,0.17,0.19,0.17h6.9c0.89,0,1.72-0.22,2.48-0.67c0.76-0.44,1.36-1.05,1.8-1.81 c0.44-0.76,0.66-1.59,0.66-2.48c0-0.74-0.14-1.41-0.42-2.03c0.76-0.99,1.13-2.1,1.13-3.31c0-0.94-0.24-1.81-0.71-2.62 s-1.11-1.45-1.92-1.92c-0.81-0.47-1.68-0.71-2.62-0.71c-1.54,0-2.83,0.58-3.86,1.73c-0.8-0.41-1.69-0.61-2.67-0.61 c-1.41,0-2.65,0.44-3.73,1.31s-1.77,1.99-2.06,3.34c-0.85,0.2-1.6,0.61-2.25,1.23c-0.65,0.62-1.11,1.35-1.36,2.19v0.04 C3.01,15.59,3,15.62,3,15.65z M11.18,4.62c0,0.23,0.09,0.43,0.27,0.6l0.61,0.65c0.16,0.16,0.37,0.24,0.61,0.24 c0.25,0,0.45-0.08,0.61-0.23c0.16-0.15,0.24-0.35,0.24-0.6c0-0.24-0.07-0.44-0.23-0.6l-0.66-0.65c-0.16-0.17-0.35-0.26-0.59-0.26 s-0.44,0.08-0.61,0.25C11.26,4.19,11.18,4.39,11.18,4.62z M16.5,9.03c0.72-0.68,1.54-1.02,2.48-1.02c0.97,0,1.8,0.35,2.51,1.05 c0.7,0.7,1.05,1.54,1.05,2.51c0,0.65-0.17,1.26-0.52,1.83c-0.96-0.96-2.11-1.43-3.46-1.43h-0.34C17.99,10.88,17.41,9.9,16.5,9.03z M18.12,3.79c0,0.23,0.08,0.43,0.25,0.59c0.17,0.16,0.37,0.24,0.6,0.24c0.24,0,0.44-0.08,0.61-0.24c0.17-0.16,0.25-0.35,0.25-0.59 V1.74c0-0.24-0.08-0.44-0.25-0.61c-0.17-0.17-0.37-0.25-0.61-0.25c-0.24,0-0.44,0.08-0.6,0.25s-0.25,0.37-0.25,0.61V3.79z M23.67,6.06c0,0.24,0.08,0.44,0.23,0.6c0.17,0.17,0.37,0.25,0.61,0.26s0.43-0.08,0.57-0.26l1.46-1.43 c0.17-0.17,0.25-0.37,0.25-0.61c0-0.23-0.08-0.43-0.25-0.6c-0.17-0.17-0.37-0.25-0.61-0.25S25.49,3.84,25.33,4L23.9,5.47 C23.75,5.63,23.67,5.82,23.67,6.06z M24.44,17.89c0,0.23,0.08,0.43,0.25,0.6l0.64,0.65c0.2,0.16,0.41,0.24,0.62,0.24 c0.19,0,0.39-0.08,0.59-0.24c0.17-0.17,0.25-0.37,0.25-0.6c0-0.22-0.08-0.42-0.25-0.61l-0.64-0.65c-0.16-0.16-0.36-0.24-0.58-0.24 c-0.25,0-0.46,0.08-0.63,0.25C24.52,17.45,24.44,17.65,24.44,17.89z M25.95,11.57c0,0.24,0.08,0.43,0.25,0.59 c0.15,0.18,0.34,0.26,0.57,0.26h2.02c0.24,0,0.44-0.08,0.61-0.25c0.17-0.17,0.25-0.37,0.25-0.6c0-0.23-0.09-0.43-0.26-0.6 c-0.17-0.17-0.37-0.26-0.6-0.26h-2.02c-0.24,0-0.43,0.08-0.59,0.25C26.03,11.13,25.95,11.33,25.95,11.57z",
    "wi-day-fog": "M0.32,21.06c0,0.23,0.08,0.43,0.25,0.59c0.17,0.16,0.38,0.24,0.63,0.24h18.71c0.24,0,0.44-0.08,0.61-0.24 c0.17-0.16,0.25-0.35,0.25-0.59c0-0.24-0.08-0.44-0.25-0.6c-0.17-0.17-0.37-0.25-0.61-0.25H1.2c-0.25,0-0.46,0.08-0.63,0.25 S0.32,20.82,0.32,21.06z M2.94,17.92c0,0.23,0.08,0.43,0.25,0.58c0.17,0.18,0.37,0.27,0.6,0.27h18.72c0.23,0,0.43-0.08,0.59-0.25 c0.16-0.17,0.24-0.37,0.24-0.6c0-0.23-0.08-0.43-0.23-0.59c-0.16-0.16-0.35-0.24-0.59-0.24H3.8c-0.24,0-0.44,0.08-0.6,0.24 C3.03,17.5,2.94,17.69,2.94,17.92z M3.07,15.52c0,0.09,0.05,0.13,0.16,0.13h1.43c0.07,0,0.14-0.05,0.21-0.16 c0.24-0.52,0.59-0.94,1.06-1.27c0.47-0.33,0.99-0.52,1.56-0.56l0.54-0.07c0.11,0,0.17-0.06,0.17-0.18l0.07-0.51 c0.11-1.08,0.56-1.98,1.37-2.71C10.45,9.47,11.4,9.1,12.5,9.1c1.08,0,2.03,0.36,2.84,1.08c0.81,0.72,1.27,1.61,1.38,2.68l0.07,0.58 c0,0.11,0.06,0.17,0.19,0.17h1.61c0.64,0,1.23,0.17,1.76,0.52c0.53,0.34,0.92,0.8,1.18,1.37c0.07,0.11,0.13,0.16,0.2,0.16h1.44 c0.13,0,0.18-0.07,0.13-0.23l-0.2-0.55c0.76-0.94,1.13-2.04,1.13-3.31c0-0.71-0.14-1.38-0.41-2.03s-0.64-1.2-1.11-1.67 c-0.46-0.47-1.02-0.84-1.67-1.12S19.72,6.35,19,6.35c-1.54,0-2.82,0.56-3.82,1.68C14.33,7.61,13.44,7.4,12.5,7.4 c-1.4,0-2.65,0.44-3.74,1.32s-1.79,2-2.1,3.37c-1.78,0.47-2.98,1.58-3.58,3.35C3.07,15.45,3.07,15.48,3.07,15.52z M4.69,24.13 c0,0.24,0.09,0.44,0.27,0.6c0.16,0.17,0.35,0.26,0.59,0.26h18.74c0.23,0,0.43-0.08,0.6-0.25c0.17-0.17,0.25-0.37,0.25-0.61 c0-0.23-0.08-0.42-0.25-0.58c-0.17-0.15-0.37-0.23-0.6-0.23H5.55c-0.24,0-0.44,0.08-0.61,0.23C4.77,23.71,4.69,23.9,4.69,24.13z M11.26,4.66c0,0.24,0.08,0.43,0.23,0.59l0.65,0.64c0.17,0.18,0.36,0.27,0.58,0.27c0.22,0,0.42-0.08,0.6-0.25 c0.17-0.17,0.26-0.37,0.26-0.61c0-0.24-0.08-0.45-0.25-0.63l-0.64-0.61c-0.16-0.17-0.36-0.26-0.6-0.26c-0.24,0-0.44,0.08-0.6,0.25 C11.34,4.21,11.26,4.42,11.26,4.66z M16.58,9.04c0.67-0.68,1.48-1.01,2.43-1.01c0.98,0,1.82,0.35,2.51,1.04 c0.69,0.69,1.04,1.53,1.04,2.5c0,0.65-0.16,1.25-0.49,1.8c-0.95-0.95-2.11-1.42-3.47-1.42h-0.34C17.97,10.77,17.41,9.8,16.58,9.04z M18.18,3.81c0,0.23,0.08,0.43,0.24,0.59c0.16,0.16,0.35,0.24,0.59,0.24c0.25,0,0.46-0.08,0.63-0.24c0.17-0.16,0.25-0.35,0.25-0.59 V1.76c0-0.23-0.09-0.43-0.26-0.6C19.45,0.99,19.24,0.91,19,0.91c-0.23,0-0.43,0.08-0.59,0.25c-0.16,0.17-0.24,0.37-0.24,0.6V3.81z M23.67,6.08c0,0.22,0.08,0.43,0.24,0.6c0.37,0.36,0.78,0.36,1.23,0l1.43-1.43c0.16-0.18,0.24-0.39,0.24-0.64 c0-0.23-0.08-0.43-0.24-0.59c-0.16-0.16-0.36-0.24-0.59-0.24c-0.24,0-0.44,0.08-0.6,0.24l-1.46,1.47 C23.75,5.67,23.67,5.87,23.67,6.08z M24.48,17.88c0,0.24,0.09,0.44,0.26,0.6l0.64,0.65c0.16,0.16,0.36,0.24,0.58,0.24 c0.21,0,0.41-0.08,0.61-0.24c0.16-0.17,0.24-0.39,0.24-0.64c0-0.22-0.08-0.41-0.24-0.56l-0.65-0.66c-0.18-0.16-0.38-0.24-0.6-0.24 c-0.24,0-0.44,0.08-0.6,0.25C24.56,17.44,24.48,17.64,24.48,17.88z M25.96,11.57c0,0.24,0.09,0.44,0.26,0.6 c0.15,0.17,0.35,0.25,0.59,0.25h2.05c0.23,0,0.43-0.08,0.59-0.25c0.16-0.17,0.24-0.37,0.24-0.6c0-0.24-0.08-0.44-0.24-0.6 c-0.16-0.16-0.35-0.24-0.59-0.24h-2.05c-0.24,0-0.44,0.08-0.6,0.25C26.04,11.14,25.96,11.34,25.96,11.57z",
    "wi-day-rain": "M1.51,16.89c0,1.33,0.46,2.48,1.39,3.44s2.06,1.47,3.41,1.53c0.11,0,0.17-0.06,0.17-0.17v-1.34c0-0.11-0.06-0.17-0.17-0.17 c-0.86-0.04-1.59-0.39-2.19-1.03s-0.9-1.4-0.9-2.26c0-0.82,0.28-1.54,0.85-2.16s1.27-0.97,2.1-1.07l0.53-0.05 c0.13,0,0.2-0.06,0.2-0.17l0.07-0.54c0.11-1.08,0.56-1.99,1.37-2.72s1.76-1.1,2.85-1.1c1.09,0,2.04,0.37,2.86,1.1 s1.28,1.64,1.4,2.72l0.07,0.57c0,0.12,0.06,0.19,0.17,0.19h1.62c0.89,0,1.65,0.32,2.3,0.96c0.65,0.64,0.97,1.39,0.97,2.27 c0,0.87-0.3,1.62-0.9,2.26c-0.6,0.64-1.33,0.98-2.18,1.03c-0.12,0-0.19,0.06-0.19,0.17v1.34c0,0.11,0.06,0.17,0.19,0.17 c1.33-0.04,2.46-0.55,3.39-1.51c0.93-0.97,1.39-2.12,1.39-3.45c0-0.72-0.14-1.39-0.42-2.01c0.78-0.97,1.17-2.07,1.17-3.31 c0-0.95-0.24-1.83-0.71-2.64c-0.47-0.81-1.11-1.45-1.92-1.92s-1.68-0.7-2.62-0.7c-1.56,0-2.85,0.58-3.88,1.74 c-0.82-0.44-1.72-0.66-2.71-0.66c-1.41,0-2.67,0.44-3.76,1.32s-1.79,2-2.1,3.36c-1.11,0.26-2.02,0.84-2.74,1.74 S1.51,15.74,1.51,16.89z M6.91,23.75c0,0.17,0.05,0.33,0.16,0.49c0.11,0.16,0.27,0.27,0.49,0.33c0.11,0.02,0.2,0.04,0.27,0.04 c0.39,0,0.65-0.21,0.77-0.64l1.58-5.88c0.07-0.24,0.04-0.46-0.08-0.67c-0.12-0.21-0.3-0.33-0.53-0.38 c-0.22-0.07-0.43-0.05-0.63,0.07c-0.2,0.11-0.34,0.28-0.41,0.51l-1.58,5.91C6.93,23.66,6.91,23.73,6.91,23.75z M9.52,26.83 c0,0.19,0.05,0.36,0.15,0.52c0.1,0.16,0.27,0.26,0.52,0.3c0.11,0.02,0.2,0.04,0.26,0.04c0.16,0,0.31-0.06,0.45-0.17 c0.14-0.12,0.23-0.28,0.27-0.48l2.4-8.93c0.06-0.23,0.04-0.45-0.07-0.64s-0.28-0.33-0.5-0.4c-0.23-0.07-0.45-0.05-0.65,0.07 c-0.2,0.11-0.34,0.28-0.4,0.51l-2.4,8.93C9.53,26.73,9.52,26.82,9.52,26.83z M9.94,4.6c0,0.24,0.08,0.44,0.25,0.61l0.65,0.66 c0.19,0.15,0.4,0.22,0.62,0.22c0.21,0,0.41-0.08,0.58-0.23c0.17-0.16,0.26-0.35,0.26-0.59c0-0.24-0.08-0.46-0.24-0.64l-0.64-0.65 c-0.18-0.17-0.38-0.25-0.6-0.25c-0.24,0-0.45,0.09-0.62,0.26C10.03,4.16,9.94,4.37,9.94,4.6z M13.67,23.77 c0,0.16,0.05,0.32,0.15,0.47s0.26,0.26,0.46,0.32c0.11,0.02,0.2,0.04,0.25,0.04c0.17,0,0.34-0.05,0.49-0.15 c0.15-0.1,0.25-0.26,0.3-0.49l1.58-5.88c0.06-0.23,0.04-0.45-0.07-0.64c-0.11-0.2-0.28-0.33-0.5-0.4c-0.24-0.07-0.45-0.05-0.65,0.07 c-0.2,0.11-0.33,0.28-0.39,0.51l-1.58,5.91C13.69,23.68,13.67,23.76,13.67,23.77z M15.3,9.03c0.71-0.67,1.53-1,2.48-1 c0.98,0,1.82,0.35,2.5,1.04c0.69,0.69,1.03,1.53,1.03,2.52c0,0.62-0.17,1.24-0.52,1.85c-0.97-0.97-2.13-1.45-3.49-1.45h-0.33 C16.7,10.81,16.14,9.83,15.3,9.03z M16.92,3.78c0,0.23,0.08,0.43,0.25,0.59c0.17,0.16,0.37,0.24,0.61,0.24 c0.23,0,0.43-0.08,0.59-0.23c0.16-0.16,0.24-0.35,0.24-0.59V1.73c0-0.26-0.08-0.47-0.23-0.63c-0.16-0.16-0.35-0.24-0.59-0.24 c-0.25,0-0.46,0.08-0.62,0.25s-0.24,0.37-0.24,0.62V3.78z M22.45,6.06c0,0.24,0.09,0.44,0.27,0.59c0.14,0.16,0.32,0.24,0.55,0.26 c0.23,0.02,0.44-0.07,0.62-0.26l1.44-1.43c0.18-0.17,0.26-0.38,0.26-0.63c0-0.24-0.08-0.45-0.25-0.61 c-0.17-0.16-0.37-0.24-0.61-0.24c-0.21,0-0.4,0.08-0.58,0.25l-1.43,1.44C22.54,5.6,22.45,5.81,22.45,6.06z M23.26,17.91 c0,0.24,0.08,0.45,0.24,0.63l0.65,0.63c0.18,0.14,0.38,0.21,0.6,0.21l0.02,0.02c0.23,0,0.42-0.08,0.58-0.24 c0.16-0.16,0.24-0.37,0.24-0.61c0-0.24-0.09-0.43-0.26-0.58l-0.62-0.66c-0.18-0.16-0.39-0.24-0.62-0.24s-0.43,0.08-0.59,0.25 S23.26,17.67,23.26,17.91z M24.72,11.58c0,0.24,0.09,0.43,0.26,0.59c0.18,0.18,0.38,0.26,0.62,0.26h2.03c0.24,0,0.44-0.08,0.61-0.25 c0.17-0.17,0.25-0.37,0.25-0.6c0-0.24-0.08-0.44-0.25-0.61s-0.37-0.26-0.61-0.26H25.6c-0.24,0-0.44,0.09-0.62,0.26 C24.8,11.14,24.72,11.34,24.72,11.58z",
    "wi-day-showers": "M1.56,16.88c0,1.33,0.46,2.47,1.39,3.43s2.06,1.46,3.4,1.53c0.11,0,0.17-0.06,0.17-0.17v-1.34c0-0.12-0.06-0.18-0.17-0.18 c-0.85-0.04-1.57-0.38-2.17-1.02s-0.89-1.39-0.89-2.25c0-0.84,0.28-1.56,0.84-2.17s1.26-0.96,2.1-1.06l0.5-0.04 c0.13,0,0.2-0.06,0.2-0.18l0.06-0.53c0.11-1.08,0.56-1.99,1.37-2.71c0.81-0.73,1.76-1.09,2.86-1.09c1.09,0,2.05,0.36,2.86,1.09 c0.81,0.73,1.27,1.63,1.37,2.71l0.07,0.57c0,0.12,0.06,0.18,0.18,0.18h1.67c0.88,0,1.63,0.32,2.27,0.96 c0.64,0.64,0.96,1.39,0.96,2.27c0,0.85-0.3,1.59-0.9,2.22s-1.32,0.98-2.16,1.05c-0.11,0-0.17,0.06-0.17,0.18v1.34 c0,0.11,0.06,0.17,0.17,0.17c0.88-0.02,1.67-0.26,2.4-0.72s1.3-1.05,1.71-1.8c0.42-0.75,0.62-1.56,0.62-2.44 c0-0.71-0.14-1.37-0.41-1.96c0.76-0.94,1.13-2.03,1.13-3.28c0-0.71-0.14-1.39-0.41-2.04c-0.27-0.65-0.65-1.2-1.12-1.67 C21,7.46,20.44,7.09,19.8,6.82c-0.65-0.28-1.33-0.41-2.04-0.41c-1.51,0-2.78,0.55-3.81,1.66c-0.79-0.43-1.7-0.64-2.73-0.64 c-1.41,0-2.66,0.44-3.75,1.31s-1.77,1.99-2.07,3.35c-1.12,0.26-2.05,0.83-2.77,1.72C1.92,14.7,1.56,15.73,1.56,16.88z M6.97,23.58 c0,0.18,0.05,0.36,0.16,0.53c0.11,0.18,0.26,0.29,0.45,0.36c0.19,0.07,0.4,0.05,0.61-0.06c0.22-0.11,0.36-0.29,0.44-0.55l0.25-1.05 c0.07-0.21,0.05-0.41-0.07-0.62c-0.12-0.21-0.29-0.35-0.51-0.42c-0.25-0.06-0.47-0.03-0.67,0.08s-0.32,0.3-0.37,0.53l-0.28,0.99 C6.98,23.42,6.97,23.49,6.97,23.58z M8.28,18.86c0,0.38,0.21,0.64,0.64,0.79c0.22,0.08,0.43,0.06,0.64-0.05 c0.21-0.11,0.34-0.29,0.41-0.53l0.24-1.03c0.07-0.21,0.05-0.41-0.07-0.62c-0.11-0.21-0.28-0.35-0.51-0.42 c-0.24-0.06-0.47-0.04-0.67,0.08s-0.32,0.29-0.37,0.52l-0.3,1.02C8.29,18.7,8.28,18.78,8.28,18.86z M9.5,26.75 c0,0.16,0.06,0.33,0.17,0.5c0.11,0.17,0.28,0.29,0.49,0.36c0.01,0,0.04,0,0.1,0.01c0.06,0.01,0.11,0.01,0.15,0.01 c0.14,0,0.26-0.02,0.37-0.07c0.19-0.08,0.33-0.27,0.41-0.58l0.27-0.99c0.07-0.23,0.05-0.45-0.07-0.65c-0.12-0.2-0.29-0.34-0.51-0.4 c-0.23-0.07-0.45-0.05-0.65,0.07c-0.2,0.12-0.34,0.29-0.4,0.51l-0.28,1.02C9.51,26.63,9.5,26.7,9.5,26.75z M9.96,4.68 c0,0.25,0.08,0.46,0.25,0.62l0.66,0.65c0.34,0.34,0.73,0.34,1.17,0c0.16-0.17,0.24-0.38,0.24-0.61c0-0.23-0.08-0.43-0.24-0.61 l-0.63-0.66c-0.16-0.16-0.36-0.24-0.6-0.24c-0.23,0-0.43,0.08-0.6,0.25C10.04,4.24,9.96,4.44,9.96,4.68z M10.85,21.96 c0,0.17,0.05,0.34,0.16,0.51c0.11,0.17,0.26,0.28,0.47,0.35c0.23,0.07,0.44,0.05,0.64-0.05c0.19-0.1,0.33-0.29,0.4-0.56l0.24-1.01 c0.07-0.23,0.05-0.45-0.06-0.65c-0.11-0.2-0.28-0.34-0.5-0.41c-0.25-0.07-0.48-0.04-0.68,0.08c-0.2,0.12-0.33,0.3-0.37,0.53 l-0.28,1.03C10.85,21.81,10.85,21.87,10.85,21.96z M13.63,23.68c0.02,0.38,0.23,0.65,0.63,0.83l0.25,0.04 c0.16,0,0.32-0.05,0.47-0.16c0.15-0.11,0.26-0.27,0.32-0.5l0.29-1.01c0.06-0.24,0.03-0.46-0.09-0.66c-0.12-0.2-0.3-0.33-0.53-0.37 c-0.21-0.07-0.41-0.05-0.62,0.07s-0.34,0.29-0.41,0.51l-0.27,1.02c-0.01,0.02-0.01,0.05-0.02,0.08s-0.01,0.06-0.02,0.08 S13.63,23.66,13.63,23.68z M15.03,18.92c0,0.16,0.05,0.32,0.15,0.48c0.1,0.16,0.25,0.27,0.45,0.32l0.25,0.03 c0.19,0,0.37-0.06,0.52-0.18s0.24-0.28,0.28-0.47l0.27-0.99c0.07-0.24,0.05-0.45-0.07-0.65c-0.11-0.2-0.28-0.33-0.51-0.39 c-0.23-0.07-0.45-0.05-0.64,0.06c-0.2,0.11-0.33,0.28-0.39,0.5l-0.3,1.06C15.04,18.77,15.03,18.85,15.03,18.92z M15.36,9.06 c0.66-0.64,1.46-0.96,2.4-0.96c0.98,0,1.82,0.35,2.51,1.04c0.69,0.69,1.04,1.53,1.04,2.51c0,0.56-0.16,1.15-0.47,1.76 c-0.96-0.96-2.11-1.43-3.47-1.43h-0.34C16.77,10.84,16.21,9.87,15.36,9.06z M16.9,3.83c0,0.25,0.08,0.45,0.24,0.61 c0.16,0.16,0.36,0.24,0.61,0.24s0.45-0.08,0.61-0.24c0.16-0.16,0.24-0.36,0.24-0.61V1.81c0-0.25-0.08-0.46-0.24-0.62 c-0.16-0.16-0.36-0.24-0.61-0.24s-0.45,0.08-0.61,0.24c-0.16,0.16-0.24,0.37-0.24,0.62V3.83z M22.45,6.12 c0,0.25,0.08,0.45,0.23,0.61c0.21,0.17,0.41,0.25,0.62,0.25c0.19,0,0.38-0.08,0.59-0.25l1.43-1.43c0.16-0.18,0.24-0.39,0.24-0.63 c0-0.24-0.08-0.44-0.24-0.6c-0.16-0.16-0.36-0.24-0.59-0.24s-0.43,0.08-0.61,0.24L22.68,5.5C22.53,5.68,22.45,5.88,22.45,6.12z M23.24,17.95c0,0.23,0.09,0.44,0.26,0.63l0.62,0.64c0.21,0.21,0.41,0.31,0.62,0.31c0.19,0,0.39-0.1,0.58-0.31 c0.18-0.18,0.27-0.39,0.26-0.61c-0.01-0.23-0.09-0.43-0.26-0.6l-0.65-0.66c-0.16-0.16-0.35-0.24-0.57-0.24 c-0.24,0-0.44,0.08-0.61,0.25C23.33,17.52,23.24,17.72,23.24,17.95z M24.71,11.64c0,0.22,0.08,0.42,0.24,0.58 c0.16,0.16,0.36,0.24,0.58,0.24h2.04c0.26,0,0.47-0.08,0.63-0.23c0.16-0.16,0.24-0.35,0.24-0.59c0-0.25-0.08-0.46-0.25-0.62 c-0.17-0.16-0.37-0.24-0.62-0.24h-2.04c-0.23,0-0.43,0.08-0.59,0.25C24.79,11.2,24.71,11.41,24.71,11.64z",
    "wi-day-snow": "M1.58,16.93c0,0.86,0.21,1.67,0.64,2.41c0.42,0.74,1,1.34,1.74,1.79c0.73,0.45,1.54,0.69,2.4,0.71 c0.11,0,0.17-0.06,0.17-0.17v-1.33c0-0.12-0.06-0.19-0.17-0.19c-0.85-0.04-1.58-0.38-2.18-1.02s-0.9-1.37-0.9-2.21 c0-0.82,0.28-1.54,0.85-2.16c0.57-0.61,1.26-0.97,2.1-1.07l0.53-0.06c0.12,0,0.18-0.06,0.18-0.19l0.08-0.51 c0.11-1.09,0.56-2,1.36-2.73c0.8-0.73,1.75-1.09,2.85-1.09c1.09,0,2.04,0.36,2.85,1.09c0.82,0.73,1.28,1.63,1.38,2.7l0.07,0.58 c0,0.11,0.06,0.17,0.17,0.17h1.61c0.9,0,1.67,0.32,2.31,0.96c0.64,0.64,0.96,1.4,0.96,2.29c0,0.84-0.3,1.57-0.9,2.21 c-0.6,0.63-1.33,0.97-2.17,1.02c-0.12,0-0.19,0.06-0.19,0.19v1.33c0,0.11,0.06,0.17,0.19,0.17c1.33-0.04,2.45-0.54,3.38-1.5 c0.93-0.96,1.39-2.09,1.39-3.41c0-0.76-0.14-1.43-0.43-2.03C22.6,13.95,23,12.85,23,11.6c0-0.94-0.23-1.81-0.7-2.61 c-0.47-0.8-1.11-1.44-1.91-1.91s-1.68-0.7-2.62-0.7c-1.54,0-2.83,0.58-3.87,1.73c-0.81-0.44-1.71-0.66-2.69-0.66 c-1.41,0-2.65,0.44-3.74,1.31s-1.78,1.99-2.09,3.34c-1.12,0.28-2.03,0.86-2.74,1.75C1.93,14.75,1.58,15.77,1.58,16.93z M7.92,20.98 c0,0.24,0.08,0.44,0.24,0.61c0.16,0.17,0.35,0.25,0.59,0.25c0.23,0,0.43-0.08,0.59-0.25c0.16-0.17,0.24-0.37,0.24-0.61 c0-0.23-0.08-0.42-0.24-0.58s-0.35-0.24-0.59-0.24c-0.23,0-0.43,0.08-0.59,0.24S7.92,20.76,7.92,20.98z M7.92,24.61 c0,0.21,0.08,0.4,0.24,0.57c0.18,0.16,0.37,0.24,0.58,0.24c0.24,0,0.43-0.08,0.59-0.23c0.16-0.16,0.23-0.35,0.23-0.58 c0-0.24-0.08-0.43-0.24-0.59c-0.16-0.16-0.35-0.23-0.59-0.23c-0.23,0-0.43,0.08-0.59,0.23C8,24.17,7.92,24.37,7.92,24.61z M9.97,4.68c0,0.24,0.08,0.44,0.24,0.59l0.66,0.66c0.16,0.16,0.34,0.25,0.53,0.25c0.21,0.03,0.41-0.04,0.61-0.22 c0.2-0.18,0.3-0.39,0.3-0.63c0-0.24-0.08-0.46-0.24-0.64l-0.64-0.61c-0.15-0.17-0.34-0.25-0.58-0.25c-0.25,0-0.46,0.08-0.63,0.25 C10.05,4.24,9.97,4.44,9.97,4.68z M11.1,22.9c0,0.22,0.08,0.42,0.24,0.6c0.16,0.16,0.36,0.24,0.58,0.24c0.24,0,0.44-0.08,0.6-0.24 s0.25-0.36,0.25-0.6c0-0.23-0.08-0.43-0.25-0.6s-0.37-0.25-0.6-0.25c-0.23,0-0.42,0.08-0.58,0.25S11.1,22.67,11.1,22.9z M11.1,19.3 c0,0.23,0.08,0.42,0.24,0.58s0.36,0.24,0.58,0.24c0.24,0,0.44-0.08,0.6-0.24c0.17-0.16,0.25-0.35,0.25-0.59 c0-0.23-0.08-0.43-0.25-0.59s-0.37-0.24-0.6-0.24c-0.23,0-0.42,0.08-0.58,0.24S11.1,19.07,11.1,19.3z M11.1,26.56 c0,0.22,0.08,0.41,0.24,0.57c0.17,0.17,0.36,0.25,0.58,0.25c0.24,0,0.44-0.08,0.6-0.23c0.17-0.16,0.25-0.35,0.25-0.59 s-0.08-0.44-0.25-0.6c-0.17-0.17-0.37-0.25-0.6-0.25c-0.22,0-0.41,0.08-0.58,0.25C11.18,26.13,11.1,26.33,11.1,26.56z M14.32,20.98 c0,0.24,0.08,0.44,0.24,0.61c0.16,0.17,0.36,0.25,0.59,0.25s0.43-0.08,0.59-0.25c0.16-0.17,0.24-0.37,0.24-0.61 c0-0.23-0.08-0.42-0.24-0.58s-0.35-0.24-0.59-0.24s-0.43,0.08-0.59,0.24S14.32,20.76,14.32,20.98z M14.32,24.61 c0,0.21,0.08,0.4,0.23,0.57c0.18,0.16,0.38,0.24,0.6,0.24c0.24,0,0.43-0.08,0.59-0.23c0.16-0.16,0.23-0.35,0.23-0.58 c0-0.24-0.08-0.43-0.24-0.59c-0.16-0.16-0.35-0.23-0.59-0.23c-0.24,0-0.44,0.08-0.6,0.24C14.4,24.18,14.32,24.38,14.32,24.61z M15.3,9.06c0.69-0.66,1.51-0.99,2.47-0.99c0.97,0,1.8,0.35,2.48,1.04c0.69,0.69,1.03,1.53,1.03,2.49c0,0.62-0.17,1.24-0.51,1.84 C19.82,12.48,18.66,12,17.3,12h-0.32C16.68,10.83,16.12,9.85,15.3,9.06z M16.9,3.84c0,0.23,0.08,0.43,0.25,0.58s0.37,0.23,0.61,0.23 s0.43-0.08,0.59-0.23c0.16-0.16,0.23-0.35,0.23-0.58V1.8c0-0.24-0.08-0.44-0.24-0.61S18,0.94,17.77,0.94s-0.43,0.09-0.6,0.26 c-0.17,0.17-0.26,0.37-0.26,0.6V3.84z M22.42,6.11c0,0.23,0.08,0.43,0.25,0.59c0.15,0.16,0.34,0.24,0.56,0.26s0.43-0.07,0.62-0.26 l1.43-1.43c0.18-0.18,0.26-0.38,0.26-0.61c0-0.24-0.09-0.44-0.26-0.61c-0.17-0.17-0.37-0.25-0.6-0.25c-0.22,0-0.41,0.08-0.58,0.25 l-1.43,1.46C22.5,5.67,22.42,5.87,22.42,6.11z M23.22,17.91c0,0.25,0.08,0.46,0.24,0.62l0.64,0.63c0.24,0.16,0.46,0.24,0.64,0.24 c0.21,0,0.39-0.09,0.56-0.26c0.17-0.17,0.25-0.38,0.25-0.61c0-0.23-0.09-0.42-0.26-0.58l-0.62-0.65c-0.18-0.16-0.38-0.24-0.61-0.24 s-0.43,0.08-0.59,0.25C23.3,17.47,23.22,17.67,23.22,17.91z M24.67,11.6c0,0.24,0.09,0.43,0.26,0.59c0.17,0.18,0.38,0.27,0.62,0.27 h2.02c0.23,0,0.43-0.08,0.6-0.25s0.25-0.37,0.25-0.61c0-0.24-0.08-0.44-0.25-0.6s-0.37-0.25-0.6-0.25h-2.02 c-0.24,0-0.44,0.08-0.62,0.25S24.67,11.37,24.67,11.6z",
    "wi-day-sprinkle": "M1.58,16.89c0,0.87,0.21,1.68,0.64,2.42c0.42,0.75,1,1.35,1.73,1.8c0.73,0.45,1.53,0.69,2.4,0.73 c0.12,0,0.18-0.06,0.18-0.17v-1.33c0-0.12-0.06-0.19-0.18-0.19c-0.85-0.04-1.58-0.38-2.18-1.02s-0.9-1.38-0.9-2.25 c0-0.82,0.28-1.54,0.84-2.15s1.26-0.96,2.09-1.06l0.52-0.03c0.12,0,0.19-0.06,0.19-0.18l0.08-0.54c0.11-1.08,0.56-1.98,1.36-2.71 c0.8-0.73,1.75-1.09,2.85-1.09c1.07,0,2.02,0.36,2.84,1.09c0.82,0.73,1.28,1.63,1.4,2.7l0.07,0.58c0,0.11,0.06,0.17,0.17,0.17h1.62 c0.88,0,1.65,0.32,2.29,0.96c0.65,0.64,0.97,1.39,0.97,2.26c0,0.86-0.3,1.61-0.9,2.25c-0.6,0.63-1.33,0.97-2.18,1.02 c-0.12,0-0.18,0.06-0.18,0.19v1.33c0,0.11,0.06,0.17,0.18,0.17c0.87-0.02,1.67-0.26,2.4-0.72s1.31-1.05,1.73-1.8 s0.63-1.56,0.63-2.43c0-0.74-0.14-1.4-0.41-2c0.78-0.95,1.16-2.05,1.16-3.3c0-0.94-0.23-1.82-0.7-2.62s-1.1-1.44-1.9-1.9 c-0.8-0.47-1.67-0.7-2.61-0.7c-1.55,0-2.84,0.58-3.87,1.73c-0.82-0.44-1.72-0.66-2.69-0.66c-1.41,0-2.65,0.44-3.74,1.31 s-1.78,1.99-2.09,3.34c-1.1,0.26-2.01,0.84-2.72,1.73C1.93,14.73,1.58,15.75,1.58,16.89z M7.48,17.77c0,0.38,0.14,0.71,0.42,0.98 s0.62,0.4,1.01,0.4c0.4,0,0.73-0.13,1-0.4c0.27-0.27,0.4-0.59,0.4-0.98c0-0.24-0.12-0.58-0.35-1c-0.23-0.43-0.45-0.76-0.65-0.99 c-0.21-0.22-0.35-0.37-0.4-0.42l-0.36,0.4c-0.27,0.29-0.52,0.63-0.74,1.03S7.48,17.52,7.48,17.77z M9.97,4.66 c0,0.26,0.08,0.46,0.24,0.61l0.65,0.66c0.42,0.31,0.82,0.31,1.21,0c0.16-0.19,0.24-0.41,0.24-0.64c0-0.23-0.08-0.43-0.24-0.59 l-0.64-0.65c-0.19-0.17-0.39-0.25-0.61-0.25c-0.24,0-0.45,0.08-0.61,0.25C10.05,4.22,9.97,4.42,9.97,4.66z M10.45,21.73 c0,0.66,0.23,1.21,0.68,1.65s1,0.67,1.65,0.67c0.65,0,1.2-0.23,1.66-0.68c0.46-0.46,0.68-1,0.68-1.64c0-0.54-0.27-1.19-0.81-1.97 c-0.46-0.61-0.89-1.1-1.28-1.49c-0.08-0.06-0.17-0.13-0.26-0.23l-0.23,0.23c-0.36,0.32-0.78,0.82-1.27,1.47 C10.73,20.5,10.45,21.16,10.45,21.73z M11.93,15.11c0,0.25,0.1,0.47,0.29,0.65c0.19,0.18,0.42,0.27,0.69,0.27 c0.26,0,0.48-0.09,0.66-0.27c0.18-0.18,0.27-0.4,0.27-0.65c0-0.41-0.31-0.95-0.93-1.6l-0.24,0.25c-0.18,0.2-0.35,0.43-0.5,0.7 C12,14.73,11.93,14.94,11.93,15.11z M15.3,9.06c0.66-0.66,1.48-0.99,2.47-0.99c0.98,0,1.8,0.34,2.49,1.03 c0.68,0.69,1.03,1.52,1.03,2.5c0,0.59-0.17,1.2-0.52,1.84C19.8,12.48,18.64,12,17.3,12h-0.33C16.68,10.84,16.12,9.86,15.3,9.06z M16.9,3.8c0,0.24,0.09,0.44,0.26,0.61c0.17,0.17,0.37,0.25,0.6,0.25s0.43-0.08,0.58-0.25c0.16-0.17,0.23-0.37,0.23-0.61V1.76 c0-0.24-0.08-0.43-0.23-0.59S18,0.94,17.77,0.94s-0.44,0.08-0.61,0.23S16.9,1.52,16.9,1.76V3.8z M22.42,6.11 c0,0.24,0.08,0.44,0.25,0.59c0.17,0.16,0.36,0.24,0.56,0.24c0.17,0,0.38-0.08,0.61-0.24l1.43-1.43c0.18-0.18,0.27-0.39,0.27-0.62 c0-0.24-0.08-0.45-0.25-0.61c-0.17-0.16-0.37-0.24-0.61-0.24c-0.22,0-0.41,0.08-0.58,0.25l-1.43,1.43 C22.5,5.65,22.42,5.86,22.42,6.11z M23.22,17.89c0,0.25,0.08,0.46,0.23,0.64l0.65,0.58c0.14,0.18,0.34,0.27,0.59,0.27 c0.24,0,0.44-0.09,0.58-0.27c0.18-0.16,0.27-0.36,0.27-0.58c0-0.22-0.09-0.41-0.27-0.58l-0.61-0.65c-0.18-0.16-0.38-0.24-0.61-0.24 c-0.24,0-0.44,0.08-0.6,0.24S23.22,17.66,23.22,17.89z M24.67,11.6c0,0.22,0.08,0.42,0.25,0.58c0.17,0.16,0.38,0.24,0.63,0.24h2.02 c0.24,0,0.44-0.08,0.6-0.24c0.17-0.16,0.25-0.35,0.25-0.59c0-0.24-0.08-0.44-0.25-0.6s-0.37-0.25-0.6-0.25h-2.02 c-0.24,0-0.44,0.08-0.62,0.25S24.67,11.37,24.67,11.6z",
    "wi-day-sunny": "M4.37,14.62c0-0.24,0.08-0.45,0.25-0.62c0.17-0.16,0.38-0.24,0.6-0.24h2.04c0.23,0,0.42,0.08,0.58,0.25 c0.15,0.17,0.23,0.37,0.23,0.61S8,15.06,7.85,15.23c-0.15,0.17-0.35,0.25-0.58,0.25H5.23c-0.23,0-0.43-0.08-0.6-0.25 C4.46,15.06,4.37,14.86,4.37,14.62z M7.23,21.55c0-0.23,0.08-0.43,0.23-0.61l1.47-1.43c0.15-0.16,0.35-0.23,0.59-0.23 c0.24,0,0.44,0.08,0.6,0.23s0.24,0.34,0.24,0.57c0,0.24-0.08,0.46-0.24,0.64L8.7,22.14c-0.41,0.32-0.82,0.32-1.23,0 C7.31,21.98,7.23,21.78,7.23,21.55z M7.23,7.71c0-0.23,0.08-0.43,0.23-0.61C7.66,6.93,7.87,6.85,8.1,6.85 c0.22,0,0.42,0.08,0.59,0.24l1.43,1.47c0.16,0.15,0.24,0.35,0.24,0.59c0,0.24-0.08,0.44-0.24,0.6s-0.36,0.24-0.6,0.24 c-0.24,0-0.44-0.08-0.59-0.24L7.47,8.32C7.31,8.16,7.23,7.95,7.23,7.71z M9.78,14.62c0-0.93,0.23-1.8,0.7-2.6s1.1-1.44,1.91-1.91 s1.67-0.7,2.6-0.7c0.7,0,1.37,0.14,2.02,0.42c0.64,0.28,1.2,0.65,1.66,1.12c0.47,0.47,0.84,1.02,1.11,1.66 c0.27,0.64,0.41,1.32,0.41,2.02c0,0.94-0.23,1.81-0.7,2.61c-0.47,0.8-1.1,1.43-1.9,1.9c-0.8,0.47-1.67,0.7-2.61,0.7 s-1.81-0.23-2.61-0.7c-0.8-0.47-1.43-1.1-1.9-1.9C10.02,16.43,9.78,15.56,9.78,14.62z M11.48,14.62c0,0.98,0.34,1.81,1.03,2.5 c0.68,0.69,1.51,1.04,2.49,1.04s1.81-0.35,2.5-1.04s1.04-1.52,1.04-2.5c0-0.96-0.35-1.78-1.04-2.47c-0.69-0.68-1.52-1.02-2.5-1.02 c-0.97,0-1.8,0.34-2.48,1.02C11.82,12.84,11.48,13.66,11.48,14.62z M14.14,22.4c0-0.24,0.08-0.44,0.25-0.6s0.37-0.24,0.6-0.24 c0.24,0,0.45,0.08,0.61,0.24s0.24,0.36,0.24,0.6v1.99c0,0.24-0.08,0.45-0.25,0.62c-0.17,0.17-0.37,0.25-0.6,0.25 s-0.44-0.08-0.6-0.25c-0.17-0.17-0.25-0.38-0.25-0.62V22.4z M14.14,6.9V4.86c0-0.23,0.08-0.43,0.25-0.6C14.56,4.09,14.76,4,15,4 s0.43,0.08,0.6,0.25c0.17,0.17,0.25,0.37,0.25,0.6V6.9c0,0.23-0.08,0.42-0.25,0.58S15.23,7.71,15,7.71s-0.44-0.08-0.6-0.23 S14.14,7.13,14.14,6.9z M19.66,20.08c0-0.23,0.08-0.42,0.23-0.56c0.15-0.16,0.34-0.23,0.56-0.23c0.24,0,0.44,0.08,0.6,0.23 l1.46,1.43c0.16,0.17,0.24,0.38,0.24,0.61c0,0.23-0.08,0.43-0.24,0.59c-0.4,0.31-0.8,0.31-1.2,0l-1.42-1.42 C19.74,20.55,19.66,20.34,19.66,20.08z M19.66,9.16c0-0.25,0.08-0.45,0.23-0.59l1.42-1.47c0.17-0.16,0.37-0.24,0.59-0.24 c0.24,0,0.44,0.08,0.6,0.25c0.17,0.17,0.25,0.37,0.25,0.6c0,0.25-0.08,0.46-0.24,0.62l-1.46,1.43c-0.18,0.16-0.38,0.24-0.6,0.24 c-0.23,0-0.41-0.08-0.56-0.24S19.66,9.4,19.66,9.16z M21.92,14.62c0-0.24,0.08-0.44,0.24-0.62c0.16-0.16,0.35-0.24,0.57-0.24h2.02 c0.23,0,0.43,0.09,0.6,0.26c0.17,0.17,0.26,0.37,0.26,0.6s-0.09,0.43-0.26,0.6c-0.17,0.17-0.37,0.25-0.6,0.25h-2.02 c-0.23,0-0.43-0.08-0.58-0.25S21.92,14.86,21.92,14.62z",
    "wi-day-sunny-overcast": "M3.09,13.43c0,0.27,0.09,0.49,0.28,0.67s0.43,0.26,0.72,0.26h1.69c0.27,0,0.5-0.09,0.69-0.27s0.29-0.4,0.29-0.66 c0-0.29-0.09-0.53-0.28-0.71s-0.42-0.28-0.69-0.28H4.09c-0.29,0-0.53,0.09-0.72,0.28C3.18,12.89,3.09,13.13,3.09,13.43z M4.86,19.61 c0,0.97,0.35,1.81,1.06,2.52c0.71,0.71,1.54,1.06,2.51,1.06h6.86c0.97,0,1.8-0.35,2.49-1.05c0.69-0.7,1.04-1.54,1.04-2.53 c0-0.48-0.07-0.89-0.21-1.23c0.83-0.53,1.49-1.24,1.97-2.12c0.48-0.88,0.73-1.83,0.73-2.84c0-0.81-0.16-1.59-0.48-2.33 c-0.32-0.74-0.75-1.38-1.28-1.91c-0.53-0.53-1.17-0.96-1.91-1.28c-0.74-0.32-1.51-0.48-2.32-0.48c-1.09,0-2.1,0.27-3.02,0.81 s-1.65,1.27-2.18,2.18c-0.53,0.92-0.79,1.92-0.79,3.01v0.34c-1,0.57-1.66,1.4-1.98,2.49c-0.76,0.24-1.36,0.66-1.81,1.27 C5.09,18.13,4.86,18.83,4.86,19.61z M6.42,5.45c0,0.28,0.09,0.51,0.26,0.67l1.61,1.69c0.47,0.36,0.94,0.36,1.41,0 c0.19-0.19,0.28-0.42,0.28-0.7c0-0.27-0.09-0.5-0.28-0.7L8.05,4.76C7.84,4.57,7.6,4.47,7.35,4.47c-0.28,0-0.51,0.09-0.68,0.28 C6.5,4.94,6.42,5.17,6.42,5.45z M6.84,19.61c0-0.42,0.13-0.78,0.4-1.08c0.27-0.3,0.61-0.47,1.02-0.51l0.62-0.08 c0.13,0,0.2-0.08,0.2-0.23l0.09-0.56c0.07-0.58,0.31-1.06,0.73-1.44c0.42-0.39,0.91-0.58,1.48-0.58c0.58,0,1.09,0.19,1.51,0.58 c0.43,0.39,0.68,0.87,0.75,1.44l0.08,0.65c0.06,0.15,0.14,0.23,0.24,0.23h1.32c0.43,0,0.8,0.16,1.12,0.47 c0.32,0.31,0.47,0.68,0.47,1.12c0,0.45-0.16,0.83-0.47,1.15s-0.69,0.48-1.12,0.48H8.43c-0.45,0-0.83-0.16-1.13-0.48 C6.99,20.45,6.84,20.06,6.84,19.61z M11.26,13.22c0.07-1.09,0.49-2.01,1.27-2.76c0.77-0.74,1.71-1.12,2.79-1.12 c1.11,0,2.06,0.4,2.84,1.19c0.78,0.79,1.17,1.76,1.17,2.89c0,0.7-0.17,1.35-0.51,1.95c-0.34,0.6-0.8,1.08-1.38,1.45 c-0.59-0.49-1.27-0.73-2.03-0.73c-0.29-0.88-0.81-1.57-1.54-2.09c-0.73-0.52-1.56-0.78-2.48-0.78H11.26z M14.35,4.47 c0,0.27,0.1,0.51,0.29,0.7c0.19,0.19,0.42,0.29,0.69,0.29c0.28,0,0.51-0.1,0.7-0.29c0.19-0.19,0.29-0.42,0.29-0.7V2.13 c0-0.26-0.1-0.48-0.29-0.66c-0.19-0.18-0.42-0.27-0.7-0.27c-0.27,0-0.5,0.09-0.69,0.27c-0.19,0.18-0.29,0.4-0.29,0.66V4.47z M20.67,19.7c0,0.27,0.09,0.5,0.27,0.7l1.64,1.62c0.42,0.42,0.89,0.42,1.41,0c0.18-0.17,0.26-0.39,0.26-0.68 c0-0.27-0.09-0.49-0.26-0.67L22.3,19c-0.18-0.17-0.41-0.25-0.68-0.25c-0.28,0-0.5,0.09-0.68,0.27C20.76,19.2,20.67,19.43,20.67,19.7 z M20.67,7.09c0,0.28,0.09,0.52,0.27,0.72c0.18,0.18,0.41,0.27,0.68,0.27c0.27,0,0.5-0.09,0.68-0.27l1.69-1.69 c0.18-0.17,0.26-0.39,0.26-0.67c0-0.28-0.1-0.51-0.29-0.69s-0.42-0.28-0.7-0.28c-0.26,0-0.49,0.1-0.68,0.29L20.94,6.4 C20.76,6.58,20.67,6.81,20.67,7.09z M23.25,13.43c0,0.27,0.09,0.49,0.28,0.67s0.43,0.26,0.72,0.26h1.69c0.27,0,0.5-0.09,0.69-0.27 s0.29-0.4,0.29-0.66c0-0.29-0.09-0.53-0.28-0.71s-0.42-0.28-0.69-0.28h-1.69c-0.29,0-0.53,0.09-0.72,0.28 C23.35,12.89,23.25,13.13,23.25,13.43z",
    "wi-fog": "M2.62,21.05c0-0.24,0.08-0.45,0.25-0.61c0.17-0.16,0.38-0.24,0.63-0.24h18.67c0.25,0,0.45,0.08,0.61,0.24 c0.16,0.16,0.24,0.36,0.24,0.61c0,0.23-0.08,0.43-0.25,0.58c-0.17,0.16-0.37,0.23-0.6,0.23H3.5c-0.25,0-0.46-0.08-0.63-0.23 C2.7,21.47,2.62,21.28,2.62,21.05z M5.24,17.91c0-0.24,0.09-0.44,0.26-0.6c0.15-0.15,0.35-0.23,0.59-0.23h18.67 c0.23,0,0.42,0.08,0.58,0.24c0.16,0.16,0.23,0.35,0.23,0.59c0,0.24-0.08,0.44-0.23,0.6c-0.16,0.17-0.35,0.25-0.58,0.25H6.09 c-0.24,0-0.44-0.08-0.6-0.25C5.32,18.34,5.24,18.14,5.24,17.91z M5.37,15.52c0,0.09,0.05,0.13,0.15,0.13h1.43 c0.06,0,0.13-0.05,0.2-0.16c0.24-0.52,0.59-0.94,1.06-1.27c0.47-0.33,0.99-0.52,1.55-0.56l0.55-0.07c0.11,0,0.17-0.06,0.17-0.18 l0.07-0.5c0.11-1.08,0.56-1.98,1.37-2.7c0.81-0.72,1.76-1.08,2.85-1.08c1.08,0,2.02,0.36,2.83,1.07c0.8,0.71,1.26,1.61,1.37,2.68 l0.08,0.57c0,0.11,0.07,0.17,0.2,0.17h1.59c0.64,0,1.23,0.17,1.76,0.52s0.92,0.8,1.18,1.37c0.07,0.11,0.14,0.16,0.21,0.16h1.43 c0.12,0,0.17-0.07,0.14-0.23c-0.29-1.02-0.88-1.86-1.74-2.51c-0.87-0.65-1.86-0.97-2.97-0.97h-0.32c-0.33-1.33-1.03-2.42-2.1-3.27 s-2.28-1.27-3.65-1.27c-1.4,0-2.64,0.44-3.73,1.32s-1.78,2-2.09,3.36c-0.85,0.2-1.6,0.6-2.24,1.21c-0.64,0.61-1.09,1.33-1.34,2.18 v-0.04C5.37,15.45,5.37,15.48,5.37,15.52z M6.98,24.11c0-0.24,0.09-0.43,0.26-0.59c0.15-0.15,0.35-0.23,0.6-0.23h18.68 c0.24,0,0.44,0.08,0.6,0.23c0.17,0.16,0.25,0.35,0.25,0.58c0,0.24-0.08,0.44-0.25,0.61c-0.17,0.17-0.37,0.25-0.6,0.25H7.84 c-0.23,0-0.43-0.09-0.6-0.26C7.07,24.55,6.98,24.34,6.98,24.11z",
    "wi-na": "M6.87,18.23h1.94v-3.64h0.02l2.05,3.64h1.99v-6.66h-1.94v3.55h-0.02l-1.94-3.55h-2.1V18.23z M13.39,18.38h1.43l2.61-6.97 h-1.42L13.39,18.38z M16.26,18.23h2.07l0.29-0.95h2.12l0.28,0.95h2.13l-2.43-6.66h-2.01L16.26,18.23z M19.07,15.84l0.64-2.04h0.03 l0.6,2.04H19.07z",
    "wi-night-alt-cloudy": "M4.14,16.9c0-1.16,0.35-2.18,1.06-3.08s1.62-1.47,2.74-1.72c0.23-1.03,0.7-1.93,1.4-2.7c0.7-0.77,1.55-1.32,2.53-1.65 c0.62-0.21,1.26-0.32,1.93-0.32c0.81,0,1.6,0.16,2.35,0.48c0.28-0.47,0.61-0.88,0.99-1.22c0.38-0.34,0.77-0.61,1.17-0.79 c0.4-0.18,0.8-0.32,1.18-0.41s0.76-0.13,1.12-0.13c0.38,0,0.79,0.05,1.23,0.16l0.82,0.25c0.14,0.06,0.18,0.13,0.14,0.22l-0.14,0.6 c-0.07,0.31-0.1,0.6-0.1,0.86c0,0.31,0.05,0.63,0.15,0.95c0.1,0.32,0.24,0.63,0.44,0.94c0.19,0.31,0.46,0.58,0.8,0.83 c0.34,0.25,0.72,0.44,1.15,0.57l0.62,0.22c0.1,0.03,0.15,0.08,0.15,0.16c0,0.02-0.01,0.04-0.02,0.07l-0.18,0.67 c-0.27,1.08-0.78,1.93-1.5,2.57c0.4,0.7,0.62,1.45,0.65,2.24c0.01,0.05,0.01,0.12,0.01,0.23c0,0.89-0.22,1.72-0.67,2.48 c-0.44,0.76-1.05,1.36-1.8,1.8c-0.76,0.44-1.59,0.67-2.48,0.67H9.07c-0.89,0-1.72-0.22-2.48-0.67s-1.35-1.05-1.79-1.8 S4.14,17.8,4.14,16.9z M5.85,16.9c0,0.89,0.32,1.66,0.96,2.31c0.64,0.65,1.39,0.98,2.26,0.98h10.81c0.89,0,1.65-0.32,2.28-0.97 s0.95-1.42,0.95-2.32c0-0.88-0.32-1.63-0.96-2.26c-0.64-0.63-1.4-0.95-2.28-0.95h-1.78l-0.1-0.75c-0.1-1.01-0.52-1.88-1.26-2.59 s-1.62-1.11-2.63-1.2c-0.03,0-0.08,0-0.15-0.01c-0.07-0.01-0.11-0.01-0.15-0.01c-0.51,0-1.02,0.1-1.54,0.29V9.4 c-0.73,0.28-1.35,0.74-1.84,1.37c-0.5,0.63-0.8,1.35-0.9,2.17l-0.07,0.72l-0.68,0.03c-0.84,0.1-1.54,0.45-2.1,1.06 S5.85,16.07,5.85,16.9z M17.6,8.79c1.06,0.91,1.72,1.97,1.97,3.18h0.32c1.24,0,2.3,0.39,3.17,1.18c0.33-0.31,0.58-0.67,0.76-1.07 c-0.91-0.43-1.63-1.09-2.16-1.97c-0.52-0.88-0.79-1.81-0.79-2.78V7.09c-0.05-0.01-0.13-0.01-0.24-0.01 c-0.58-0.01-1.15,0.13-1.7,0.44C18.38,7.82,17.93,8.24,17.6,8.79z",
    "wi-night-alt-partly-cloudy": "M6.77,19.65c0-0.79,0.23-1.48,0.68-2.09c0.45-0.61,1.06-1.03,1.81-1.27c0.32-1.09,0.98-1.92,1.99-2.49v-0.35 c0-1.46,0.46-2.74,1.38-3.85s2.09-1.8,3.5-2.06c0.36-0.06,0.72-0.09,1.08-0.09h0.03c0.21,0,0.44,0.02,0.7,0.05 c0.26,0.02,0.5,0.06,0.73,0.11l0.91,0.28c0.13,0.07,0.18,0.16,0.16,0.26l-0.13,0.7C19.54,9.18,19.5,9.5,19.5,9.82 c0,0.35,0.05,0.71,0.16,1.07c0.11,0.37,0.27,0.72,0.5,1.08s0.52,0.68,0.91,0.97c0.38,0.29,0.83,0.51,1.33,0.66l0.71,0.21 c0.11,0.03,0.17,0.08,0.17,0.18c0,0.04,0,0.06-0.01,0.07l-0.18,0.68c-0.06,0.24-0.13,0.49-0.22,0.73c-0.15,0.44-0.38,0.89-0.7,1.37 c0-0.01,0-0.01-0.01-0.01c-0.44,0.63-0.98,1.16-1.64,1.58c0.14,0.34,0.21,0.75,0.21,1.24c0,0.99-0.35,1.83-1.04,2.53 c-0.69,0.7-1.52,1.05-2.49,1.05h-6.85c-0.97,0-1.81-0.35-2.52-1.06C7.13,21.46,6.77,20.62,6.77,19.65z M8.75,19.65 c0,0.45,0.15,0.83,0.46,1.15s0.69,0.47,1.14,0.47h6.85c0.43,0,0.8-0.16,1.12-0.48c0.32-0.32,0.47-0.7,0.47-1.14 c0-0.43-0.16-0.8-0.47-1.12s-0.69-0.47-1.12-0.47H15.9c-0.11,0-0.19-0.07-0.25-0.23l-0.08-0.64c-0.07-0.58-0.32-1.06-0.75-1.44 s-0.93-0.58-1.51-0.58c-0.57,0-1.06,0.19-1.48,0.58c-0.42,0.39-0.66,0.87-0.73,1.44l-0.1,0.55c0,0.15-0.06,0.22-0.19,0.22 l-0.63,0.08c-0.41,0.04-0.75,0.21-1.02,0.51C8.89,18.87,8.75,19.23,8.75,19.65z M13.18,13.25h0.12c0.93,0,1.75,0.26,2.49,0.78 c0.73,0.52,1.25,1.22,1.54,2.1c0.77,0,1.45,0.24,2.03,0.72c0.69-0.43,1.2-1.02,1.53-1.75c-1.04-0.52-1.85-1.27-2.43-2.25 s-0.88-2.01-0.88-3.11V9.39c-0.03,0-0.07,0-0.12,0c-0.05,0-0.09,0-0.12,0c-0.61,0-1.2,0.13-1.77,0.39 c-0.57,0.26-1.05,0.64-1.44,1.12l-0.03-0.02C13.55,11.56,13.25,12.35,13.18,13.25z",
    "wi-night-alt-rain": "M4.07,16.9c0,1.33,0.47,2.48,1.4,3.44s2.07,1.47,3.4,1.53c0.12,0,0.18-0.06,0.18-0.17v-1.34c0-0.11-0.06-0.17-0.18-0.17 c-0.86-0.05-1.59-0.39-2.19-1.03c-0.6-0.64-0.9-1.39-0.9-2.26c0-0.83,0.28-1.55,0.85-2.17c0.57-0.62,1.27-0.97,2.1-1.07l0.53-0.04 c0.13,0,0.2-0.06,0.2-0.17l0.07-0.54c0.11-1.08,0.57-1.99,1.38-2.72c0.81-0.73,1.77-1.1,2.86-1.1c1.09,0,2.04,0.37,2.86,1.1 c0.82,0.73,1.28,1.64,1.4,2.72l0.08,0.57c0,0.12,0.06,0.18,0.17,0.18h1.62c0.89,0,1.67,0.32,2.32,0.96c0.65,0.64,0.98,1.4,0.98,2.28 c0,0.87-0.3,1.62-0.9,2.26c-0.6,0.64-1.33,0.98-2.19,1.03c-0.14,0-0.21,0.06-0.21,0.17v1.34c0,0.11,0.07,0.17,0.21,0.17 c1.33-0.04,2.46-0.55,3.38-1.51c0.93-0.97,1.39-2.12,1.39-3.45c0-0.88-0.23-1.7-0.68-2.46c0.81-0.73,1.33-1.6,1.58-2.62l0.14-0.72 c0.01-0.01,0.02-0.03,0.02-0.07c0-0.07-0.05-0.13-0.16-0.16l-0.56-0.18c-0.57-0.16-1.06-0.44-1.46-0.83 c-0.41-0.39-0.7-0.8-0.87-1.23c-0.17-0.43-0.26-0.86-0.26-1.28c-0.02-0.22,0.01-0.5,0.08-0.82l0.14-0.61c0.04-0.1,0-0.18-0.14-0.24 l-0.79-0.24c-0.45-0.1-0.87-0.15-1.27-0.15c-0.38,0-0.76,0.04-1.14,0.13c-0.39,0.09-0.79,0.22-1.2,0.41 c-0.41,0.18-0.81,0.45-1.2,0.8c-0.39,0.35-0.72,0.75-1,1.22c-0.82-0.3-1.6-0.45-2.33-0.45c-1.41,0-2.67,0.44-3.76,1.32 s-1.8,2-2.11,3.37c-1.11,0.26-2.02,0.83-2.74,1.73C4.43,14.72,4.07,15.75,4.07,16.9z M9.63,23.74c0,0.17,0.05,0.33,0.16,0.49 c0.11,0.16,0.27,0.27,0.49,0.33c0.23,0.07,0.45,0.05,0.64-0.04c0.2-0.1,0.33-0.28,0.4-0.56l1.43-5.87c0.06-0.25,0.03-0.48-0.08-0.67 c-0.12-0.2-0.29-0.32-0.52-0.37c-0.22-0.07-0.43-0.05-0.63,0.07c-0.2,0.11-0.34,0.28-0.41,0.51l-1.44,5.9 c0,0.01-0.01,0.04-0.02,0.09C9.64,23.67,9.63,23.71,9.63,23.74z M12.24,26.81c0,0.16,0.05,0.31,0.15,0.46 c0.1,0.15,0.25,0.25,0.45,0.31c0.11,0.03,0.19,0.04,0.24,0.04c0.44,0,0.71-0.2,0.82-0.59l2.25-8.93c0.06-0.24,0.04-0.46-0.07-0.65 c-0.11-0.19-0.28-0.32-0.5-0.39c-0.23-0.07-0.45-0.05-0.66,0.07s-0.34,0.28-0.39,0.5l-2.26,8.92c0,0.01,0,0.05-0.01,0.12 C12.24,26.73,12.24,26.78,12.24,26.81z M16.4,23.82c0,0.36,0.21,0.6,0.63,0.74c0.14,0.04,0.24,0.06,0.3,0.06 c0.11,0,0.23-0.02,0.35-0.07c0.21-0.08,0.34-0.28,0.39-0.58l1.43-5.87c0.06-0.24,0.04-0.45-0.08-0.65 c-0.11-0.19-0.28-0.32-0.51-0.39c-0.23-0.07-0.45-0.05-0.66,0.07c-0.21,0.11-0.33,0.28-0.38,0.51l-1.43,5.9 C16.42,23.7,16.4,23.8,16.4,23.82z M17.58,8.77c0.32-0.58,0.75-1.02,1.31-1.33c0.55-0.3,1.14-0.45,1.76-0.44 c0.12,0,0.21,0,0.27,0.01v0.3c-0.01,0.97,0.24,1.88,0.77,2.75c0.52,0.86,1.26,1.52,2.21,1.97c-0.22,0.46-0.49,0.81-0.79,1.07 c-0.92-0.76-1.99-1.13-3.23-1.13h-0.31C19.3,10.7,18.64,9.64,17.58,8.77z",
    "wi-night-alt-showers": "M4.07,16.91c0,1.33,0.46,2.48,1.39,3.43s2.06,1.47,3.4,1.53c0.11,0,0.17-0.06,0.17-0.17v-1.37c0-0.12-0.06-0.18-0.17-0.18 c-0.87-0.07-1.6-0.41-2.19-1.04c-0.59-0.62-0.89-1.36-0.89-2.21c0-0.83,0.28-1.54,0.84-2.16s1.26-0.97,2.1-1.07l0.53-0.07 c0.13,0,0.2-0.06,0.2-0.17l0.07-0.52c0.11-1.08,0.56-1.99,1.37-2.71c0.81-0.73,1.76-1.09,2.85-1.09c1.09,0,2.04,0.36,2.85,1.09 c0.81,0.72,1.28,1.63,1.39,2.72l0.08,0.58c0,0.12,0.06,0.18,0.18,0.18h1.61c0.9,0,1.67,0.32,2.32,0.95 c0.64,0.63,0.97,1.39,0.97,2.28c0,0.85-0.3,1.59-0.89,2.21c-0.59,0.62-1.33,0.97-2.19,1.04c-0.13,0-0.2,0.06-0.2,0.18v1.37 c0,0.11,0.07,0.17,0.2,0.17c1.33-0.04,2.46-0.55,3.38-1.51c0.92-0.96,1.38-2.11,1.38-3.45c0-0.87-0.22-1.68-0.65-2.43 c0.81-0.73,1.34-1.6,1.58-2.62v-0.13l0.19-0.79l-0.76-0.21c-0.81-0.24-1.44-0.7-1.89-1.35c-0.45-0.66-0.67-1.34-0.67-2.03 c0-0.26,0.03-0.52,0.08-0.78l0.2-0.8l-0.85-0.25L21.9,5.49c-0.47-0.09-0.88-0.14-1.25-0.14c-0.38,0-0.76,0.04-1.14,0.13 c-0.39,0.09-0.79,0.22-1.2,0.41c-0.42,0.19-0.82,0.45-1.2,0.8c-0.38,0.35-0.72,0.76-1,1.23c-0.74-0.33-1.53-0.49-2.36-0.49 c-1.41,0-2.66,0.44-3.75,1.31s-1.77,1.99-2.07,3.36c-1.12,0.26-2.05,0.83-2.77,1.72C4.43,14.73,4.07,15.76,4.07,16.91z M9.47,23.68 c0,0.15,0.05,0.3,0.15,0.45c0.1,0.15,0.25,0.26,0.45,0.33c0.22,0.07,0.43,0.06,0.64-0.05s0.34-0.28,0.41-0.51l0.28-1.06 c0.07-0.21,0.05-0.41-0.07-0.62c-0.12-0.21-0.29-0.34-0.51-0.41c-0.23-0.06-0.45-0.03-0.65,0.08s-0.34,0.3-0.42,0.53l-0.23,0.99 C9.49,23.57,9.47,23.66,9.47,23.68z M10.77,18.95c0,0.11,0.03,0.23,0.1,0.36c0.07,0.17,0.25,0.3,0.53,0.38 c0.24,0.06,0.46,0.04,0.66-0.06c0.19-0.1,0.33-0.28,0.4-0.52l0.28-1.03c0.07-0.23,0.05-0.45-0.07-0.64 c-0.12-0.2-0.29-0.33-0.51-0.39c-0.24-0.06-0.47-0.04-0.67,0.07c-0.2,0.11-0.33,0.28-0.4,0.52l-0.27,1.01 C10.79,18.78,10.77,18.88,10.77,18.95z M12.02,26.8c0,0.17,0.05,0.33,0.15,0.49c0.1,0.16,0.25,0.27,0.45,0.33 c0.11,0.03,0.18,0.05,0.23,0.05c0.09,0,0.21-0.03,0.38-0.1c0.2-0.08,0.34-0.27,0.43-0.55l0.3-1.05c0.07-0.21,0.05-0.42-0.07-0.63 c-0.12-0.21-0.29-0.34-0.51-0.41c-0.24-0.06-0.47-0.04-0.67,0.08c-0.2,0.12-0.34,0.29-0.41,0.53l-0.25,1.01 C12.03,26.63,12.02,26.72,12.02,26.8z M13.35,22.03c0,0.15,0.05,0.3,0.15,0.45s0.25,0.26,0.46,0.33c0.22,0.07,0.44,0.05,0.64-0.06 c0.2-0.11,0.33-0.28,0.4-0.52l0.27-1.04c0.07-0.21,0.05-0.42-0.06-0.62c-0.11-0.2-0.27-0.34-0.49-0.41 c-0.24-0.06-0.47-0.03-0.68,0.08s-0.35,0.3-0.42,0.53l-0.24,1L13.35,22.03z M16.16,23.79c0,0.38,0.21,0.62,0.64,0.75 c0.09,0.02,0.17,0.03,0.24,0.03c0.15,0,0.27-0.02,0.37-0.07c0.21-0.08,0.36-0.27,0.44-0.57l0.27-1.02c0.06-0.25,0.04-0.47-0.08-0.67 s-0.29-0.32-0.53-0.37c-0.23-0.07-0.45-0.05-0.64,0.07s-0.33,0.29-0.4,0.51l-0.27,1.04c0,0.02-0.01,0.07-0.02,0.15 C16.16,23.71,16.16,23.76,16.16,23.79z M17.55,18.98c0,0.16,0.05,0.31,0.15,0.46c0.1,0.15,0.26,0.26,0.46,0.32 c0.14,0.03,0.22,0.05,0.23,0.05c0.09,0,0.21-0.03,0.38-0.1c0.21-0.08,0.35-0.27,0.44-0.55l0.28-1.04c0.06-0.22,0.03-0.43-0.08-0.63 s-0.3-0.33-0.53-0.4c-0.22-0.07-0.43-0.05-0.63,0.07s-0.33,0.29-0.4,0.52l-0.26,1.06C17.56,18.83,17.55,18.92,17.55,18.98z M17.58,8.81c0.32-0.56,0.76-1,1.33-1.31c0.57-0.31,1.17-0.47,1.81-0.47h0.21c-0.01,0.09-0.01,0.21-0.01,0.38 c0,0.95,0.26,1.85,0.78,2.71c0.52,0.86,1.25,1.51,2.17,1.96c-0.22,0.43-0.48,0.8-0.78,1.1c-0.93-0.81-2.02-1.21-3.25-1.21h-0.32 C19.27,10.78,18.63,9.73,17.58,8.81z",
    "wi-night-alt-snow": "M4.07,16.93c0,1.33,0.47,2.47,1.4,3.43s2.07,1.47,3.4,1.51c0.12,0,0.18-0.06,0.18-0.17v-1.34c0-0.11-0.06-0.17-0.18-0.17 c-0.85-0.04-1.58-0.39-2.18-1.03c-0.61-0.64-0.91-1.39-0.91-2.24c0-0.85,0.28-1.58,0.85-2.2c0.57-0.62,1.27-0.96,2.1-1.03l0.53-0.07 c0.13,0,0.2-0.06,0.2-0.17l0.07-0.52c0.11-1.09,0.56-2.01,1.37-2.75s1.76-1.11,2.86-1.11c1.09,0,2.04,0.37,2.86,1.1 c0.82,0.73,1.28,1.64,1.4,2.72l0.08,0.57c0,0.12,0.06,0.18,0.17,0.18h1.62c0.91,0,1.68,0.32,2.33,0.97 c0.65,0.64,0.97,1.41,0.97,2.31c0,0.85-0.3,1.6-0.91,2.24c-0.61,0.64-1.33,0.98-2.18,1.03c-0.14,0-0.21,0.06-0.21,0.17v1.34 c0,0.11,0.07,0.17,0.21,0.17c0.88-0.02,1.68-0.26,2.41-0.71c0.73-0.45,1.31-1.05,1.73-1.8s0.63-1.56,0.63-2.43 c0-0.91-0.22-1.74-0.65-2.48c0.74-0.66,1.24-1.52,1.52-2.58l0.17-0.72c0.01-0.01,0.02-0.04,0.02-0.08c0-0.07-0.05-0.13-0.16-0.16 l-0.61-0.17c-0.44-0.13-0.83-0.32-1.17-0.57s-0.61-0.53-0.81-0.84c-0.2-0.31-0.34-0.62-0.44-0.95c-0.1-0.32-0.15-0.64-0.15-0.95 c0-0.27,0.03-0.56,0.1-0.86l0.11-0.62c0.02-0.09-0.02-0.17-0.14-0.22l-0.8-0.24c-0.44-0.11-0.85-0.16-1.25-0.16 c-0.37,0-0.74,0.04-1.12,0.13c-0.38,0.09-0.77,0.22-1.18,0.41c-0.41,0.19-0.8,0.45-1.18,0.8c-0.38,0.35-0.71,0.75-0.99,1.22 c-0.81-0.33-1.6-0.5-2.38-0.5c-1.41,0-2.67,0.44-3.76,1.32s-1.8,2-2.11,3.37c-1.12,0.28-2.04,0.87-2.75,1.76 C4.43,14.74,4.07,15.77,4.07,16.93z M10.46,21.02c0,0.24,0.08,0.44,0.24,0.6c0.16,0.17,0.35,0.25,0.59,0.25 c0.24,0,0.44-0.08,0.6-0.25s0.24-0.37,0.24-0.6c0-0.22-0.08-0.42-0.24-0.58s-0.36-0.24-0.6-0.24c-0.23,0-0.43,0.08-0.59,0.24 C10.54,20.6,10.46,20.79,10.46,21.02z M10.46,24.66c0,0.23,0.08,0.42,0.24,0.58c0.16,0.16,0.36,0.24,0.58,0.24 c0.24,0,0.44-0.08,0.6-0.23c0.16-0.16,0.24-0.35,0.24-0.59c0-0.24-0.08-0.43-0.24-0.59c-0.16-0.16-0.36-0.23-0.6-0.23 c-0.24,0-0.43,0.08-0.59,0.23C10.54,24.22,10.46,24.42,10.46,24.66z M13.66,22.96c0,0.24,0.08,0.44,0.24,0.59 c0.16,0.16,0.36,0.24,0.58,0.24c0.24,0,0.44-0.08,0.61-0.24s0.25-0.36,0.25-0.59c0-0.24-0.08-0.44-0.25-0.61s-0.37-0.26-0.61-0.26 c-0.22,0-0.41,0.09-0.58,0.26S13.66,22.72,13.66,22.96z M13.66,19.32c0,0.24,0.08,0.43,0.24,0.58c0.16,0.16,0.36,0.24,0.58,0.24 c0.24,0,0.45-0.08,0.61-0.23s0.25-0.35,0.25-0.59c0-0.23-0.08-0.43-0.25-0.6s-0.37-0.25-0.61-0.25c-0.22,0-0.42,0.08-0.58,0.25 S13.66,19.09,13.66,19.32z M13.66,26.63c0,0.22,0.08,0.41,0.24,0.57c0.17,0.17,0.36,0.25,0.58,0.25c0.24,0,0.44-0.08,0.61-0.24 c0.17-0.16,0.25-0.35,0.25-0.59c0-0.24-0.08-0.44-0.25-0.61c-0.17-0.17-0.37-0.26-0.61-0.26c-0.22,0-0.41,0.09-0.58,0.26 C13.75,26.19,13.66,26.4,13.66,26.63z M16.9,21.02c0,0.24,0.08,0.44,0.25,0.6s0.36,0.25,0.6,0.25s0.43-0.08,0.59-0.25 s0.24-0.37,0.24-0.6c0-0.22-0.08-0.42-0.24-0.58s-0.35-0.24-0.59-0.24s-0.43,0.08-0.6,0.24S16.9,20.79,16.9,21.02z M16.9,24.66 c0,0.23,0.08,0.42,0.24,0.58c0.16,0.16,0.36,0.24,0.6,0.24s0.43-0.08,0.59-0.24c0.16-0.16,0.23-0.35,0.23-0.59 c0-0.24-0.08-0.43-0.23-0.59c-0.16-0.16-0.35-0.23-0.59-0.23s-0.44,0.08-0.6,0.23C16.98,24.22,16.9,24.42,16.9,24.66z M17.58,8.77 c0.31-0.54,0.75-0.96,1.3-1.26S20,7.06,20.59,7.05c0.15,0,0.26,0.01,0.33,0.02v0.31c0,0.97,0.26,1.88,0.78,2.74s1.25,1.51,2.17,1.96 c-0.16,0.36-0.41,0.72-0.76,1.07c-0.89-0.79-1.96-1.18-3.23-1.18h-0.31C19.3,10.74,18.64,9.68,17.58,8.77z",
    "wi-night-alt-sprinkle": "M4.11,16.89c0,1.33,0.46,2.48,1.39,3.43c0.93,0.96,2.06,1.47,3.4,1.54c0.12,0,0.18-0.06,0.18-0.18v-1.33 c0-0.12-0.06-0.18-0.18-0.18c-0.86-0.04-1.58-0.38-2.18-1.02c-0.6-0.64-0.9-1.39-0.9-2.26c0-0.83,0.28-1.55,0.84-2.17 c0.56-0.61,1.26-0.97,2.1-1.07l0.52-0.04c0.13,0,0.2-0.06,0.2-0.18l0.07-0.54c0.11-1.08,0.56-1.99,1.37-2.71 c0.81-0.73,1.76-1.09,2.85-1.09c1.09,0,2.04,0.37,2.86,1.1c0.82,0.73,1.28,1.63,1.4,2.71l0.07,0.57c0,0.12,0.06,0.19,0.18,0.19h1.62 c0.89,0,1.65,0.32,2.3,0.96s0.97,1.4,0.97,2.27c0,0.87-0.3,1.62-0.9,2.26c-0.6,0.64-1.33,0.98-2.18,1.02 c-0.12,0-0.19,0.06-0.19,0.18v1.33c0,0.12,0.06,0.18,0.19,0.18c0.88-0.03,1.68-0.27,2.41-0.72s1.31-1.05,1.73-1.8 s0.63-1.57,0.63-2.44c0-0.87-0.23-1.68-0.68-2.45c0.78-0.74,1.29-1.6,1.54-2.58l0.14-0.73c0.01-0.01,0.02-0.03,0.02-0.07 c0-0.07-0.05-0.13-0.16-0.16l-0.57-0.17c-0.57-0.16-1.06-0.44-1.46-0.82c-0.41-0.38-0.7-0.79-0.87-1.21 c-0.17-0.43-0.26-0.85-0.26-1.28c0-0.29,0.04-0.57,0.11-0.85l0.13-0.61c0.02-0.1-0.02-0.18-0.13-0.23l-0.8-0.24 c-0.45-0.1-0.87-0.15-1.27-0.15c-0.36,0-0.73,0.04-1.12,0.13c-0.38,0.09-0.78,0.22-1.19,0.4s-0.81,0.44-1.2,0.79s-0.72,0.74-1,1.2 c-0.81-0.31-1.59-0.46-2.33-0.46c-1.41,0-2.67,0.44-3.76,1.32s-1.8,2-2.11,3.36c-1.11,0.26-2.02,0.84-2.73,1.74 C4.47,14.71,4.11,15.74,4.11,16.89z M10.05,17.77c0,0.38,0.14,0.71,0.42,0.98c0.28,0.27,0.62,0.4,1.02,0.4c0.4,0,0.73-0.13,1-0.4 c0.27-0.27,0.4-0.59,0.4-0.98c0-0.26-0.12-0.6-0.35-1.02c-0.23-0.42-0.45-0.75-0.65-0.98c-0.11-0.12-0.24-0.26-0.41-0.43l-0.35,0.41 c-0.27,0.29-0.52,0.64-0.75,1.04S10.05,17.51,10.05,17.77z M13.04,21.76c0,0.66,0.23,1.21,0.68,1.66c0.46,0.45,1.01,0.67,1.65,0.67 c0.66,0,1.21-0.23,1.66-0.68c0.45-0.46,0.68-1.01,0.68-1.65c0-0.55-0.27-1.22-0.8-2c-0.44-0.58-0.87-1.08-1.28-1.49 c-0.08-0.06-0.17-0.13-0.26-0.23l-0.23,0.23c-0.39,0.36-0.82,0.86-1.28,1.48c-0.24,0.33-0.43,0.68-0.59,1.04 C13.11,21.16,13.04,21.48,13.04,21.76z M14.51,15.09c0,0.26,0.1,0.47,0.29,0.66s0.42,0.27,0.7,0.27c0.26,0,0.47-0.09,0.66-0.27 c0.18-0.18,0.27-0.4,0.27-0.66c0-0.43-0.31-0.97-0.93-1.62l-0.25,0.27c-0.18,0.2-0.35,0.43-0.5,0.7 C14.58,14.71,14.51,14.93,14.51,15.09z M17.56,8.77c0.35-0.57,0.8-1,1.34-1.29c0.54-0.29,1.12-0.44,1.72-0.44 c0.12,0,0.21,0.01,0.27,0.02v0.3c0,0.96,0.26,1.87,0.79,2.74s1.25,1.52,2.18,1.97c-0.16,0.38-0.41,0.72-0.75,1.03 c-0.93-0.76-1.99-1.14-3.21-1.14h-0.33C19.27,10.65,18.6,9.59,17.56,8.77z",
    "wi-night-clear": "M7.91,14.48c0-0.96,0.19-1.87,0.56-2.75s0.88-1.63,1.51-2.26c0.63-0.63,1.39-1.14,2.27-1.52c0.88-0.38,1.8-0.57,2.75-0.57 h1.14c0.16,0.04,0.23,0.14,0.23,0.28l0.05,0.88c0.04,1.27,0.49,2.35,1.37,3.24c0.88,0.89,1.94,1.37,3.19,1.42l0.82,0.07 c0.16,0,0.24,0.08,0.24,0.23v0.98c0.01,1.28-0.3,2.47-0.93,3.56c-0.63,1.09-1.48,1.95-2.57,2.59c-1.08,0.63-2.27,0.95-3.55,0.95 c-0.97,0-1.9-0.19-2.78-0.56s-1.63-0.88-2.26-1.51c-0.63-0.63-1.13-1.39-1.5-2.26C8.1,16.37,7.91,15.45,7.91,14.48z M9.74,14.48 c0,0.76,0.15,1.48,0.45,2.16c0.3,0.67,0.7,1.24,1.19,1.7c0.49,0.46,1.05,0.82,1.69,1.08c0.63,0.27,1.28,0.4,1.94,0.4 c0.58,0,1.17-0.11,1.76-0.34c0.59-0.23,1.14-0.55,1.65-0.96c0.51-0.41,0.94-0.93,1.31-1.57c0.37-0.64,0.6-1.33,0.71-2.09 c-1.63-0.34-2.94-1.04-3.92-2.1s-1.55-2.3-1.7-3.74C13.86,9.08,13,9.37,12.21,9.9c-0.78,0.53-1.39,1.2-1.82,2.02 C9.96,12.74,9.74,13.59,9.74,14.48z",
    "wi-night-fog": "M2.66,20.92c0,0.23,0.08,0.42,0.25,0.57c0.17,0.16,0.38,0.23,0.62,0.23h18.61c0.24,0,0.44-0.08,0.6-0.23 c0.17-0.16,0.25-0.35,0.25-0.57c0-0.24-0.08-0.45-0.24-0.61c-0.16-0.17-0.37-0.25-0.61-0.25H3.53c-0.24,0-0.44,0.08-0.61,0.25 C2.75,20.48,2.66,20.69,2.66,20.92z M5.27,17.81c0,0.24,0.09,0.43,0.26,0.59c0.14,0.18,0.33,0.27,0.59,0.27h18.61 c0.23,0,0.42-0.08,0.58-0.25s0.23-0.37,0.23-0.61c0-0.23-0.08-0.43-0.23-0.58C25.16,17.08,24.96,17,24.73,17H6.12 c-0.24,0-0.44,0.08-0.6,0.23C5.35,17.39,5.27,17.58,5.27,17.81z M5.42,15.39v-0.05c-0.04,0.15,0,0.22,0.12,0.22h1.44 c0.06,0,0.12-0.05,0.19-0.15c0.24-0.52,0.59-0.94,1.06-1.27c0.47-0.33,0.99-0.52,1.55-0.56l0.53-0.08c0.12,0,0.19-0.06,0.19-0.18 l0.06-0.5c0.11-1.08,0.56-1.97,1.36-2.7c0.8-0.72,1.75-1.08,2.84-1.08c1.07,0,2.02,0.36,2.82,1.07s1.27,1.6,1.38,2.67l0.07,0.57 c0,0.12,0.07,0.18,0.21,0.18h1.58c0.64,0,1.23,0.17,1.75,0.52c0.52,0.34,0.92,0.8,1.17,1.36c0.07,0.1,0.14,0.15,0.22,0.15h1.42 c0.12,0,0.17-0.07,0.15-0.22c-0.22-0.56-0.37-0.91-0.46-1.06c0.72-0.65,1.23-1.51,1.5-2.57l0.17-0.66c0.03-0.06,0.02-0.12-0.01-0.16 c-0.03-0.04-0.07-0.07-0.12-0.07l-0.62-0.22c-0.89-0.26-1.57-0.78-2.04-1.58c-0.47-0.8-0.59-1.65-0.37-2.56l0.13-0.58 c0.05-0.09,0.01-0.17-0.13-0.23l-0.84-0.23c-1.09-0.27-2.17-0.18-3.22,0.26c-1.05,0.44-1.87,1.15-2.47,2.12 c-0.79-0.31-1.56-0.46-2.29-0.46c-1.39,0-2.62,0.44-3.71,1.31s-1.78,1.99-2.1,3.35c-0.84,0.2-1.58,0.6-2.22,1.21 S5.67,14.55,5.42,15.39z M7,23.97c0,0.24,0.09,0.43,0.26,0.59c0.17,0.18,0.37,0.27,0.59,0.27H26.5c0.23,0,0.43-0.08,0.59-0.25 c0.16-0.17,0.24-0.37,0.24-0.61c0-0.23-0.08-0.42-0.24-0.58s-0.36-0.23-0.59-0.23H7.86c-0.24,0-0.44,0.08-0.6,0.23 C7.09,23.55,7,23.74,7,23.97z M18.51,8.7c0.35-0.57,0.82-1.02,1.41-1.33c0.59-0.31,1.21-0.44,1.87-0.38 c-0.07,1.04,0.17,2.02,0.7,2.93c0.54,0.91,1.28,1.58,2.22,2.02c-0.15,0.35-0.4,0.71-0.75,1.07c-0.92-0.76-1.97-1.13-3.14-1.13H20.5 C20.18,10.57,19.52,9.51,18.51,8.7z",
    "wi-rain": "M4.64,16.91c0-1.15,0.36-2.17,1.08-3.07c0.72-0.9,1.63-1.47,2.73-1.73c0.31-1.36,1.02-2.48,2.11-3.36s2.34-1.31,3.75-1.31 c1.38,0,2.6,0.43,3.68,1.28c1.08,0.85,1.78,1.95,2.1,3.29h0.32c0.89,0,1.72,0.22,2.48,0.65s1.37,1.03,1.81,1.78 c0.44,0.75,0.67,1.58,0.67,2.47c0,0.88-0.21,1.69-0.63,2.44c-0.42,0.75-1,1.35-1.73,1.8c-0.73,0.45-1.53,0.69-2.4,0.71 c-0.13,0-0.2-0.06-0.2-0.17v-1.33c0-0.12,0.07-0.18,0.2-0.18c0.85-0.04,1.58-0.38,2.18-1.02s0.9-1.39,0.9-2.26s-0.33-1.62-0.98-2.26 s-1.42-0.96-2.31-0.96h-1.61c-0.12,0-0.18-0.06-0.18-0.17l-0.08-0.58c-0.11-1.08-0.58-1.99-1.39-2.71 c-0.82-0.73-1.76-1.09-2.85-1.09c-1.09,0-2.05,0.36-2.85,1.09c-0.81,0.73-1.26,1.63-1.36,2.71l-0.07,0.53c0,0.12-0.07,0.19-0.2,0.19 l-0.53,0.03c-0.83,0.1-1.53,0.46-2.1,1.07s-0.85,1.33-0.85,2.16c0,0.87,0.3,1.62,0.9,2.26s1.33,0.98,2.18,1.02 c0.11,0,0.17,0.06,0.17,0.18v1.33c0,0.11-0.06,0.17-0.17,0.17c-1.34-0.06-2.47-0.57-3.4-1.53S4.64,18.24,4.64,16.91z M9.99,23.6 c0-0.04,0.01-0.11,0.04-0.2l1.63-5.77c0.06-0.19,0.17-0.34,0.32-0.44c0.15-0.1,0.31-0.15,0.46-0.15c0.07,0,0.15,0.01,0.24,0.03 c0.24,0.04,0.42,0.17,0.54,0.37c0.12,0.2,0.15,0.42,0.08,0.67l-1.63,5.73c-0.12,0.43-0.4,0.64-0.82,0.64 c-0.04,0-0.07-0.01-0.11-0.02c-0.06-0.02-0.09-0.03-0.1-0.03c-0.22-0.06-0.38-0.17-0.49-0.33C10.04,23.93,9.99,23.77,9.99,23.6z M12.61,26.41l2.44-8.77c0.04-0.19,0.14-0.34,0.3-0.44c0.16-0.1,0.32-0.15,0.49-0.15c0.09,0,0.18,0.01,0.27,0.03 c0.22,0.06,0.38,0.19,0.49,0.39c0.11,0.2,0.13,0.41,0.07,0.64l-2.43,8.78c-0.04,0.17-0.13,0.31-0.29,0.43 c-0.16,0.12-0.32,0.18-0.51,0.18c-0.09,0-0.18-0.02-0.25-0.05c-0.2-0.05-0.37-0.18-0.52-0.39C12.56,26.88,12.54,26.67,12.61,26.41z M16.74,23.62c0-0.04,0.01-0.11,0.04-0.23l1.63-5.77c0.06-0.19,0.16-0.34,0.3-0.44c0.15-0.1,0.3-0.15,0.46-0.15 c0.08,0,0.17,0.01,0.26,0.03c0.21,0.06,0.36,0.16,0.46,0.31c0.1,0.15,0.15,0.31,0.15,0.47c0,0.03-0.01,0.08-0.02,0.14 s-0.02,0.1-0.02,0.12l-1.63,5.73c-0.04,0.19-0.13,0.35-0.28,0.46s-0.32,0.17-0.51,0.17l-0.24-0.05c-0.2-0.06-0.35-0.16-0.46-0.32 C16.79,23.94,16.74,23.78,16.74,23.62z",
    "wi-rain-mix": "M4.65,16.96c0,1.32,0.47,2.46,1.4,3.41c0.93,0.96,2.06,1.46,3.38,1.5c0.12,0,0.18-0.06,0.18-0.17v-1.33 c0-0.12-0.06-0.18-0.18-0.18c-0.84-0.04-1.57-0.38-2.17-1.02s-0.91-1.37-0.91-2.22c0-0.84,0.28-1.57,0.85-2.19 c0.57-0.62,1.26-0.97,2.1-1.04l0.53-0.07c0.12,0,0.19-0.06,0.19-0.18l0.07-0.5c0.1-1.09,0.55-2.01,1.36-2.75s1.76-1.11,2.86-1.11 c1.08,0,2.03,0.37,2.84,1.1c0.81,0.73,1.28,1.63,1.4,2.71l0.07,0.58c0,0.12,0.06,0.18,0.19,0.18h1.6c0.9,0,1.67,0.32,2.32,0.97 c0.64,0.64,0.97,1.41,0.97,2.3c0,0.84-0.3,1.58-0.9,2.22c-0.6,0.63-1.33,0.97-2.18,1.02c-0.13,0-0.2,0.06-0.2,0.18v1.33 c0,0.11,0.07,0.17,0.2,0.17c1.33-0.04,2.46-0.54,3.38-1.5s1.38-2.09,1.38-3.42c0-0.89-0.22-1.72-0.67-2.48 c-0.44-0.76-1.05-1.36-1.81-1.8c-0.76-0.44-1.59-0.66-2.48-0.66h-0.31c-0.33-1.34-1.03-2.43-2.11-3.29 c-1.07-0.85-2.3-1.28-3.68-1.28c-1.41,0-2.66,0.44-3.75,1.31s-1.79,1.99-2.1,3.35c-1.13,0.29-2.04,0.88-2.75,1.77 S4.65,15.8,4.65,16.96z M10.05,23.98c0,0.17,0.05,0.34,0.16,0.51c0.11,0.17,0.27,0.28,0.47,0.35c0.23,0.07,0.44,0.06,0.64-0.04 c0.19-0.09,0.33-0.28,0.39-0.56l0.14-0.61c0.05-0.23,0.02-0.44-0.09-0.63s-0.28-0.33-0.52-0.4c-0.22-0.07-0.44-0.04-0.64,0.08 s-0.34,0.3-0.4,0.53l-0.14,0.59C10.06,23.83,10.05,23.89,10.05,23.98z M10.81,21.08c0,0.21,0.08,0.4,0.25,0.57 c0.16,0.17,0.34,0.25,0.56,0.25c0.24,0,0.44-0.08,0.6-0.24c0.16-0.16,0.24-0.35,0.24-0.59c0-0.23-0.08-0.43-0.24-0.59 c-0.16-0.16-0.36-0.24-0.6-0.24c-0.23,0-0.42,0.08-0.58,0.23C10.89,20.65,10.81,20.85,10.81,21.08z M11.42,18.81 c-0.01,0.16,0.03,0.31,0.14,0.45c0.1,0.15,0.26,0.25,0.48,0.32c0.21,0.06,0.41,0.04,0.62-0.07S13,19.23,13.07,19l0.28-0.9 c0.07-0.24,0.05-0.46-0.07-0.65c-0.12-0.19-0.3-0.32-0.54-0.39c-0.22-0.07-0.43-0.05-0.63,0.07c-0.2,0.11-0.34,0.28-0.41,0.5 l-0.24,0.92c0,0.02-0.01,0.06-0.02,0.12C11.43,18.72,11.42,18.77,11.42,18.81z M12.59,27.1c0,0.18,0.05,0.34,0.15,0.5 c0.1,0.16,0.26,0.27,0.48,0.33c0.08,0.02,0.17,0.03,0.25,0.03c0.43,0,0.69-0.2,0.79-0.61l0.14-0.59c0.06-0.26,0.03-0.48-0.08-0.68 c-0.12-0.2-0.29-0.32-0.52-0.37c-0.21-0.07-0.42-0.05-0.63,0.07c-0.21,0.12-0.34,0.29-0.41,0.51l-0.14,0.59 C12.6,26.97,12.59,27.04,12.59,27.1z M13.36,24.2c0,0.22,0.08,0.41,0.25,0.58c0.16,0.16,0.35,0.24,0.57,0.24 c0.24,0,0.43-0.08,0.59-0.23c0.16-0.16,0.23-0.35,0.23-0.59c0-0.23-0.08-0.42-0.23-0.58c-0.16-0.16-0.35-0.23-0.59-0.23 c-0.24,0-0.43,0.08-0.59,0.23S13.36,23.97,13.36,24.2z M13.99,21.93c-0.01,0.15,0.03,0.31,0.13,0.47c0.1,0.16,0.25,0.26,0.45,0.3 c0.23,0.06,0.44,0.04,0.64-0.06s0.33-0.29,0.41-0.56l0.27-0.9c0.07-0.22,0.05-0.43-0.07-0.63c-0.12-0.2-0.29-0.33-0.53-0.4 c-0.22-0.07-0.43-0.04-0.64,0.08c-0.21,0.12-0.34,0.3-0.41,0.53l-0.23,0.9C14,21.74,13.99,21.83,13.99,21.93z M16.75,24.08 c0,0.16,0.05,0.32,0.15,0.48c0.1,0.16,0.26,0.27,0.46,0.33c0.03,0,0.08,0.01,0.14,0.02c0.06,0.01,0.1,0.02,0.14,0.02 c0.41,0,0.66-0.22,0.76-0.66l0.14-0.6c0.07-0.21,0.05-0.42-0.07-0.63c-0.11-0.21-0.28-0.34-0.51-0.41 c-0.25-0.06-0.48-0.04-0.68,0.08s-0.34,0.29-0.41,0.53l-0.09,0.59c0,0.02-0.01,0.07-0.02,0.12S16.75,24.04,16.75,24.08z M17.49,21.12c0,0.22,0.08,0.42,0.25,0.57c0.15,0.16,0.34,0.24,0.57,0.24c0.24,0,0.43-0.08,0.59-0.23s0.23-0.35,0.23-0.58 c0-0.24-0.08-0.43-0.23-0.59s-0.35-0.23-0.59-0.23c-0.24,0-0.43,0.08-0.59,0.23C17.57,20.69,17.49,20.88,17.49,21.12z M18.1,18.81 c0,0.17,0.05,0.33,0.16,0.48c0.11,0.15,0.27,0.26,0.49,0.32c0.02,0,0.06,0.01,0.12,0.02s0.11,0.02,0.14,0.02 c0.1,0,0.22-0.03,0.36-0.09c0.21-0.11,0.35-0.29,0.41-0.52l0.24-0.9c0.06-0.23,0.04-0.44-0.08-0.63c-0.11-0.2-0.28-0.33-0.51-0.4 c-0.23-0.07-0.44-0.05-0.64,0.06c-0.19,0.11-0.33,0.27-0.39,0.51l-0.28,0.91c0,0.02-0.01,0.06-0.02,0.12 C18.1,18.74,18.1,18.78,18.1,18.81z",
    "wi-showers": "M4.6,16.93c0-1.16,0.36-2.18,1.09-3.08c0.72-0.9,1.65-1.48,2.78-1.73c0.29-1.38,0.98-2.5,2.07-3.39S12.88,7.4,14.3,7.4 c1.39,0,2.63,0.43,3.72,1.28c1.08,0.85,1.79,1.95,2.12,3.3h0.34c0.9,0,1.73,0.22,2.48,0.66c0.76,0.44,1.35,1.04,1.79,1.8 c0.43,0.76,0.65,1.59,0.65,2.49c0,1.34-0.46,2.48-1.37,3.44c-0.92,0.96-2.04,1.46-3.37,1.5c-0.12,0-0.18-0.06-0.18-0.17v-1.34 c0-0.11,0.06-0.17,0.18-0.17c0.84-0.07,1.57-0.42,2.17-1.05s0.9-1.37,0.9-2.22c0-0.89-0.32-1.66-0.96-2.31 c-0.64-0.64-1.4-0.97-2.29-0.97h-1.63c-0.12,0-0.19-0.06-0.22-0.18l-0.07-0.57c-0.07-0.71-0.3-1.36-0.7-1.94s-0.91-1.03-1.53-1.36 c-0.62-0.33-1.3-0.49-2.02-0.49c-1.1,0-2.05,0.36-2.86,1.09c-0.81,0.73-1.27,1.64-1.37,2.72l-0.07,0.54c0,0.09-0.05,0.14-0.16,0.14 L9.31,13.7c-0.84,0.07-1.55,0.41-2.11,1.03c-0.57,0.62-0.85,1.35-0.85,2.2c0,0.87,0.3,1.62,0.89,2.25c0.59,0.63,1.31,0.97,2.17,1.02 c0.12,0,0.18,0.06,0.18,0.17v1.34c0,0.11-0.06,0.17-0.18,0.17c-0.66-0.03-1.28-0.18-1.88-0.45S6.42,20.8,6,20.36 c-0.43-0.44-0.77-0.95-1.02-1.55S4.6,17.59,4.6,16.93z M10.02,23.7c0-0.03,0.01-0.08,0.02-0.13s0.02-0.09,0.02-0.11l0.27-1.03 c0.07-0.22,0.2-0.4,0.4-0.51c0.2-0.12,0.41-0.14,0.64-0.07c0.23,0.07,0.4,0.2,0.52,0.4c0.12,0.2,0.14,0.41,0.07,0.64l-0.24,1.01 c-0.13,0.44-0.38,0.66-0.76,0.66c-0.03,0-0.05,0-0.09,0c-0.03,0-0.07-0.01-0.11-0.01c-0.04-0.01-0.07-0.01-0.1-0.01 c-0.21-0.06-0.37-0.18-0.48-0.34S10.02,23.86,10.02,23.7z M11.34,18.88c0-0.02,0-0.06,0.01-0.11c0.01-0.05,0.01-0.08,0.01-0.09 l0.3-1.05c0.06-0.19,0.17-0.34,0.32-0.45c0.15-0.1,0.31-0.15,0.47-0.15c0.02,0,0.05,0,0.08,0c0.03,0,0.06,0.01,0.09,0.01 c0.03,0.01,0.06,0.01,0.08,0.01c0.23,0.07,0.4,0.2,0.51,0.4c0.12,0.2,0.14,0.41,0.07,0.64l-0.24,1c-0.07,0.28-0.2,0.47-0.4,0.59 s-0.42,0.12-0.65,0.02c-0.22-0.06-0.38-0.17-0.49-0.34S11.34,19.04,11.34,18.88z M12.57,26.83c0-0.03,0.01-0.07,0.02-0.13 s0.02-0.09,0.02-0.12l0.29-0.99c0.06-0.24,0.2-0.42,0.4-0.54c0.2-0.12,0.42-0.15,0.65-0.08c0.23,0.07,0.39,0.2,0.51,0.41 s0.13,0.42,0.07,0.65l-0.25,1.04c-0.11,0.41-0.37,0.61-0.8,0.61c-0.05,0-0.13-0.01-0.24-0.04c-0.22-0.04-0.38-0.14-0.49-0.3 C12.63,27.18,12.57,27.01,12.57,26.83z M13.91,22.06c0-0.06,0.01-0.14,0.04-0.25l0.27-1.03c0.07-0.23,0.2-0.4,0.41-0.51 c0.2-0.12,0.42-0.14,0.65-0.07c0.23,0.06,0.39,0.19,0.51,0.39c0.11,0.2,0.13,0.41,0.06,0.65l-0.24,0.99 c-0.13,0.45-0.37,0.68-0.72,0.68c-0.04,0-0.15-0.02-0.31-0.06c-0.22-0.04-0.38-0.14-0.49-0.3C13.97,22.4,13.91,22.23,13.91,22.06z M16.73,23.74c0-0.07,0.01-0.15,0.03-0.24l0.28-0.99c0.07-0.24,0.2-0.42,0.41-0.54s0.41-0.15,0.63-0.09 c0.23,0.07,0.41,0.2,0.53,0.41c0.12,0.2,0.15,0.41,0.09,0.63l-0.29,1.06c-0.1,0.41-0.36,0.61-0.79,0.61c-0.09,0-0.18-0.01-0.26-0.03 c-0.2-0.04-0.35-0.14-0.46-0.3C16.8,24.08,16.74,23.91,16.73,23.74z M18.11,18.98c0-0.03,0.02-0.12,0.05-0.26l0.3-1.03 c0.04-0.21,0.13-0.37,0.29-0.47c0.16-0.1,0.32-0.15,0.49-0.14c0.04-0.01,0.13,0,0.24,0.03c0.22,0.05,0.39,0.18,0.52,0.38 c0.12,0.17,0.14,0.38,0.07,0.65l-0.24,1.03c-0.13,0.43-0.38,0.65-0.76,0.65c-0.06,0-0.17-0.02-0.34-0.06 c-0.21-0.06-0.36-0.17-0.46-0.31C18.16,19.29,18.11,19.14,18.11,18.98z",
    "wi-sleet": "M4.09,16.95c0-1.16,0.36-2.19,1.08-3.09s1.64-1.49,2.74-1.74c0.31-1.37,1.01-2.49,2.1-3.37s2.35-1.32,3.77-1.32 c0.81,0,1.55,0.13,2.2,0.4c0,0.01,0,0.01,0.01,0.02c0.84,0.32,1.58,0.84,2.21,1.55h0.03c0.68,0.73,1.13,1.6,1.37,2.62h0.31 c1.08,0,2.02,0.29,2.83,0.86v-0.01c0.53,0.36,0.98,0.8,1.34,1.33c0.36,0.53,0.6,1.11,0.73,1.74c0.04,0.21,0.06,0.38,0.08,0.52v0.06 c0,0.01,0,0.06,0.01,0.17s0.01,0.19,0.01,0.24v0.03c0,0.88-0.21,1.7-0.64,2.45c-0.42,0.75-1,1.36-1.74,1.81 c-0.73,0.45-1.54,0.69-2.42,0.72c-0.13,0-0.2-0.06-0.2-0.17v-1.34c0-0.13,0.07-0.19,0.2-0.19c0.86-0.04,1.58-0.38,2.18-1.02 c0.6-0.64,0.9-1.39,0.9-2.26c0-0.89-0.32-1.65-0.97-2.29c-0.64-0.64-1.41-0.96-2.31-0.96h-1.61c-0.12,0-0.18-0.06-0.18-0.17 l-0.08-0.59c-0.1-1-0.52-1.86-1.27-2.59c-0.01-0.01-0.01-0.02-0.02-0.03s-0.02-0.02-0.02-0.03c-0.01-0.01-0.02-0.02-0.04-0.02 c0-0.02-0.01-0.03-0.02-0.03c-0.73-0.66-1.58-1.04-2.56-1.12c-0.07-0.01-0.18-0.01-0.34-0.01c-1.1,0-2.05,0.37-2.86,1.1 s-1.27,1.64-1.37,2.72l-0.08,0.59c-0.03,0.09-0.11,0.14-0.22,0.14L8.75,13.7c-0.84,0.1-1.55,0.46-2.11,1.07s-0.84,1.34-0.84,2.18 v0.04h0.03c0.01,0.48,0.11,0.93,0.3,1.35c0.2,0.43,0.46,0.79,0.8,1.09c0.21,0.18,0.45,0.34,0.74,0.48v0.01 c0.4,0.19,0.8,0.3,1.21,0.32c0.11,0,0.17,0.06,0.17,0.18v1.34c0,0.11-0.06,0.17-0.17,0.17c-0.52-0.03-1.01-0.13-1.48-0.3v0.01 c-0.83-0.29-1.54-0.77-2.11-1.43s-0.95-1.44-1.11-2.31v-0.03c-0.01-0.01-0.01-0.02-0.01-0.04C4.11,17.57,4.09,17.27,4.09,16.95z M9.59,24.12c0-0.03,0.01-0.07,0.02-0.13c0.01-0.05,0.02-0.09,0.02-0.12l0.09-0.59c0.07-0.24,0.2-0.41,0.41-0.53 s0.43-0.14,0.68-0.08c0.23,0.07,0.39,0.21,0.51,0.41c0.11,0.21,0.13,0.42,0.07,0.63l-0.14,0.6c-0.1,0.44-0.35,0.66-0.76,0.66 c-0.03,0-0.08-0.01-0.14-0.02c-0.06-0.01-0.1-0.02-0.14-0.02c-0.21-0.06-0.36-0.17-0.46-0.33C9.64,24.45,9.59,24.29,9.59,24.12z M10.33,21.18c0-0.24,0.08-0.43,0.23-0.59s0.35-0.23,0.59-0.23s0.43,0.08,0.59,0.23s0.23,0.35,0.23,0.59c0,0.23-0.08,0.42-0.23,0.58 s-0.35,0.23-0.59,0.23c-0.23,0-0.42-0.08-0.57-0.24C10.42,21.59,10.33,21.4,10.33,21.18z M11.97,27.17c0-0.04,0.01-0.11,0.04-0.23 l0.13-0.59c0.07-0.23,0.21-0.4,0.41-0.51c0.21-0.12,0.42-0.14,0.63-0.07c0.23,0.04,0.41,0.17,0.53,0.37 c0.12,0.2,0.15,0.43,0.08,0.68l-0.13,0.59c-0.1,0.41-0.37,0.61-0.8,0.61c-0.07,0-0.16-0.01-0.24-0.03 c-0.22-0.06-0.38-0.17-0.49-0.33C12.03,27.5,11.97,27.34,11.97,27.17z M12.76,24.26c0-0.23,0.08-0.43,0.23-0.58 c0.16-0.16,0.35-0.23,0.59-0.23s0.43,0.08,0.59,0.23c0.16,0.16,0.23,0.35,0.23,0.58c0,0.24-0.08,0.43-0.23,0.59 c-0.16,0.16-0.35,0.23-0.59,0.23c-0.23,0-0.42-0.08-0.58-0.24C12.84,24.68,12.76,24.49,12.76,24.26z M13.38,21.99 c0-0.1,0.01-0.19,0.03-0.27l0.23-0.9c0.07-0.23,0.21-0.41,0.41-0.53c0.21-0.12,0.42-0.15,0.64-0.08c0.24,0.07,0.41,0.2,0.53,0.4 s0.14,0.41,0.07,0.63l-0.26,0.9c-0.08,0.28-0.22,0.46-0.41,0.56c-0.19,0.1-0.41,0.12-0.64,0.06c-0.2-0.04-0.35-0.14-0.45-0.3 C13.41,22.3,13.37,22.14,13.38,21.99z M16.14,24.13c0-0.03,0-0.08,0.01-0.13s0.01-0.09,0.01-0.11l0.09-0.59 c0.07-0.24,0.2-0.41,0.41-0.53s0.43-0.14,0.68-0.08c0.23,0.07,0.4,0.21,0.51,0.41c0.12,0.21,0.14,0.42,0.07,0.63l-0.14,0.6 c-0.1,0.44-0.35,0.66-0.76,0.66c-0.03,0-0.08-0.01-0.14-0.02c-0.06-0.01-0.11-0.02-0.14-0.02c-0.2-0.06-0.35-0.17-0.45-0.33 C16.2,24.46,16.14,24.3,16.14,24.13z M16.88,21.19c0-0.24,0.08-0.43,0.23-0.59c0.16-0.16,0.35-0.23,0.59-0.23 c0.24,0,0.43,0.08,0.59,0.23c0.16,0.16,0.23,0.35,0.23,0.59c0,0.23-0.08,0.43-0.23,0.58C18.14,21.92,17.95,22,17.71,22 c-0.24,0-0.43-0.08-0.58-0.24C16.97,21.61,16.88,21.42,16.88,21.19z",
    "wi-snow": "M4.64,16.95c0-1.16,0.35-2.18,1.06-3.08s1.62-1.48,2.74-1.76c0.31-1.36,1.01-2.48,2.1-3.36s2.34-1.31,3.75-1.31 c1.38,0,2.6,0.43,3.68,1.28c1.08,0.85,1.78,1.95,2.1,3.29h0.32c0.89,0,1.72,0.22,2.48,0.66c0.76,0.44,1.37,1.04,1.81,1.8 c0.44,0.76,0.67,1.59,0.67,2.48c0,1.32-0.46,2.47-1.39,3.42c-0.92,0.96-2.05,1.46-3.38,1.5c-0.13,0-0.2-0.06-0.2-0.17v-1.33 c0-0.12,0.07-0.18,0.2-0.18c0.85-0.04,1.58-0.38,2.18-1.02s0.9-1.38,0.9-2.23c0-0.89-0.32-1.65-0.97-2.3s-1.42-0.97-2.32-0.97h-1.61 c-0.12,0-0.18-0.06-0.18-0.17l-0.08-0.58c-0.11-1.08-0.58-1.99-1.39-2.72c-0.82-0.73-1.76-1.1-2.85-1.1c-1.1,0-2.05,0.37-2.86,1.11 c-0.81,0.74-1.27,1.65-1.37,2.75l-0.06,0.5c0,0.12-0.07,0.19-0.2,0.19l-0.53,0.07c-0.83,0.07-1.53,0.41-2.1,1.04 s-0.85,1.35-0.85,2.19c0,0.85,0.3,1.59,0.9,2.23s1.33,0.97,2.18,1.02c0.11,0,0.17,0.06,0.17,0.18v1.33c0,0.11-0.06,0.17-0.17,0.17 c-1.34-0.04-2.47-0.54-3.4-1.5C5.1,19.42,4.64,18.27,4.64,16.95z M11,21.02c0-0.22,0.08-0.42,0.24-0.58 c0.16-0.16,0.35-0.24,0.59-0.24c0.23,0,0.43,0.08,0.59,0.24c0.16,0.16,0.24,0.36,0.24,0.58c0,0.24-0.08,0.44-0.24,0.6 c-0.16,0.17-0.35,0.25-0.59,0.25c-0.23,0-0.43-0.08-0.59-0.25C11.08,21.46,11,21.26,11,21.02z M11,24.65c0-0.24,0.08-0.44,0.24-0.6 c0.16-0.15,0.35-0.23,0.58-0.23c0.23,0,0.43,0.08,0.59,0.23c0.16,0.16,0.24,0.35,0.24,0.59c0,0.24-0.08,0.43-0.24,0.59 c-0.16,0.16-0.35,0.23-0.59,0.23c-0.23,0-0.43-0.08-0.59-0.23C11.08,25.08,11,24.88,11,24.65z M14.19,22.95 c0-0.23,0.08-0.44,0.25-0.62c0.16-0.16,0.35-0.24,0.57-0.24c0.23,0,0.43,0.09,0.6,0.26c0.17,0.17,0.26,0.37,0.26,0.6 c0,0.23-0.08,0.43-0.25,0.6c-0.17,0.17-0.37,0.25-0.61,0.25c-0.23,0-0.42-0.08-0.58-0.25S14.19,23.18,14.19,22.95z M14.19,19.33 c0-0.23,0.08-0.43,0.25-0.6c0.18-0.16,0.37-0.24,0.57-0.24c0.24,0,0.44,0.08,0.61,0.25c0.17,0.17,0.25,0.36,0.25,0.6 c0,0.23-0.08,0.43-0.25,0.59c-0.17,0.16-0.37,0.24-0.61,0.24c-0.23,0-0.42-0.08-0.58-0.24C14.27,19.76,14.19,19.56,14.19,19.33z M14.19,26.61c0-0.23,0.08-0.43,0.25-0.61c0.16-0.16,0.35-0.24,0.57-0.24c0.24,0,0.44,0.08,0.61,0.25c0.17,0.17,0.25,0.37,0.25,0.6 s-0.08,0.43-0.25,0.59c-0.17,0.16-0.37,0.24-0.61,0.24c-0.23,0-0.42-0.08-0.58-0.24C14.27,27.03,14.19,26.84,14.19,26.61z M17.41,21.02c0-0.22,0.08-0.41,0.25-0.58c0.17-0.17,0.37-0.25,0.6-0.25c0.23,0,0.43,0.08,0.59,0.24c0.16,0.16,0.24,0.36,0.24,0.58 c0,0.24-0.08,0.44-0.24,0.6c-0.16,0.17-0.35,0.25-0.59,0.25c-0.24,0-0.44-0.08-0.6-0.25C17.5,21.45,17.41,21.25,17.41,21.02z M17.41,24.65c0-0.22,0.08-0.42,0.25-0.6c0.16-0.15,0.36-0.23,0.6-0.23c0.24,0,0.43,0.08,0.59,0.23s0.23,0.35,0.23,0.59 c0,0.24-0.08,0.43-0.23,0.59c-0.16,0.16-0.35,0.23-0.59,0.23c-0.24,0-0.44-0.08-0.6-0.24C17.5,25.07,17.41,24.88,17.41,24.65z",
    "wi-sprinkle": "M4.64,16.91c0-1.15,0.36-2.17,1.08-3.07c0.72-0.9,1.63-1.47,2.73-1.73c0.31-1.36,1.01-2.48,2.1-3.35s2.35-1.31,3.76-1.31 c1.38,0,2.6,0.43,3.68,1.27c1.07,0.85,1.78,1.94,2.11,3.28h0.31c0.89,0,1.72,0.22,2.48,0.65s1.37,1.03,1.81,1.78 c0.44,0.75,0.67,1.58,0.67,2.47c0,1.34-0.46,2.49-1.38,3.45s-2.05,1.47-3.38,1.51c-0.13,0-0.2-0.06-0.2-0.17v-1.33 c0-0.12,0.07-0.18,0.2-0.18c0.86-0.04,1.58-0.38,2.18-1.02s0.9-1.39,0.9-2.26s-0.32-1.62-0.98-2.26c-0.65-0.64-1.42-0.96-2.31-0.96 h-1.6c-0.12,0-0.19-0.06-0.19-0.17l-0.07-0.58c-0.11-1.07-0.57-1.98-1.38-2.71c-0.82-0.73-1.77-1.1-2.85-1.1 c-1.09,0-2.05,0.36-2.86,1.09c-0.81,0.73-1.27,1.63-1.38,2.71l-0.06,0.54c0,0.12-0.07,0.18-0.2,0.18l-0.53,0.03 c-0.82,0.04-1.51,0.37-2.09,1s-0.86,1.37-0.86,2.22c0,0.87,0.3,1.62,0.9,2.26s1.33,0.98,2.18,1.02c0.11,0,0.17,0.06,0.17,0.18v1.33 c0,0.11-0.06,0.17-0.17,0.17c-1.34-0.06-2.47-0.57-3.4-1.53S4.64,18.24,4.64,16.91z M10.57,17.79c0-0.24,0.12-0.57,0.37-0.99 c0.24-0.42,0.47-0.75,0.68-1.01c0.21-0.24,0.34-0.38,0.38-0.42l0.36,0.4c0.26,0.28,0.5,0.61,0.72,1.02c0.22,0.4,0.33,0.74,0.33,1 c0,0.39-0.13,0.72-0.4,0.98c-0.27,0.26-0.6,0.39-1,0.39c-0.39,0-0.73-0.13-1.01-0.4C10.71,18.5,10.57,18.17,10.57,17.79z M13.55,21.78c0-0.28,0.08-0.59,0.24-0.96s0.35-0.7,0.59-1.02c0.18-0.26,0.4-0.54,0.67-0.84c0.26-0.3,0.46-0.52,0.6-0.65 c0.07-0.06,0.15-0.14,0.24-0.23l0.24,0.23c0.38,0.33,0.8,0.82,1.27,1.46c0.24,0.33,0.43,0.68,0.59,1.04s0.23,0.68,0.23,0.97 c0,0.64-0.23,1.19-0.68,1.65s-1.01,0.68-1.66,0.68c-0.64,0-1.19-0.23-1.65-0.67C13.77,22.98,13.55,22.43,13.55,21.78z M15.02,15.12 c0-0.42,0.32-0.95,0.97-1.6l0.24,0.25c0.18,0.21,0.33,0.45,0.48,0.71c0.14,0.26,0.22,0.47,0.22,0.64c0,0.26-0.09,0.48-0.28,0.66 c-0.18,0.18-0.4,0.28-0.66,0.28c-0.27,0-0.5-0.09-0.69-0.28C15.11,15.6,15.02,15.38,15.02,15.12z",
    "wi-storm-showers": "M4.63,16.91c0,1.11,0.33,2.1,0.99,2.97s1.51,1.47,2.56,1.79l-0.65,1.68c-0.03,0.14,0.02,0.22,0.14,0.22h2.13l-1.04,3.78 h0.28l3.97-5.22c0.04-0.04,0.04-0.09,0.02-0.14s-0.07-0.07-0.14-0.07h-2.18l2.48-4.64c0.06-0.14,0.02-0.21-0.14-0.21h-2.94 c-0.09,0-0.17,0.05-0.22,0.14L8.8,20.08c-0.71-0.18-1.3-0.57-1.77-1.16c-0.47-0.59-0.7-1.26-0.7-2.01c0-0.83,0.28-1.55,0.84-2.16 s1.26-0.96,2.1-1.06l0.52-0.07c0.13,0,0.2-0.06,0.2-0.17l0.07-0.52c0.1-1.08,0.55-1.99,1.36-2.72c0.81-0.73,1.76-1.1,2.85-1.1 c1.09,0,2.04,0.37,2.85,1.1c0.82,0.73,1.28,1.64,1.4,2.72l0.06,0.58c0,0.12,0.06,0.18,0.19,0.18h1.61c0.91,0,1.68,0.32,2.32,0.95 c0.64,0.63,0.96,1.39,0.96,2.28c0,0.85-0.3,1.59-0.89,2.21c-0.59,0.62-1.32,0.96-2.19,1.03c-0.13,0-0.2,0.06-0.2,0.19v1.37 c0,0.11,0.07,0.17,0.2,0.17c0.65-0.02,1.27-0.17,1.86-0.44c0.59-0.27,1.1-0.63,1.52-1.07c0.42-0.44,0.76-0.96,1.01-1.57 c0.25-0.6,0.38-1.23,0.38-1.88c0-0.9-0.22-1.73-0.67-2.49c-0.45-0.76-1.05-1.36-1.81-1.8c-0.76-0.44-1.59-0.66-2.48-0.66h-0.32 c-0.33-1.33-1.03-2.42-2.11-3.26c-1.08-0.84-2.3-1.27-3.68-1.27c-1.41,0-2.67,0.44-3.76,1.31s-1.79,1.99-2.1,3.36 c-1.1,0.26-2.01,0.83-2.73,1.73S4.63,15.76,4.63,16.91z M12.79,26.77c0,0.16,0.05,0.31,0.15,0.46c0.1,0.15,0.26,0.26,0.46,0.32 c0.14,0.03,0.22,0.05,0.25,0.05c0.09,0,0.21-0.03,0.38-0.1c0.21-0.09,0.35-0.27,0.42-0.52l0.28-1.05c0.06-0.22,0.04-0.43-0.08-0.63 s-0.29-0.33-0.53-0.4c-0.22-0.06-0.43-0.04-0.63,0.08c-0.2,0.12-0.34,0.29-0.41,0.53l-0.27,1C12.8,26.68,12.79,26.77,12.79,26.77z M14.13,22c0,0.14,0.05,0.29,0.15,0.44c0.1,0.15,0.25,0.26,0.45,0.33c0.22,0.07,0.44,0.06,0.64-0.05s0.33-0.28,0.4-0.52l0.3-1.04 c0.06-0.22,0.03-0.43-0.08-0.63c-0.12-0.2-0.3-0.34-0.53-0.41c-0.23-0.06-0.44-0.04-0.65,0.08s-0.34,0.29-0.41,0.52l-0.24,1.01 C14.14,21.9,14.13,21.99,14.13,22z M16.95,23.65c0,0.17,0.05,0.34,0.16,0.51c0.11,0.17,0.27,0.28,0.47,0.35 c0.02,0,0.06,0.01,0.12,0.02c0.05,0.01,0.09,0.02,0.12,0.02c0.13,0,0.26-0.02,0.38-0.08c0.19-0.07,0.33-0.26,0.41-0.57l0.25-1.01 c0.07-0.23,0.05-0.45-0.06-0.66c-0.11-0.21-0.28-0.35-0.5-0.42c-0.25-0.06-0.48-0.03-0.68,0.08c-0.2,0.12-0.33,0.3-0.37,0.53 l-0.27,1.03C16.96,23.51,16.95,23.58,16.95,23.65z M18.31,18.86c-0.01,0.16,0.04,0.31,0.15,0.47c0.11,0.16,0.27,0.28,0.49,0.38 c0.08,0.04,0.16,0.06,0.26,0.06c0.11,0,0.22-0.03,0.34-0.08c0.21-0.1,0.35-0.29,0.44-0.57l0.29-1.03c0.02-0.13,0.03-0.2,0.03-0.22 c0-0.17-0.05-0.33-0.16-0.49s-0.27-0.27-0.49-0.33c-0.02,0-0.06-0.01-0.11-0.02C19.49,17,19.45,17,19.42,17 c-0.17,0-0.33,0.05-0.49,0.15c-0.16,0.1-0.27,0.26-0.33,0.48l-0.27,1.01C18.32,18.72,18.31,18.79,18.31,18.86z",
    "wi-sunrise": "M2.75,15.36c0-0.25,0.1-0.48,0.3-0.69c0.22-0.19,0.46-0.29,0.7-0.29h2.33c0.27,0,0.49,0.1,0.67,0.29 c0.18,0.19,0.27,0.43,0.27,0.69c0,0.29-0.09,0.53-0.27,0.72c-0.18,0.19-0.41,0.29-0.67,0.29H3.75c-0.27,0-0.5-0.1-0.7-0.3 C2.85,15.86,2.75,15.62,2.75,15.36z M6.08,7.38c0-0.27,0.09-0.5,0.26-0.68C6.57,6.5,6.8,6.4,7.05,6.4c0.26,0,0.49,0.1,0.68,0.29 l1.64,1.65c0.19,0.22,0.28,0.45,0.28,0.69c0,0.28-0.09,0.52-0.27,0.7s-0.4,0.28-0.66,0.28c-0.24,0-0.48-0.1-0.7-0.29L6.34,8.11 C6.17,7.9,6.08,7.65,6.08,7.38z M8.08,20.88c0-0.28,0.1-0.51,0.29-0.68c0.18-0.17,0.4-0.26,0.68-0.26h2.63l3.11-2.92 c0.1-0.08,0.21-0.08,0.34,0l3.16,2.92h2.77c0.27,0,0.5,0.09,0.69,0.28c0.19,0.18,0.29,0.41,0.29,0.67c0,0.27-0.1,0.5-0.29,0.69 c-0.19,0.19-0.42,0.29-0.69,0.29h-3.38c-0.1,0-0.2-0.02-0.29-0.07l-2.41-2.27l-2.39,2.27c-0.08,0.05-0.17,0.07-0.28,0.07H9.05 c-0.27,0-0.5-0.1-0.69-0.29C8.17,21.38,8.08,21.15,8.08,20.88z M9,15.36c0,0.97,0.21,1.85,0.62,2.64c0.02,0.12,0.11,0.18,0.25,0.18 h1.88c0.07,0,0.12-0.03,0.15-0.08c0.03-0.06,0.02-0.12-0.02-0.19c-0.64-0.77-0.96-1.62-0.96-2.55c0-1.12,0.4-2.08,1.2-2.87 c0.8-0.79,1.76-1.18,2.89-1.18c1.12,0,2.07,0.39,2.86,1.18c0.79,0.79,1.19,1.74,1.19,2.87c0,0.94-0.32,1.79-0.95,2.55 c-0.04,0.07-0.05,0.13-0.03,0.19s0.07,0.08,0.15,0.08h1.9c0.13,0,0.21-0.06,0.23-0.18C20.8,17.23,21,16.35,21,15.36 c0-0.81-0.16-1.59-0.48-2.32c-0.32-0.74-0.75-1.37-1.28-1.91c-0.53-0.53-1.17-0.96-1.91-1.28c-0.74-0.32-1.51-0.47-2.32-0.47 c-0.81,0-1.59,0.16-2.33,0.47c-0.74,0.32-1.38,0.74-1.92,1.28c-0.54,0.53-0.97,1.17-1.29,1.91C9.16,13.77,9,14.54,9,15.36z M14.03,6.4v-2.3c0-0.29,0.09-0.52,0.28-0.71s0.43-0.28,0.71-0.28c0.28,0,0.51,0.09,0.7,0.28S16,3.83,16,4.11v2.3 c0,0.29-0.09,0.52-0.28,0.71c-0.18,0.18-0.42,0.28-0.7,0.28c-0.29,0-0.52-0.09-0.71-0.28C14.12,6.93,14.03,6.69,14.03,6.4z M20.38,9.04c0-0.25,0.09-0.48,0.27-0.69l1.62-1.65c0.19-0.19,0.43-0.29,0.7-0.29c0.27,0,0.51,0.1,0.69,0.29 c0.19,0.19,0.28,0.42,0.28,0.69c0,0.29-0.09,0.53-0.26,0.73L22,9.73c-0.21,0.19-0.45,0.29-0.7,0.29c-0.27,0-0.49-0.09-0.66-0.28 S20.38,9.32,20.38,9.04z M22.99,15.36c0-0.27,0.09-0.5,0.27-0.69c0.18-0.19,0.4-0.29,0.66-0.29h2.35c0.27,0,0.5,0.1,0.69,0.29 c0.19,0.19,0.29,0.43,0.29,0.69c0,0.28-0.1,0.51-0.29,0.71s-0.42,0.3-0.69,0.3h-2.35c-0.27,0-0.49-0.1-0.67-0.29 C23.08,15.88,22.99,15.64,22.99,15.36z",
    "wi-sunset": "M2.88,15.47c0-0.28,0.1-0.5,0.3-0.68c0.17-0.18,0.4-0.26,0.68-0.26h2.31c0.27,0,0.49,0.09,0.67,0.27 c0.17,0.18,0.26,0.4,0.26,0.67c0,0.28-0.09,0.52-0.27,0.71c-0.18,0.19-0.4,0.29-0.66,0.29H3.87c-0.27,0-0.5-0.1-0.69-0.3 C2.98,15.97,2.88,15.74,2.88,15.47z M6.17,7.61c0-0.28,0.08-0.51,0.25-0.68c0.2-0.2,0.43-0.3,0.7-0.3c0.29,0,0.51,0.1,0.68,0.3 l1.62,1.63c0.46,0.44,0.46,0.89,0,1.35c-0.19,0.19-0.4,0.28-0.65,0.28c-0.22,0-0.44-0.09-0.68-0.28L6.43,8.29 C6.26,8.11,6.17,7.88,6.17,7.61z M8.14,20.89c0-0.26,0.1-0.49,0.3-0.69c0.18-0.18,0.41-0.27,0.68-0.27h3.22 c0.11,0,0.2,0.02,0.28,0.08l2.35,2.22L17.36,20c0.07-0.05,0.17-0.08,0.29-0.08h3.3c0.27,0,0.5,0.09,0.69,0.28 c0.19,0.19,0.29,0.42,0.29,0.68c0,0.27-0.1,0.5-0.29,0.69c-0.19,0.19-0.42,0.29-0.69,0.29h-2.68l-3.14,2.84 c-0.12,0.09-0.23,0.09-0.33,0l-3.08-2.84h-2.6c-0.27,0-0.5-0.1-0.69-0.29C8.24,21.39,8.14,21.16,8.14,20.89z M9.08,15.47 c0,0.99,0.19,1.87,0.58,2.62c0.06,0.11,0.15,0.16,0.27,0.16h1.87c0.08,0,0.13-0.02,0.15-0.07c0.02-0.05-0.01-0.11-0.07-0.18 c-0.59-0.74-0.89-1.59-0.89-2.53c0-1.1,0.39-2.04,1.18-2.81c0.79-0.77,1.74-1.16,2.85-1.16c1.1,0,2.04,0.39,2.83,1.16 c0.78,0.78,1.18,1.71,1.18,2.8c0,0.94-0.3,1.79-0.89,2.53c-0.07,0.07-0.09,0.13-0.07,0.18c0.02,0.05,0.07,0.07,0.15,0.07h1.88 c0.13,0,0.21-0.05,0.24-0.16c0.41-0.78,0.62-1.66,0.62-2.62c0-0.79-0.16-1.56-0.47-2.29s-0.74-1.37-1.27-1.9s-1.16-0.95-1.89-1.27 c-0.73-0.32-1.5-0.47-2.3-0.47c-0.8,0-1.57,0.16-2.3,0.47c-0.73,0.32-1.36,0.74-1.89,1.27s-0.95,1.16-1.27,1.9 S9.08,14.68,9.08,15.47z M14.04,6.66V4.33c0-0.27,0.1-0.5,0.29-0.69s0.42-0.29,0.69-0.29c0.27,0,0.5,0.1,0.69,0.29 s0.29,0.42,0.29,0.69v2.32c0,0.27-0.1,0.5-0.29,0.69c-0.19,0.19-0.42,0.29-0.69,0.29c-0.27,0-0.5-0.1-0.69-0.29 C14.13,7.15,14.04,6.93,14.04,6.66z M20.31,9.24c0-0.28,0.09-0.51,0.26-0.67l1.63-1.63c0.16-0.2,0.39-0.3,0.68-0.3 c0.27,0,0.5,0.1,0.68,0.29c0.18,0.19,0.27,0.42,0.27,0.69c0,0.28-0.08,0.51-0.25,0.68l-1.66,1.63c-0.23,0.19-0.46,0.28-0.69,0.28 c-0.26,0-0.48-0.09-0.66-0.28C20.4,9.74,20.31,9.51,20.31,9.24z M22.9,15.47c0-0.27,0.09-0.49,0.26-0.67 c0.17-0.18,0.4-0.27,0.67-0.27h2.32c0.27,0,0.5,0.09,0.69,0.27c0.19,0.18,0.29,0.4,0.29,0.67c0,0.27-0.1,0.5-0.29,0.7 c-0.19,0.2-0.42,0.3-0.69,0.3h-2.32c-0.26,0-0.48-0.1-0.66-0.29C22.99,15.99,22.9,15.75,22.9,15.47z",
    "wi-thunderstorm": "M4.63,16.91c0,1.11,0.33,2.1,0.99,2.97s1.52,1.47,2.58,1.79l-0.66,1.68c-0.03,0.14,0.02,0.22,0.14,0.22h2.13l-0.98,4.3h0.28 l3.92-5.75c0.04-0.04,0.04-0.09,0.01-0.14c-0.03-0.05-0.08-0.07-0.15-0.07h-2.18l2.48-4.64c0.07-0.14,0.02-0.22-0.14-0.22h-2.94 c-0.09,0-0.17,0.05-0.23,0.15l-1.07,2.87c-0.71-0.18-1.3-0.57-1.77-1.16c-0.47-0.59-0.7-1.26-0.7-2.01c0-0.83,0.28-1.55,0.85-2.17 c0.57-0.61,1.27-0.97,2.1-1.07l0.53-0.07c0.13,0,0.2-0.06,0.2-0.18l0.07-0.51c0.11-1.08,0.56-1.99,1.37-2.72 c0.81-0.73,1.76-1.1,2.85-1.1c1.09,0,2.04,0.37,2.85,1.1c0.82,0.73,1.28,1.64,1.4,2.72l0.07,0.58c0,0.11,0.06,0.17,0.18,0.17h1.6 c0.91,0,1.68,0.32,2.32,0.95c0.64,0.63,0.97,1.4,0.97,2.28c0,0.85-0.3,1.59-0.89,2.21c-0.59,0.62-1.33,0.97-2.2,1.04 c-0.13,0-0.2,0.06-0.2,0.18v1.37c0,0.11,0.07,0.17,0.2,0.17c1.33-0.04,2.46-0.55,3.39-1.51s1.39-2.11,1.39-3.45 c0-0.9-0.22-1.73-0.67-2.49c-0.44-0.76-1.05-1.36-1.81-1.8c-0.77-0.44-1.6-0.66-2.5-0.66H20.1c-0.33-1.33-1.04-2.42-2.11-3.26 s-2.3-1.27-3.68-1.27c-1.41,0-2.67,0.44-3.76,1.31s-1.79,1.99-2.1,3.36c-1.11,0.26-2.02,0.83-2.74,1.73S4.63,15.76,4.63,16.91z M12.77,26.62c0,0.39,0.19,0.65,0.58,0.77c0.01,0,0.05,0,0.11,0.01c0.06,0.01,0.11,0.01,0.14,0.01c0.17,0,0.33-0.05,0.49-0.15 c0.16-0.1,0.27-0.26,0.32-0.48l2.25-8.69c0.06-0.24,0.04-0.45-0.07-0.65c-0.11-0.19-0.27-0.32-0.5-0.39 c-0.17-0.02-0.26-0.03-0.26-0.03c-0.16,0-0.32,0.05-0.47,0.15c-0.15,0.1-0.26,0.25-0.31,0.45l-2.26,8.72 C12.78,26.44,12.77,26.53,12.77,26.62z M16.93,23.56c0,0.13,0.03,0.26,0.1,0.38c0.14,0.22,0.31,0.37,0.51,0.44 c0.11,0.03,0.21,0.05,0.3,0.05s0.2-0.02,0.32-0.08c0.21-0.09,0.35-0.28,0.42-0.57l1.44-5.67c0.03-0.14,0.05-0.23,0.05-0.27 c0-0.15-0.05-0.3-0.16-0.45s-0.26-0.26-0.46-0.32c-0.17-0.02-0.26-0.03-0.26-0.03c-0.17,0-0.33,0.05-0.47,0.15 c-0.14,0.1-0.24,0.25-0.3,0.45l-1.46,5.7c0,0.02,0,0.05-0.01,0.11C16.93,23.5,16.93,23.53,16.93,23.56z",
    "wi-time-3": "M3.74,14.47c0-2.04,0.51-3.93,1.52-5.66s2.38-3.1,4.11-4.11s3.61-1.51,5.64-1.51c1.52,0,2.98,0.3,4.37,0.89 s2.58,1.4,3.59,2.4s1.81,2.2,2.4,3.6s0.89,2.85,0.89,4.39c0,1.52-0.3,2.98-0.89,4.37s-1.4,2.59-2.4,3.59s-2.2,1.8-3.59,2.39 s-2.84,0.89-4.37,0.89c-1.53,0-3-0.3-4.39-0.89s-2.59-1.4-3.6-2.4s-1.8-2.2-2.4-3.58S3.74,16,3.74,14.47z M6.22,14.47 c0,2.37,0.86,4.43,2.59,6.18c1.73,1.73,3.79,2.59,6.2,2.59c1.58,0,3.05-0.39,4.39-1.18s2.42-1.85,3.21-3.2s1.19-2.81,1.19-4.39 s-0.4-3.05-1.19-4.4s-1.86-2.42-3.21-3.21s-2.81-1.18-4.39-1.18s-3.05,0.39-4.39,1.18S8.2,8.72,7.4,10.07S6.22,12.89,6.22,14.47z M14.14,14.47V7.81c0-0.23,0.08-0.43,0.24-0.59s0.36-0.24,0.59-0.24s0.43,0.08,0.59,0.24s0.24,0.36,0.24,0.59v5.82h3.78 c0.23,0,0.43,0.08,0.59,0.24s0.24,0.36,0.24,0.59c0,0.22-0.08,0.42-0.24,0.59c-0.16,0.17-0.36,0.25-0.59,0.25h-4.44 c-0.03,0.01-0.09,0.01-0.18,0.01c-0.23,0-0.43-0.08-0.59-0.24S14.14,14.71,14.14,14.47z",
    "wi-time-9": "M3.74,14.47c0-2.04,0.51-3.93,1.52-5.66s2.38-3.1,4.11-4.11s3.61-1.51,5.64-1.51c1.52,0,2.98,0.3,4.37,0.89 s2.58,1.4,3.59,2.4s1.81,2.2,2.4,3.6s0.89,2.85,0.89,4.39c0,1.52-0.3,2.98-0.89,4.37s-1.4,2.59-2.4,3.59s-2.2,1.8-3.59,2.39 s-2.84,0.89-4.37,0.89c-1.53,0-3-0.3-4.39-0.89s-2.59-1.4-3.6-2.4s-1.8-2.2-2.4-3.58S3.74,16,3.74,14.47z M6.22,14.47 c0,2.37,0.86,4.43,2.59,6.18c1.73,1.73,3.79,2.59,6.2,2.59c1.58,0,3.05-0.39,4.39-1.18s2.42-1.85,3.21-3.2s1.19-2.81,1.19-4.39 s-0.4-3.05-1.19-4.4s-1.86-2.42-3.21-3.21s-2.81-1.18-4.39-1.18s-3.05,0.39-4.39,1.18S8.2,8.72,7.4,10.07S6.22,12.89,6.22,14.47z M9.51,14.46c0-0.23,0.08-0.43,0.24-0.59s0.36-0.24,0.59-0.24h3.79V7.81c0-0.23,0.08-0.43,0.24-0.59s0.36-0.24,0.59-0.24 s0.43,0.08,0.59,0.24s0.24,0.36,0.24,0.59v6.67c0,0.23-0.08,0.43-0.24,0.59s-0.36,0.24-0.59,0.24c-0.1,0-0.16,0-0.19-0.01h-4.44 c-0.23,0-0.43-0.08-0.59-0.25C9.59,14.88,9.51,14.68,9.51,14.46z",
    "wi-umbrella": "M4.64,14.65c0.01-0.34,0.18-0.86,0.5-1.58c0.32-0.72,0.76-1.48,1.33-2.3c1.86-2.61,4.49-3.98,7.88-4.13V6.21 c0-0.21,0.07-0.37,0.21-0.5c0.14-0.13,0.3-0.19,0.48-0.19c0.19,0,0.35,0.06,0.5,0.19c0.14,0.13,0.22,0.3,0.22,0.5v0.44 c0.98,0.04,1.9,0.19,2.75,0.45c0.85,0.26,1.59,0.59,2.22,1c0.63,0.41,1.17,0.83,1.61,1.27c0.45,0.43,0.85,0.9,1.2,1.41 c0.41,0.59,0.77,1.23,1.06,1.9c0.29,0.67,0.5,1.21,0.61,1.61c0.11,0.4,0.17,0.6,0.18,0.61v0.19c0,0.18-0.07,0.32-0.21,0.44 s-0.3,0.17-0.49,0.17c-0.31,0-0.51-0.09-0.6-0.26c-0.78-0.88-1.63-1.31-2.55-1.31c-0.34,0.02-0.69,0.1-1.03,0.23 c-0.34,0.13-0.62,0.27-0.82,0.42c-0.21,0.14-0.4,0.29-0.58,0.44c-0.18,0.15-0.27,0.22-0.28,0.23c-0.19,0.17-0.37,0.26-0.53,0.26 c-0.23,0-0.4-0.06-0.52-0.18c-0.73-0.73-1.39-1.17-2.01-1.32v7.57l0,0.17l-0.01,0.21l-0.04,0.23l-0.06,0.25l-0.09,0.26l-0.13,0.27 l-0.17,0.26l-0.21,0.25c-0.51,0.59-1.23,0.88-2.18,0.88c-1.01,0-1.77-0.29-2.28-0.88c-0.12-0.12-0.22-0.25-0.31-0.38 c-0.09-0.14-0.16-0.27-0.21-0.41c-0.05-0.13-0.09-0.26-0.12-0.38s-0.05-0.24-0.06-0.36C9.88,22,9.87,21.9,9.87,21.82s0-0.16,0-0.23 s0.01-0.12,0.01-0.13c0-0.18,0.08-0.34,0.23-0.47c0.16-0.13,0.34-0.18,0.55-0.14c0.18,0,0.32,0.08,0.44,0.23s0.18,0.34,0.18,0.56 c-0.06,0.41,0.02,0.76,0.25,1.05c0.21,0.29,0.65,0.44,1.32,0.44c0.52,0,0.9-0.12,1.13-0.36c0.13-0.13,0.23-0.29,0.29-0.48 c0.06-0.19,0.09-0.34,0.08-0.47l-0.01-0.19v-7.36c-0.73,0.18-1.38,0.56-1.93,1.14c-0.04,0.08-0.12,0.16-0.23,0.23 s-0.21,0.11-0.3,0.11c-0.18,0-0.38-0.11-0.6-0.34c-0.8-0.89-1.65-1.33-2.55-1.31c-0.4,0.01-0.78,0.07-1.12,0.2 C7.26,14.43,7,14.56,6.82,14.69c-0.17,0.13-0.36,0.28-0.54,0.45s-0.29,0.27-0.32,0.29c-0.21,0.14-0.38,0.22-0.51,0.22 s-0.3-0.06-0.48-0.17c-0.16-0.1-0.26-0.21-0.3-0.32C4.64,15.04,4.63,14.87,4.64,14.65z M6.73,13.23c0.68-0.36,1.32-0.53,1.92-0.53 h0.08c1.15,0,2.2,0.44,3.15,1.33c0.38-0.33,0.84-0.62,1.39-0.88c0.54-0.26,1.13-0.41,1.77-0.45h0.08c1.15,0,2.2,0.44,3.15,1.33 c0.38-0.33,0.84-0.62,1.39-0.88c0.54-0.26,1.13-0.41,1.77-0.45h0.09c0.56,0,1.15,0.15,1.75,0.44c-0.44-0.86-0.74-1.41-0.88-1.66 c-1.79-2.34-4.27-3.51-7.43-3.51c-1.58,0-2.99,0.3-4.24,0.9c-1.24,0.6-2.26,1.47-3.05,2.61C7.44,11.82,7.12,12.41,6.73,13.23z",
}


def icon_svg(name):
    """Inline SVG for an icon name; sized by the surrounding font-size."""
    d = ICONS.get(name) or ICONS["wi-na"]
    return f'<svg viewBox="0 0 30 30" aria-hidden="true"><path d="{d}"/></svg>'
