"""
Nature's Clothes Horse: TRMNL serverless transform.

Input: the Open-Meteo /v1/forecast response (polled by TRMNL), plus the
`trmnl` namespace that TRMNL adds (we read the "load" custom field from it).
Output: a small, template-ready dict: current conditions, today/tomorrow,
and a washing verdict.

The drying model
----------------
Each forecast hour gets a drying rate: the FAO-56 reference
evapotranspiration (ET0, mm/h). That's the standard "how fast does water
evaporate from a surface" figure, and it already combines sunshine,
temperature, humidity and wind. A load of washing is dry once the ET0 it has
been exposed to adds up to DRY_NEED[load].

An hour is "wet" (washing must be in) if rain is forecast or likely.
Drying only counts during daylight: evenings bring dew, not drying.

All the tunable numbers are in the constants block below. They're a first
guess, to be calibrated against real washing.

Stdlib only: TRMNL's transform runtime has no pip installs.
"""
from datetime import datetime, timedelta

# ---- Tunables -------------------------------------------------------------

# Cumulative ET0 (mm) a load needs to be dry.
DRY_NEED = {"light": 0.9, "normal": 1.3, "heavy": 1.9}
DEFAULT_LOAD = "normal"

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

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def describe(code, is_day=True):
    text, day, night = WMO.get(code, ("Unknown", "wi-na", "wi-na"))
    return text, (day if is_day else night)


def compass(degrees):
    return COMPASS[round(degrees / 22.5) % 16]


def parse(ts):
    return datetime.fromisoformat(ts)


def hhmm(dt):
    return dt.strftime("%H:%M")


def rnd(x):
    return int(round(x))


# ---- Hours ---------------------------------------------------------------

def build_hours(hourly):
    """Zip Open-Meteo's column arrays into one dict per hour."""
    hours = []
    for i, ts in enumerate(hourly["time"]):
        prob = hourly["precipitation_probability"][i] or 0
        mm = hourly["precipitation"][i] or 0
        hours.append({
            "start": parse(ts),
            "rate": hourly["et0_fao_evapotranspiration"][i] or 0,
            "prob": prob,
            "mm": mm,
            "day": bool(hourly["is_day"][i]),
            "wet": prob >= WET_PROB or mm >= WET_MM,
            "rh": hourly["relative_humidity_2m"][i],
            "wind": hourly["wind_speed_10m"][i],
        })
    return hours


def sun_times(daily):
    """{date: (sunrise, sunset)}"""
    return {
        parse(d).date(): (parse(r), parse(s))
        for d, r, s in zip(daily["time"], daily["sunrise"], daily["sunset"])
    }


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
    p = sim["max_prob"]
    bits.append("low rain risk" if p < 15 else f"{rnd(p)}% rain chance")
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


def washing(hours, now, suns, need, raining_now):
    """The verdict for one load size."""
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    sunrise, sunset = suns[today]

    if raining_now:
        start, sim = first_good_start(hours, now, need, sunset)
        if start:
            return verdict("wait", "Raining now",
                           f"Put it out at {hhmm(start)}",
                           start=start, sim=sim)
    elif now < sunset:
        sim = simulate(hours, max(now, sunrise), need, sunset)
        if sim["dry_at"]:
            code = "risky" if is_risky(sim) else "go"
            head = "Put it out now" if now >= sunrise else f"Put it out at {hhmm(sunrise)}"
            if code == "risky":
                head = "Risky, but go"
            return verdict(code, head, why_text(sim),
                           start=max(now, sunrise), sim=sim)

        start, later = first_good_start(hours, now, need, sunset)
        if start:
            return verdict("wait", "Wait",
                           f"Put it out at {hhmm(start)}",
                           start=start, sim=later)

        if sim["done"] >= PARTIAL_OK and sim["stop_at"] > now:
            return verdict("risky", "Partly dry at best",
                           f"About {rnd(sim['done'] * 100)}% dry by {hhmm(sim['stop_at'])}",
                           start=max(now, sunrise), sim=sim, partial=True)

    # Nothing today: try tomorrow.
    if tomorrow in suns:
        t_rise, t_set = suns[tomorrow]
        start, sim = first_good_start(hours, t_rise, need, t_set)
        if start:
            return verdict("tomorrow", "Not today",
                           f"Tomorrow from {hhmm(start)}", start=start, sim=sim)

    return verdict("no", "Dry it indoors", "No drying window today or tomorrow")


def verdict(code, headline, detail, start=None, sim=None, partial=False):
    v = {"code": code, "icon": VERDICT_ICON[code],
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
        if code == "go":
            v["detail"] = f"Dry by {hhmm(sim['dry_at'])}"
    return v


# ---- Timeline ------------------------------------------------------------

def timeline(hours, day, suns, win):
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
        })
    return bars


# ---- Entry point ---------------------------------------------------------

def run(input):
    if "hourly" not in input:
        reason = input.get("reason") or "No forecast data"
        return {"error": reason}

    settings = (input.get("trmnl") or {}).get("plugin_settings") or {}
    fields = settings.get("custom_fields_values") or {}
    load = str(fields.get("load") or DEFAULT_LOAD).lower()
    if load not in DRY_NEED:
        load = DEFAULT_LOAD

    cur = input["current"]
    now = parse(cur["time"])
    is_day = bool(cur["is_day"])
    hours = build_hours(input["hourly"])
    suns = sun_times(input["daily"])
    daily = input["daily"]

    days = []
    for i in range(min(2, len(daily["time"]))):
        text, icon = describe(daily["weather_code"][i])
        rise, sset = suns[parse(daily["time"][i]).date()]
        days.append({
            "name": "Today" if i == 0 else "Tomorrow",
            "text": text,
            "icon": icon,
            "hi": rnd(daily["temperature_2m_max"][i]),
            "lo": rnd(daily["temperature_2m_min"][i]),
            "rain": rnd(daily["precipitation_probability_max"][i] or 0),
            "sunrise": hhmm(rise),
            "sunset": hhmm(sset),
        })

    raining_now = (cur.get("precipitation") or 0) > 0
    wash = washing(hours, now, suns, DRY_NEED[load], raining_now)

    # Chart today's daylight; after sunset, chart tomorrow instead.
    chart_day = now.date()
    if now >= suns[chart_day][1] or wash["code"] == "tomorrow":
        chart_day = chart_day + timedelta(days=1)
    bars = timeline(hours, chart_day, suns, wash["window"])
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
        "load": load,
        "chart": {
            "day": "Today" if chart_day == now.date() else "Tomorrow",
            "bars": bars,
            "now": now_label,
        },
    }
