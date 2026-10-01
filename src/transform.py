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
import re
from datetime import datetime, timedelta

# ---- Tunables -------------------------------------------------------------

# Cumulative ET0 (mm) a load needs to be dry. Calibrated against real washing
# (see "Calibration log" in the README): the first guess of 1.3 for a normal
# load ran about an hour slow, so all three are scaled by 0.85.
DRY_NEED = {"light": 0.75, "normal": 1.1, "heavy": 1.6}
VERDICT_LOAD = "normal"

# Rain is graded, not a yes/no cliff, and judged on its *chance* alone: that
# comes from many model runs, so it already allows for their disagreement,
# while the forecast amount is one run's guess. (The lawn is different: how
# wet it gets depends on the amount, so mowing uses both.)
# - an hour is "wet" (the washing must be in) from this chance;
WET_PROB = 60       # %
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

# Say the line is sheltered when less than this share of the wind reaches it.
SHELTERED = 0.7

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
    "fence_section": {"name": "fence", "profile": [(1.8, 0)],          "half_width": 2.5,  "shade": 1.0, "wind": 0.8},
    "hedge":    {"name": "hedge",    "profile": [(3.0, 0)],            "half_width": None, "shade": 0.9, "wind": 0.6},
    "shed":     {"name": "shed",     "profile": [(2.5, 0)],            "half_width": 1.5,  "shade": 1.0, "wind": 1.0},
    "bungalow": {"name": "bungalow", "profile": [(2.7, 0), (5.5, 4)],  "half_width": None,  "shade": 1.0, "wind": 1.0},
    "house2":   {"name": "house",    "profile": [(5.3, 0), (8.5, 4)],  "half_width": None,  "shade": 1.0, "wind": 1.0},
    "house3":   {"name": "house",    "profile": [(7.8, 0), (11.0, 4)], "half_width": None,  "shade": 1.0, "wind": 1.0},
    "tree":     {"name": "tree",     "profile": [(10.0, 0)],           "half_width": 3.0,  "shade": 0.7, "wind": 0.5},
}
# The washing is a band hanging from the line: the line's height is a
# setting (default 2.0 m, about 6.5 ft) and items hang about 0.8 m below it.
DEFAULT_LINE_HEIGHT = 2.0  # m
HANG = 0.8                 # m
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
    for words, key in ((("section", "short"), "fence_section"),
                       (("fence", "wall"), "fence"), (("hedge",), "hedge"),
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
    # Line height: a value in metres, or a label like "6.5 ft (2.0 m)".
    metres = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*m\b", pick("line_height"))]
    try:
        top = metres[-1] if metres else float(pick("line_height"))
    except ValueError:
        top = DEFAULT_LINE_HEIGHT
    top = min(max(top, 1.2), 3.0)
    return {"obstructions": obstructions, "exposure": exposure,
            "top": top, "bottom": max(top - HANG, 0.3)}


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


def band_below(height, garden):
    """Share of the washing band that's below `height`."""
    top, bottom = garden["top"], garden["bottom"]
    return min(max((height - bottom) / (top - bottom), 0.0), 1.0)


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
        share = band_below(shadow_height(ob, elev, az), garden) * ob["shade"]
        if share > blocked:
            blocked, name = share, ob["name"]
    return blocked, name


def shade_fraction(start, end, geo, garden, steps=4):
    step = (end - start) / steps
    samples = [start + step * (k + 0.5) for k in range(steps)]
    return sum(shading(*sun_position(t, *geo), garden)[0] for t in samples) / steps


def wind_factor(from_deg, garden):
    """Share of the forecast wind that reaches the line."""
    return wind_shelter(from_deg, garden)[0]


def wind_shelter(from_deg, garden):
    """(share of the wind reaching the line, name of the biggest blocker)."""
    factor = garden["exposure"]
    biggest, name = 0.0, None
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
        cut = reduction * c * ob["wind"] * band_below(top, garden)
        factor *= 1 - cut
        if cut > biggest:
            biggest, name = cut, ob["name"]
    return factor, name


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

# code: (text, day icon, night icon). Icon names match the SVGs in the
# "icon" template in shared.liquid.
WMO = {
    0: ("Clear", "wi-day-sunny", "wi-night-clear"),
    1: ("Mostly clear", "wi-day-sunny-overcast", "wi-night-alt-partly-cloudy"),
    2: ("Partly cloudy", "wi-day-cloudy", "wi-night-alt-cloudy"),
    3: ("Overcast", "wi-cloudy", "wi-cloudy"),
    45: ("Fog", "wi-fog", "wi-fog"),
    48: ("Freezing fog", "wi-fog", "wi-fog"),
    51: ("Light drizzle", "wi-sprinkle", "wi-sprinkle"),
    53: ("Drizzle", "wi-sprinkle", "wi-sprinkle"),
    55: ("Heavy drizzle", "wi-sprinkle", "wi-sprinkle"),
    56: ("Freezing drizzle", "wi-sleet", "wi-sleet"),
    57: ("Freezing drizzle", "wi-sleet", "wi-sleet"),
    61: ("Light rain", "wi-rain", "wi-rain"),
    63: ("Rain", "wi-rain", "wi-rain"),
    65: ("Heavy rain", "wi-rain", "wi-rain"),
    66: ("Freezing rain", "wi-sleet", "wi-sleet"),
    67: ("Freezing rain", "wi-sleet", "wi-sleet"),
    71: ("Light snow", "wi-snow", "wi-snow"),
    73: ("Snow", "wi-snow", "wi-snow"),
    75: ("Heavy snow", "wi-snow", "wi-snow"),
    77: ("Snow grains", "wi-snow", "wi-snow"),
    80: ("Showers", "wi-showers", "wi-showers"),
    81: ("Showers", "wi-showers", "wi-showers"),
    82: ("Heavy showers", "wi-showers", "wi-showers"),
    85: ("Snow showers", "wi-snow", "wi-snow"),
    86: ("Snow showers", "wi-snow", "wi-snow"),
    95: ("Thunderstorm", "wi-thunderstorm", "wi-thunderstorm"),
    96: ("Thunderstorm", "wi-thunderstorm", "wi-thunderstorm"),
    99: ("Thunderstorm", "wi-thunderstorm", "wi-thunderstorm"),
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
    text, day, night = WMO.get(code, ("Unknown", "wi-cloudy", "wi-cloudy"))
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
        sheltered_by = None

        if own_et0:
            t, h = mid(temp, i, j), mid(rh, i, j)
            elev, _ = sun_position(start + timedelta(minutes=30), *geo)
            # Open ground (the lawn), then the line with the garden applied.
            open_rate = et0_hourly(t, h, wind_mph * MPH * U10_TO_U2, sw[i] or 0, elev, z)
            rate = open_rate
            if garden:
                shade = shade_fraction(start, end, geo, garden)
                if wdir[i] is not None:
                    share, sheltered_by = wind_shelter(wdir[i], garden)
                    wind_mph *= share
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
            "wet": prob >= WET_PROB,
            "rh": mid(rh, i, j),
            "temp": temp[j],
            "wind": wind_mph,
            "open_wind": open_wind,
            "sheltered_by": sheltered_by,
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
# to clear before it's worth mowing. Whether it rains is judged on the chance,
# like the washing; the forecast amount only sets how wet it gets when it
# does (more rain, softer ground, longer to recover). Humid nights add dew.
# Open-ground ET0 dries it.
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

    Returns ((start, end) or None, whether the grass is wet at `after`).
    Spells before `after` are ignored.
    """
    wet_at_after = None
    water = MOW_DEW
    best, cur = None, None
    stop = evening_stop(hours, sset)
    for h in hours:
        if h["start"] >= stop:
            break
        rained = h["prob"] >= MOW_RAIN_PROB
        dry_at_start = water <= 0
        if wet_at_after is None and h["start"] + timedelta(hours=1) > after:
            wet_at_after = not dry_at_start or rained
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
    return (best if best and best[1] - best[0] >= MOW_MIN else None), bool(wet_at_after)


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
        winds.append((h["open_wind"], h["wind"], h.get("sheltered_by")))

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
        # Forecast (open) wind, and the share of it that reaches the line.
        "avg_wind": sum(o for o, _, _ in winds) / len(winds) if winds else None,
        "line_share": (sum(w for _, w, _ in winds) / sum(o for o, _, _ in winds))
                      if winds and sum(o for o, _, _ in winds) > 0 else 1.0,
        "sheltered_by": most_common([n for _, _, n in winds if n]) if any(n for _, _, n in winds) else None,
    }


def why_text(sim):
    """Short explanation of the drying conditions."""
    bits = []
    rh, wind = sim["avg_rh"], sim["avg_wind"]
    if rh is not None:
        bits.append("dry air" if rh < 60 else "humid" if rh > 80 else None)
    if wind is not None:
        # Describe the forecast wind like a forecast would, then say if the
        # garden takes much of it away.
        bits.append("calm" if wind < 4 else "light winds" if wind < 8 else
                    "breezy" if wind < 15 else "windy")
        if sim["line_share"] < SHELTERED:
            by = sim["sheltered_by"]
            bits.append(f"sheltered by the {by}" if by else "sheltered")
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
    return {"name": name, "out": hhmm(start), "latest": latest_text(hours, start, sunset),
            "loads": load_times(hours, start, sunset),
            "in_by": hhmm(sim["stop_at"]), "in_why": sim["stop_why"], "note": None}


def latest_text(hours, start, sunset):
    latest = latest_start(hours, start, sunset)
    return hhmm(latest) if latest and latest > quarter(start, up=True) else None


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
        if sunset <= h["start"] < next_sunrise and h["prob"] >= TONIGHT_PROB:
            return f"Rain from {hhmm(h['start'])} tonight, don't leave it out"
    return None


CHART_HOURS = 24


def hang_out(hours, h, now, suns):
    """Could a normal load hung out at the start of this hour (or now, for the
    current hour) be dry before it has to come in? "yes", "risky" or None."""
    start = max(h["start"], now)
    day = suns.get(start.date())
    if not day or h["wet"] or not h["day"] or start < day[0] or start >= day[1]:
        return None
    sim = simulate(hours, start, DRY_NEED[VERDICT_LOAD], day[1])
    if not sim["dry_at"]:
        return None
    return "risky" if is_risky(sim) else "yes"


def latest_start(hours, start, sunset):
    """Latest time (to the quarter hour) a normal load hung out from `start`
    onwards would still be dry before it has to come in."""
    need = DRY_NEED[VERDICT_LOAD]
    t, last = quarter(start, up=True), None
    while t < sunset:
        if simulate(hours, t, need, sunset)["dry_at"]:
            last = t
        elif last:
            break
        t += timedelta(minutes=15)
    return last


def timeline(hours, now, suns):
    """One bar per hour for the next 24 hours, starting with the current one.

    Bar height is drying strength (zero at night); rain chance rides on top.
    Black bars are hours you could hang out a normal load and have it dry.
    """
    first = now.replace(minute=0, second=0, microsecond=0)
    bars = []
    for h in hours:
        if not first <= h["start"] < first + timedelta(hours=CHART_HOURS):
            continue
        bars.append({
            "label": h["start"].strftime("%H"),
            "tick": h["start"].hour % 3 == 0 or h["start"] == first,
            "pct": min(100, rnd(h["rate"] / RATE_FULL * 100)) if h["day"] else 0,
            "prob": rnd(h["prob"]),
            "wet": h["wet"],
            "hang": hang_out(hours, h, now, suns),
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
        mow, wet_now = mow_window(hours, rise, sset, max(now, rise))
        if mow:
            mow_text = f"Mow {hhmm(quarter(mow[0], up=True))}–{hhmm(quarter(mow[1]))}"
        elif now + MOW_MIN > evening_stop(hours, sset):
            mow_text = "Too late to mow"
        elif wet_now:
            mow_text = "Too wet to mow"
        else:
            mow_text = "Not enough time to mow"
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
    if raining_now:
        # It's raining now, whatever the forecast chance said: this hour's wet.
        for h in hours:
            if h["start"] <= now < h["start"] + timedelta(hours=1):
                h["wet"] = True
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
                                     "latest": latest_text(hours, win["start"], suns[win["start"].date()][1])
                                               if wash["dry_by"] else None,
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
        # Even when it's not worth putting out, say when anything already on
        # the line has to come in.
        if now < sunset:
            in_at, in_why = next_stop(hours, now, sunset)
            in_by = "Now" if in_at <= now else hhmm(in_at)
        else:
            in_by, in_why = "Now", "sunset"
        rows[today] = {"name": "Today", "out": None, "loads": [], "in_by": in_by, "in_why": in_why,
                       "note": note}
    if tomorrow not in rows and tomorrow in suns:
        rows[tomorrow] = day_plan("Tomorrow", hours, *suns[tomorrow])
    wash["plan"] = [rows[d] for d in sorted(rows)]

    bars = timeline(hours, now, suns)
    wash["window"] = None  # datetimes aren't JSON; the bars carry it now

    text, icon = describe(cur.get("weather_code"), is_day)
    return {
        "now": {
            "time": hhmm(now),
            "text": "Raining" if raining_now and (cur.get("weather_code") or 0) < 51 else text,
            "icon": icon,
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
    }
