"""
Ministry of Meteorology, Laundry & Associated Atmospheric Affairs (MMLAAA):
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

# An hour is wet if either is true.
WET_PROB = 40       # % precipitation probability
WET_MM = 0.2        # mm precipitation forecast in the hour

# A dry window that still carries this much rain chance is "risky".
RISKY_PROB = 25     # %
# Finishing less than this long before rain or sunset is "risky".
MARGIN = timedelta(hours=1)
# Below this fraction dry by the end of the window, it's not worth it.
PARTIAL_OK = 0.6

# Drying rate that fills the timeline bar to 100%.
RATE_FULL = 0.45    # mm/h

# ---- Garden ---------------------------------------------------------------
# Plugin settings describe where the line is relative to the house. With
# "none" there's no adjustment.

FACES = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315}
FACES_WORDS = {"north": "n", "north-east": "ne", "northeast": "ne", "east": "e",
               "south-east": "se", "southeast": "se", "south": "s",
               "south-west": "sw", "southwest": "sw", "west": "w",
               "north-west": "nw", "northwest": "nw"}

# House profile, metres: (eaves height at the back wall, ridge height, ridge
# set back from the wall). The ridge is assumed parallel to the back wall.
HOUSE = {"1": (2.7, 5.5, 4.0), "2": (5.3, 8.5, 4.0), "3": (7.8, 11.0, 4.0)}
DEFAULT_HOUSE = "2"
LINE_HEIGHT = 1.7   # m, roughly the middle of the hanging washing
DEFAULT_DISTANCE = 6

# Wind reduction behind a building, by distance in building (ridge) heights,
# for wind blowing straight over it. Scaled by cos(angle) for oblique wind.
WAKE = [(0, 0.6), (1, 0.6), (2, 0.45), (4, 0.25), (8, 0.05), (12, 0.0)]

# Extra wind reduction from fences, hedges and other houses.
SHELTER = {"open": 1.0, "some": 0.85, "enclosed": 0.7}


def parse_garden(fields):
    """Plugin settings -> garden dict, or None for no adjustment.

    TRMNL may pass either a select's value or its label, so accept both.
    """
    def pick(key):
        return str(fields.get(key) or "").strip().lower()

    faces = pick("garden_faces")
    faces = FACES_WORDS.get(faces, faces)
    if faces not in FACES:
        return None

    height = pick("house_height")
    height = ("3" if "loft" in height or height.startswith("3")
              else "1" if height.startswith("1") or "bungalow" in height
              else "2" if height else DEFAULT_HOUSE)
    digits = "".join(c for c in pick("line_distance") if c.isdigit())
    distance = int(digits) if digits else DEFAULT_DISTANCE
    shelter = pick("shelter").split()[0] if pick("shelter") else "open"
    if shelter not in SHELTER:
        shelter = "open"

    eaves, ridge, setback = HOUSE[height]
    return {"faces": faces, "azimuth": FACES[faces], "distance": distance,
            "eaves": eaves, "ridge": ridge, "setback": setback,
            "shelter": SHELTER[shelter]}


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


def line_shaded(elev, az, garden):
    """Is the line in the house's shadow with the sun here?"""
    if elev <= 0:
        return True
    behind = -math.cos(math.radians(az - garden["azimuth"]))
    if behind <= 0:
        return False  # sun is on the garden side of the house
    reach = behind / math.tan(math.radians(elev))
    for height, setback in ((garden["eaves"], 0.0), (garden["ridge"], garden["setback"])):
        if (height - LINE_HEIGHT) * reach - setback >= garden["distance"]:
            return True
    return False


def shade_fraction(start, end, geo, garden, steps=4):
    step = (end - start) / steps
    samples = [start + step * (k + 0.5) for k in range(steps)]
    return sum(line_shaded(*sun_position(t, *geo), garden) for t in samples) / steps


def wind_factor(from_deg, garden):
    """Share of the forecast wind that reaches the line."""
    house_dir = (garden["azimuth"] + 180) % 360
    c = math.cos(math.radians(from_deg - house_dir))
    reduction = 0.0
    if c > 0:
        x = garden["distance"] / garden["ridge"]
        for (x0, r0), (x1, r1) in zip(WAKE, WAKE[1:]):
            if x <= x1:
                reduction = r0 + (r1 - r0) * (x - x0) / (x1 - x0)
                break
        reduction *= c
    return (1 - reduction) * garden["shelter"]


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


def rnd(x):
    return int(round(x))


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

    def col(name):
        return hourly.get(name) or [None] * n

    def mid(values, i, j):
        a, b = values[j], values[i]
        return b if a is None else a if b is None else (a + b) / 2

    temp, rh, wind = col("temperature_2m"), col("relative_humidity_2m"), col("wind_speed_10m")
    wdir, code, is_day = col("wind_direction_10m"), col("weather_code"), col("is_day")
    sw, direct = col("shortwave_radiation"), col("direct_radiation")
    own_et0 = sw[0] is not None and temp[0] is not None
    geo = (data.get("utc_offset_seconds", 0), data.get("latitude", 0), data.get("longitude", 0))
    z = data.get("elevation") or 0

    hours = []
    for i, ts in enumerate(hourly["time"]):
        j = max(i - 1, 0)
        end = parse(ts)
        start = end - timedelta(hours=1)
        prob = hourly["precipitation_probability"][i] or 0
        mm = hourly["precipitation"][i] or 0
        wind_mph = mid(wind, i, j) or 0
        shade = 0.0

        if own_et0:
            if garden:
                shade = shade_fraction(start, end, geo, garden)
                if wdir[i] is not None:
                    wind_mph *= wind_factor(wdir[i], garden)
            rs = (sw[i] or 0) - (direct[i] or 0) * shade
            elev, _ = sun_position(start + timedelta(minutes=30), *geo)
            rate = et0_hourly(mid(temp, i, j), mid(rh, i, j), wind_mph * MPH * U10_TO_U2, rs, elev, z)
        else:
            rate = col("et0_fao_evapotranspiration")[i] or 0

        hours.append({
            "start": start,
            "rate": rate,
            "prob": prob,
            "mm": mm,
            "day": bool(is_day[j] or is_day[i]),
            "wet": prob >= WET_PROB or mm >= WET_MM,
            "rh": mid(rh, i, j),
            "temp": temp[j],
            "wind": wind_mph,
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
      max_prob highest rain chance while it's out
    """
    got = 0.0
    max_prob = 0
    humid = []
    winds = []
    for h in hours:
        h_end = h["start"] + timedelta(hours=1)
        if h_end <= start:
            continue
        if h["start"] >= sunset:
            return _result(None, sunset, "sunset", got / need, max_prob, humid, winds)
        if h["wet"]:
            stop = max(h["start"], start)
            return _result(None, stop, "rain", got / need, max_prob, humid, winds)

        # Only count the part of the hour that's after `start` and before sunset.
        seg_start = max(h["start"], start)
        seg_end = min(h_end, sunset)
        frac = (seg_end - seg_start).total_seconds() / 3600
        rate = h["rate"] if h["day"] else 0.0
        max_prob = max(max_prob, h["prob"])
        humid.append(h["rh"])
        winds.append(h["wind"])

        if rate > 0 and got + rate * frac >= need:
            dry_at = seg_start + timedelta(hours=(need - got) / rate)
            # Look ahead: when would it have to come in anyway?
            stop, why = next_stop(hours, dry_at, sunset)
            return _result(dry_at, stop, why, 1.0, max_prob, humid, winds)
        got += rate * frac
    return _result(None, sunset, "sunset", got / need, max_prob, humid, winds)


def next_stop(hours, after, sunset):
    for h in hours:
        if h["start"] + timedelta(hours=1) <= after:
            continue
        if h["start"] >= sunset:
            break
        if h["wet"]:
            return max(h["start"], after), "rain"
    return sunset, "sunset"


def _result(dry_at, stop_at, why, done, max_prob, humid, winds):
    return {
        "dry_at": dry_at,
        "stop_at": stop_at,
        "stop_why": why,
        "done": min(done, 1.0),
        "max_prob": max_prob,
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
    if sim["max_prob"] < 15:
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


def wait_reason(hours, now, start, raining_now):
    """Why wait until `start`? Describe the rain in between."""
    wet = [h for h in hours if h["wet"] and now < h["start"] + timedelta(hours=1) and h["start"] < start]
    if not wet:
        return "Better drying later"
    until = hhmm(wet[-1]["start"] + timedelta(hours=1))
    if raining_now:
        return f"Should clear by {until}"
    if wet[0]["start"] <= now:
        return f"Rain likely until {until}"
    return f"Rain {hhmm(wet[0]['start'])}–{until}, then dry"


def risky_reason(sim):
    if sim["stop_at"] - sim["dry_at"] < MARGIN:
        if sim["stop_why"] == "rain":
            return f"Rain due {hhmm(sim['stop_at'])}, cutting it fine"
        return "Only just dry by sunset"
    return f"{rnd(sim['max_prob'])}% chance of a shower"


def not_today_reason(today_sim, now, sunset, raining_now):
    if now >= sunset:
        return "Too late for today"
    if today_sim and today_sim["stop_why"] == "rain":
        if raining_now or today_sim["stop_at"] <= now:
            return "Wet on and off until dark"
        return f"Rain from {hhmm(today_sim['stop_at'])}"
    return "Not enough drying time left today"


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
                return verdict("risky", "Risky, but go", risky_reason(sim),
                               start=out, sim=sim)
            head = "Put it out now" if now >= sunrise else f"Put it out at {hhmm(sunrise)}"
            return verdict("go", head, f"Dry in {duration(sim['dry_at'] - out)}",
                           start=out, sim=sim)

        start, later = first_good_start(hours, now, need, sunset)
        if start:
            return verdict("wait", "Raining now" if raining_now else "Wait",
                           wait_reason(hours, now, start, raining_now),
                           start=start, sim=later)

        if not raining_now and sim["done"] >= PARTIAL_OK and sim["stop_at"] > now:
            return verdict("risky", "Partly dry at best",
                           f"About {rnd(sim['done'] * 100)}% dry by {hhmm(sim['stop_at'])}",
                           start=out, sim=sim, partial=True)

    # Nothing today: try tomorrow.
    if tomorrow in suns:
        t_rise, t_set = suns[tomorrow]
        start, t_sim = first_good_start(hours, t_rise, need, t_set)
        if start:
            v = verdict("tomorrow", "Not today",
                        not_today_reason(sim, now, sunset, raining_now),
                        start=start, sim=t_sim)
            v["when"] = "tomorrow"
            return v

    return verdict("no", "Dry it indoors", "No drying window today or tomorrow")


def verdict(code, headline, detail, start=None, sim=None, partial=False):
    v = {"code": code, "icon": VERDICT_ICON[code], "when": "today",
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


def rain_tonight(hours, sunset, next_sunrise):
    """First wet hour between sunset and the next sunrise, as a warning."""
    for h in hours:
        if sunset <= h["start"] < next_sunrise and h["wet"]:
            return f"Rain from {hhmm(h['start'])} tonight, don't leave it out"
    return None


def timeline(hours, day, suns, win, now):
    """One bar per daylight hour of `day`, for the chart strip."""
    rise, sset = suns[day]
    first = rise.replace(minute=0)
    bars = []
    for h in hours:
        if h["start"].date() != day or h["start"] < first or h["start"] >= sset:
            continue
        in_win = bool(win) and win["start"] < h["start"] + timedelta(hours=1) and h["start"] < win["end"]
        bars.append({
            "label": h["start"].strftime("%H"),
            "pct": min(100, rnd(h["rate"] / RATE_FULL * 100)),
            "prob": rnd(h["prob"]),
            "wet": h["wet"],
            "win": in_win,
            "past": h["start"] + timedelta(hours=1) <= now,
        })
    return bars


def shade_text(rise, sset, geo, garden):
    """When the house shades the line during the day, in words."""
    step = timedelta(minutes=5)
    t, shaded, changes = rise, [], []
    while t < sset:
        elev, az = sun_position(t, *geo)
        if elev >= 3:  # ignore the sun skimming the horizon
            shaded.append((t, line_shaded(elev, az, garden)))
        t += step
    if not shaded or not any(s for _, s in shaded):
        return None
    if all(s for _, s in shaded):
        return "house shades the line all day"
    for (t0, s0), (t1, s1) in zip(shaded, shaded[1:]):
        if s0 != s1:
            changes.append((t1, s1))
    parts = []
    if shaded[0][1]:
        parts.append(f"until {hhmm(changes[0][0])}")
        changes = changes[1:]
    parts += [f"from {hhmm(t)}" for t, s in changes if s]
    return "house shade " + " and ".join(parts) if parts else None


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
    for i in range(min(2, len(daily["time"]))):
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
        days.append({
            "name": "Today" if i == 0 else "Tomorrow",
            "text": text,
            "icon": icon,
            "hi": rnd(daily["temperature_2m_max"][i]),
            "lo": rnd(lo),
            "rain": rnd(rain),
            "sunrise": hhmm(rise),
            "sunset": hhmm(sset),
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

    # Chart today's daylight; after sunset, chart tomorrow instead.
    chart_day = now.date()
    if now >= suns[chart_day][1] or wash["code"] == "tomorrow":
        chart_day = chart_day + timedelta(days=1)
    bars = timeline(hours, chart_day, suns, wash["window"], now)
    now_label = now.strftime("%H") if chart_day == now.date() else None
    wash["window"] = None  # datetimes aren't JSON; the bars carry it now

    text, icon = describe(cur["weather_code"], is_day)
    return {
        "now": {
            "time": hhmm(now),
            "text": "Raining" if raining_now and cur["weather_code"] < 51 else text,
            "icon": icon,
            "temp": rnd(cur["temperature_2m"]),
            "feels": rnd(cur["apparent_temperature"]),
            "humidity": rnd(cur["relative_humidity_2m"]),
            "wind": rnd(cur["wind_speed_10m"]),
            "gust": rnd(cur["wind_gusts_10m"]),
            "wind_dir": compass(cur["wind_direction_10m"]),
        },
        "today": days[0],
        "tomorrow": days[1] if len(days) > 1 else None,
        "wash": wash,
        "garden": f"{garden['faces'].upper()} garden, line {garden['distance']} m" if garden else None,
        "chart": {
            "day": "Today" if chart_day == now.date() else "Tomorrow",
            "bars": bars,
            "now": now_label,
        },
    }
