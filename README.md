# natures-clothes-horse
A "will it rain?" TRMNL plugin

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
combines sunshine, temperature, humidity and wind. A load is dry once the ET0
it's been exposed to adds up to a threshold: light 0.9, normal 1.3, heavy
1.9 mm.

- An hour counts as **wet** if rain chance is ≥ 40% or ≥ 0.2 mm is forecast.
  The washing has to come in before it.
- Drying stops at **sunset**.
- **Risky** means it'll dry, but with less than an hour to spare before rain
  or sunset, or with a rain chance of 25% or more along the way. It also
  covers days where the washing gets at least 60% dry.

All thresholds are in the tunables block at the top of `src/transform.py`.
They're first guesses and need calibrating against real washing.

## Setup

Uses [trmnlp](https://github.com/usetrmnl/trmnlp) via Docker (no Ruby
needed). From the project folder:

```sh
# Once: paste your API key from your TRMNL account page when asked
docker run -it --rm -v "$HOME/.config/trmnlp:/root/.config/trmnlp" \
  -v "$PWD:/plugin" trmnl/trmnlp login

# Each time you want to upload changes
docker run -it --rm -v "$HOME/.config/trmnlp:/root/.config/trmnlp" \
  -v "$PWD:/plugin" trmnl/trmnlp push
```

The first push creates the private plugin and writes its `id` into
`src/settings.yml`. Commit that, so later pushes update the same plugin
instead of creating a new one.

Then, in TRMNL, open the plugin's settings. Set **Location** (search for a place or enter `lat,lon`) and **Typical load**.

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

## Roadmap

- **Phase 2: real-time observations.** Use a nearby station's live rain
  sensor so "it's raining now" overrides the forecast.
- Calibrate the drying thresholds against real loads.
