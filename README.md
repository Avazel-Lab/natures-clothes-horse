# Ministry of Meteorology, Laundry & Associated Atmospheric Affairs

**MMLAAA**: a TRMNL plugin that decides when to hang the washing out.

Tells you whether to hang the washing out, when it'll be dry, and when it has
to come in, alongside the usual weather: current conditions, feels-like,
humidity, wind, today and tomorrow's highs/lows, sunrise and sunset.

Designed for the TRMNL X (1872×1404, 16 greys), with smaller layouts for
mashups.

## How it works

- **Data:** [Open-Meteo](https://open-meteo.com/) (free, no API key). In the
  UK its default model uses Met Office UKV data (2 km grid) and adds hourly
  rain probabilities. TRMNL polls it directly using the location you set in
  the plugin settings.
- **Engine:** `src/transform.py` runs as a TRMNL serverless transform. It turns
  the forecast into a verdict (`go`, `risky`, `wait`, `tomorrow` or `no`) and a
  timeline of hourly drying strength.
- **Screen:** Liquid templates in `src/` using the TRMNL framework, with
  [Weather Icons](https://erikflowers.github.io/weather-icons/).

### The drying model

Each hour's drying rate is the FAO-56 reference evapotranspiration (ET0,
mm/h). This is the standard measure of how fast water evaporates, and it
combines sunshine, temperature, humidity and wind. We calculate it ourselves
from Open-Meteo's hourly sunshine, temperature, humidity and wind (it matches
Open-Meteo's own ET0 to within 1%), so the garden can adjust the inputs. A load is dry once the ET0
it's been exposed to adds up to a threshold: light 0.9, normal 1.3, heavy
1.9 mm. The verdict is for a normal load; the screen also shows when light
and heavy loads would be dry if hung out at the same time.

- An hour counts as **wet** if rain chance is ≥ 40% or ≥ 0.2 mm is forecast.
  The washing has to come in before it.
- Drying stops at **sunset**.
- **Risky** means it'll dry, but with less than an hour to spare before rain
  or sunset, or with a rain chance of 25% or more along the way. It also
  covers days where the washing gets at least 60% dry.

The verdict explains itself: how long drying will take, when the rain
arrives or clears, or why today is a write-off. If rain is due overnight it
says so, in case the washing would otherwise stay out.

Today's and tomorrow's conditions and rain chance cover **daylight hours
only**. Open-Meteo's daily summary is the worst weather in the whole 24 hours,
so a sunny day after a drizzly night would otherwise read "Light drizzle".
The low shown for each day is the **coming night's** low (sunset to the next
sunrise), for planning blankets and windows, not the calendar day's minimum.

### Your garden

Optional settings describe where the line is: which way the back of the
house faces, its height, the line's distance from the house, and other
shelter. From these, each hour:

- **Shade:** the sun's position is calculated, and when the house's shadow
  reaches the line, direct sunshine is removed (sky light still counts). The
  verdict says when, e.g. "house shade from 16:32". The shade time moves with
  the seasons automatically.
- **Wind shelter:** wind blowing over the house towards the line is reduced,
  most when the line is within one or two house-heights and the wind blows
  straight over; wind along the wall or from the garden side isn't. Fences
  and hedges reduce it further.

The house is treated as a long wall with a ridge set back 4 m, which is
good for shade timing (roughly ±30 min). Wind shelter is a rough estimate,
to be tuned with real drying times.

With "No house nearby" there is no adjustment. The title bar shows the
garden in use (e.g. "SE garden, line 4 m"), or "Garden settings not applied"
if TRMNL didn't pass the settings to the transform.

All thresholds are in the tunables block at the top of `src/transform.py`.
They're first guesses and need calibrating against real washing.

## Setup

Uses [trmnlp](https://github.com/usetrmnl/trmnlp) via Docker (no Ruby
needed). Create an account API key on your TRMNL account page. `trmnlp login`
only accepts legacy `user_` keys, so pass the new `trmnl_` key through the
environment instead. From the project folder:

```sh
read -rs TRMNL_API_KEY && export TRMNL_API_KEY   # paste key, press Enter
docker run -it --rm -e TRMNL_API_KEY -v "$PWD:/plugin" trmnl/trmnlp push
```

The first push creates the private plugin and writes its `id` into
`src/settings.yml`. Commit that, so later pushes update the same plugin
instead of creating a new one.

Then, in TRMNL, open the plugin's settings and set **Location** (search for a place or enter `lat,lon`), and optionally the garden settings.

## Local preview

```sh
docker run --rm -p 4567:4567 -e WASHING_LOCATION="51.45,-0.97" \
  -v "$PWD:/plugin" trmnl/trmnlp serve
```

Then open http://localhost:4567. To see it as a TRMNL X renders it:

```
http://localhost:4567/render/full.png?width=1872&height=1404&color_depth=4&screen_classes=screen%20screen--v2%20screen--lg%20screen--density-2x%20screen--4bit
```

## Tests

```sh
python3 -m unittest discover tests
```

## Lint

`trmnlp lint` reports some warnings, all fine for a private plugin:

- The plugin name is over 50 characters (it's the full Ministry name).
- Too many custom styles. The check counts any CSS property in the markup,
  including the shared stylesheet in `src/shared.liquid`.
- The garden fields are "not used in markup": they're read by the transform,
  which lint doesn't look at.
- `lat_lon` is an unknown field type. TRMNL documents and supports it, but
  the linter hasn't caught up.

## Roadmap

- **Phase 2: real-time observations.** Use a nearby station's live rain
  sensor so "it's raining now" overrides the forecast.
- Calibrate the drying thresholds against real loads.
