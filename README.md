# The Ministry of Meteorology, Laundry & Regional Atmospheric Affairs

**MoMRAAA**, or the Ministry of Meteorology & Laundry Affairs for short: a
TRMNL plugin that decides when to hang the washing out.

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
  [Weather Icons](https://erikflowers.github.io/weather-icons/) by Erik
  Flowers (SIL OFL 1.1): the 20 the plugin uses are embedded as inline SVG
  in `src/shared.liquid`, so nothing is loaded from a CDN. (Not in
  `transform.py`: TRMNL rejects a transform that large.)

### The drying model

Each hour's drying rate is the FAO-56 reference evapotranspiration (ET0,
mm/h). This is the standard measure of how fast water evaporates, and it
combines sunshine, temperature, humidity and wind. We calculate it ourselves
from Open-Meteo's hourly sunshine, temperature, humidity and wind (it matches
Open-Meteo's own ET0 to within 1%), so the garden can adjust the inputs. A load is dry once the ET0
it's been exposed to adds up to a threshold: light 0.9, normal 1.3, heavy
1.9 mm. The verdict is for a normal load; the screen also shows when light
and heavy loads would be dry if hung out at the same time.

- Rain is judged on its **chance** alone: that comes from many model runs,
  so it already allows for their disagreement, while the forecast amount is
  one run's guess. An hour is **wet** (rain likely: the washing has to come
  in before it, and the chart hatches it) from 60%. From 20% up to that, the
  washing stays out but the hour's drying is scaled by the chance it stays
  dry, so a 35% hour gives 65% of its drying. The overnight warning starts
  at 40%. If it's raining right now, the current hour counts as wet.
- Drying stops at **sunset**.
- **Risky** means it'll dry, but with less than an hour to spare before rain
  or sunset, or with a rain chance of 25% or more along the way. It also
  covers days where the washing gets at least 60% dry.

Under the verdict, a small table gives **Today** and **Tomorrow** rows: when
to put it out, when light, normal and heavy loads would be dry, and when it
has to come in. Tomorrow's row uses the earliest start that dries a normal
load (or a light one if that's all that's possible). If nothing would dry,
the row says why ("Too late today", "Rain most of the day"). Today's row
always gives an **In by** time, for washing that's already out.

The chart shows the **next 24 hours** from now. Bar height is drying
strength (none at night, marked by a black "night" band). The shading says
what you can do in that hour:

- **black:** hang out a normal load now and it'll be dry before it has to
  come in;
- **black/grey stripes:** you could, but it's risky (a shower chance while
  it's out, or dry with under an hour to spare);
- **grey:** drying still happens, so anything already out keeps drying, but
  it's too late to hang out a new normal load;
- **hatched:** rain likely (60%+).

Any hour with a 20%+ chance of rain shows the percentage, so the chart
doubles as a "will I get wet going out?" guide. The table's Out time also
gives the **latest** time to hang out a normal load, to the quarter hour.

The verdict explains itself: how long drying will take, when the rain
arrives or clears, or why today is a write-off. If rain is due overnight it
says so, in case the washing would otherwise stay out.

Today's and tomorrow's conditions and rain chance cover **daylight hours
only**. Open-Meteo's daily summary is the worst weather in the whole 24 hours,
so a sunny day after a drizzly night would otherwise read "Light drizzle".
The low shown for each day is the **coming night's** low (sunset to the next
sunrise), for planning blankets and windows, not the calendar day's minimum.

### Your garden

Optional settings list up to three **obstructions** near the line: your
house, a fence, a shed, a tree. Each has a type, a direction *from the line*
(the house is north-west of the line in a south-east facing garden) and a
distance. **General exposure** tweaks wind for everything else.

| Type | Modelled as |
|---|---|
| Fence or wall, full width | 1.8 m, runs right across, fairly solid to wind |
| Fence or wall, short section | 1.8 m, 5 m long, so wind and sun get round it |
| Tall hedge | 3 m, runs right across, lets some sun and wind through |
| Shed or garage | 2.5 m, 3 m wide |
| Bungalow / house | eaves plus a ridge set back 4 m (ridge 5.5, 8.5 or 11 m); runs right across, since neighbours usually continue the building line |
| Tree | 10 m, 6 m wide, blocks 70% of direct sun and half as much wind as a wall |

Each hour:

- **Shade:** the sun's position is calculated, and if it's below the top of
  an obstruction as seen from the line, that share of direct sunshine is
  removed (sky light still counts). The verdict names it, e.g. "house shade
  from 16:27". Shade times move with the seasons automatically.
- **Wind:** wind blowing over an obstruction towards the line is reduced,
  most when the line is within one or two obstruction-heights and the wind
  blows straight over it.

The washing is treated as a band hanging about 0.8 m below the line, whose
height is a setting (5-8 ft; default 6.5 ft, 2.0 m). An obstruction shades
only the part of the band below its shadow, and shelters only the part below
its top, so on a high line most of the washing clears a 1.8 m fence. The
conditions line names the main wind blocker when less than 70% of the wind
reaches the line ("sheltered by the house"). Shade
timing should be good to roughly half an hour; wind shelter is a rough
estimate, to be tuned with real drying times.

With no obstructions and open exposure, there's no adjustment. The title bar
shows what's in use (e.g. "NW house 4 m, SW fence 2 m"), or "Garden settings
not applied" if TRMNL didn't pass the settings to the transform.

### Mowing window

Each day box shows the longest spell (at least 2 hours) when the lawn should
be dry enough to mow, e.g. "Mow 11:00–17:30", or else "Too wet to mow" (the
grass is wet now), "Not enough time to mow" or "Too late to mow". The lawn
holds some water that open-ground drying has to clear. Whether it rains is
judged on the chance (50%+), like the washing; when it does, it leaves 0.4 mm
plus 0.15 mm per mm of forecast rain (soggy ground takes longer), capped at 2.5 mm, and
a humid night (RH ≥ 90%) leaves 0.3 mm of dew. In the evening, mowing runs
until sunset unless dew is likely first: in the last two hours before
sunset it stops at the first hour humid enough for dew, which is 75-92% RH
depending on wind and cloud (calm, clear evenings dew soonest). It assumes a typical lawn (a few cm long); the numbers are in the
Mowing block of `src/transform.py`. The forecast includes the previous day so
yesterday's rain counts.

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

- The plugin name is over 50 characters (it's the full ceremonial name; the
  screen uses the short one).
- Too many custom styles. The check counts any CSS property in the markup,
  including the shared stylesheet in `src/shared.liquid`.
- Most obstruction fields are "not used in markup": they're read by the
  transform, which lint doesn't look at.
- `lat_lon` is an unknown field type. TRMNL documents and supports it, but
  the linter hasn't caught up.

## Roadmap

- **Phase 2: real-time observations.** Use a nearby station's live rain
  sensor so "it's raining now" overrides the forecast.
- Calibrate the drying thresholds against real loads.
