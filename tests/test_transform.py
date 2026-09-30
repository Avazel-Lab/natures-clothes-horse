"""Tests for the washing engine. Run: python3 -m unittest discover tests"""
import json
import sys
from datetime import datetime
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import transform  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def forecast(now="2026-09-30T10:00", rate=0.3, prob=0, mm=0.0, overrides=None,
             sunrise="07:00", sunset="19:00", raining_now=False, codes=None):
    """A synthetic two-day Open-Meteo response.

    Every daylight hour gets `rate` ET0, `prob` rain chance and `mm` rain,
    except hours listed in `overrides`: {"2026-09-30T15:00": {"prob": 80}}.
    `codes` optionally gives today's hourly weather codes: {hour: code}.
    """
    overrides = overrides or {}
    days = ("2026-09-30", "2026-10-01", "2026-10-02")
    specs = []  # (timestamp, values) for each hour, keyed by the hour's start
    for day in days:
        for hr in range(24):
            ts = f"{day}T{hr:02d}:00"
            is_day = int(sunrise[:2]) <= hr < int(sunset[:2])
            h = {"et0_fao_evapotranspiration": rate if is_day else 0.0,
                 "precipitation_probability": prob, "precipitation": mm,
                 "is_day": int(is_day), "relative_humidity_2m": 60, "wind_speed_10m": 8,
                 "weather_code": (codes or {}).get(hr, 2) if day == "2026-09-30" else 2,
                 "temperature_2m": 16.0 if is_day else 11.0}
            h.update({{"rate": "et0_fao_evapotranspiration", "prob": "precipitation_probability",
                       "mm": "precipitation", "temp": "temperature_2m"}.get(k, k): v
                      for k, v in overrides.get(ts, {}).items()})
            specs.append((ts, h))

    # Like Open-Meteo: instant values sit at their own timestamp, but "preceding
    # hour" values (ET0, rain) for the hour starting at T sit at T + 1h.
    period = ("et0_fao_evapotranspiration", "precipitation_probability", "precipitation")
    hourly = {k: [] for k in ("time",) + tuple(specs[0][1])}
    for k, (ts, h) in enumerate(specs):
        before = specs[k - 1][1] if k else h
        hourly["time"].append(ts)
        for name, v in h.items():
            hourly[name].append(before[name] if name in period else v)
    return {
        "current": {"time": now, "temperature_2m": 18.2, "apparent_temperature": 16.6,
                    "relative_humidity_2m": 65, "weather_code": 61 if raining_now else 2,
                    "is_day": 1, "precipitation": 0.4 if raining_now else 0.0,
                    "wind_speed_10m": 9.0, "wind_gusts_10m": 20.0, "wind_direction_10m": 225},
        "hourly": hourly,
        "daily": {"time": list(days), "weather_code": [2, 3, 3],
                  "temperature_2m_max": [19.4, 17.6, 17.0], "temperature_2m_min": [10.5, 9.4, 9.0],
                  "sunrise": [f"{d}T{sunrise}" for d in days],
                  "sunset": [f"{d}T{sunset}" for d in days],
                  "precipitation_probability_max": [prob] * 3},
    }


def wash(data):
    return transform.run(data)["wash"]


class Verdicts(unittest.TestCase):
    def test_good_day_says_go(self):
        w = wash(forecast(rate=0.3))
        self.assertEqual(w["code"], "go")
        self.assertEqual(w["out_at"], "Now")
        # normal = 1.3 mm at 0.3 mm/h = 4h20m
        self.assertEqual(w["dry_by"], "14:20")
        self.assertEqual(w["in_by"], "19:00")
        self.assertEqual(w["in_why"], "sunset")

    def test_dry_times_for_every_load(self):
        # 0.9, 1.3 and 1.9 mm at 0.3 mm/h from 10:00.
        loads = {l["name"]: l["dry_by"] for l in wash(forecast(rate=0.3))["loads"]}
        self.assertEqual(loads, {"Light": "13:00", "Normal": "14:20", "Heavy": "16:20"})

    def test_heavy_load_that_wont_dry(self):
        # Rain at 15:00: normal dries at 14:20, heavy never does.
        w = wash(forecast(rate=0.3, overrides={"2026-09-30T15:00": {"prob": 80}}))
        loads = {l["name"]: l["dry_by"] for l in w["loads"]}
        self.assertEqual(loads["Normal"], "14:20")
        self.assertIsNone(loads["Heavy"])

    def test_loads_start_when_the_verdict_says(self):
        rain = {f"2026-09-30T{h}:00": {"prob": 80} for h in ("11", "12")}
        loads = {l["name"]: l["dry_by"] for l in wash(forecast(rate=0.3, overrides=rain))["loads"]}
        self.assertEqual(loads["Light"], "16:00")  # out 13:00 + 3h

    def test_rain_before_dry_means_wait(self):
        rain = {f"2026-09-30T{h}:00": {"prob": 80} for h in ("11", "12")}
        w = wash(forecast(rate=0.3, overrides=rain))
        self.assertEqual(w["code"], "wait")
        self.assertEqual(w["out_at"], "13:00")

    def test_rain_just_after_dry_is_risky(self):
        # Dry at 14:20, rain at 15:00: under an hour's margin.
        w = wash(forecast(rate=0.3, overrides={"2026-09-30T15:00": {"prob": 80}}))
        self.assertEqual(w["code"], "risky")
        self.assertEqual(w["in_by"], "15:00")
        self.assertEqual(w["in_why"], "rain")

    def test_moderate_rain_chance_is_risky(self):
        w = wash(forecast(rate=0.3, prob=30))
        self.assertEqual(w["code"], "risky")

    def test_raining_now_waits_for_it_to_stop(self):
        data = forecast(rate=0.3, raining_now=True,
                        overrides={"2026-09-30T10:00": {"mm": 1.0}})
        w = wash(data)
        self.assertEqual(w["code"], "wait")
        self.assertEqual(w["headline"], "Raining now")
        self.assertEqual(w["out_at"], "11:00")

    def test_poor_drying_all_day_partly_dry(self):
        w = wash(forecast(rate=0.09))  # 9h * 0.09 = 0.81 of 1.3 = 62%
        self.assertEqual(w["code"], "risky")
        self.assertIsNone(w["dry_by"])
        self.assertIn("62%", w["detail"])

    def test_too_late_today_points_to_tomorrow(self):
        w = wash(forecast(now="2026-09-30T16:30", rate=0.3))
        self.assertEqual(w["code"], "tomorrow")
        self.assertEqual(w["out_at"], "07:00")

    def test_after_sunset_points_to_tomorrow(self):
        data = forecast(now="2026-09-30T21:00", rate=0.3)
        out = transform.run(data)
        self.assertEqual(out["wash"]["code"], "tomorrow")
        self.assertEqual(out["chart"]["day"], "Tomorrow")
        self.assertIsNone(out["chart"]["now"])

    def test_before_sunrise_says_out_at_sunrise(self):
        w = wash(forecast(now="2026-09-30T06:15", rate=0.3))
        self.assertEqual(w["code"], "go")
        self.assertEqual(w["out_at"], "07:00")
        self.assertEqual(w["headline"], "Put it out at 07:00")

    def test_warns_about_rain_overnight(self):
        w = wash(forecast(rate=0.3, overrides={"2026-09-30T22:00": {"prob": 70}}))
        self.assertEqual(w["tonight"], "Rain from 22:00 tonight, don't leave it out")
        self.assertIsNone(wash(forecast(rate=0.3))["tonight"])

    def test_no_tonight_warning_for_a_tomorrow_window(self):
        data = forecast(now="2026-09-30T21:00", rate=0.3,
                        overrides={"2026-10-01T22:00": {"prob": 70}})
        self.assertIsNone(wash(data)["tonight"])

    def test_rain_all_week_says_indoors(self):
        w = wash(forecast(rate=0.3, prob=90))
        self.assertEqual(w["code"], "no")
        self.assertIsNone(w["out_at"])


class Explanations(unittest.TestCase):
    def test_go_says_how_long(self):
        self.assertEqual(wash(forecast(rate=0.3))["detail"], "Dry in about 4½ hours")

    def test_wait_explains_the_rain(self):
        rain = {f"2026-09-30T{h}:00": {"prob": 80} for h in ("11", "12")}
        self.assertEqual(wash(forecast(rate=0.3, overrides=rain))["detail"],
                         "Rain 11:00–13:00, then dry")

    def test_wait_when_rain_has_started(self):
        rain = {f"2026-09-30T{h}:00": {"prob": 80} for h in ("10", "11")}
        self.assertEqual(wash(forecast(rate=0.3, overrides=rain))["detail"],
                         "Rain likely until 12:00")

    def test_raining_now_says_when_it_clears(self):
        data = forecast(rate=0.3, raining_now=True, overrides={"2026-09-30T10:00": {"mm": 1.0}})
        self.assertEqual(wash(data)["detail"], "Should clear by 11:00")

    def test_risky_names_the_risk(self):
        w = wash(forecast(rate=0.3, overrides={"2026-09-30T15:00": {"prob": 80}}))
        self.assertEqual(w["detail"], "Rain due 15:00, cutting it fine")
        self.assertEqual(wash(forecast(rate=0.3, prob=30))["detail"], "30% chance of a shower")

    def test_tomorrow_says_why_not_today(self):
        w = wash(forecast(now="2026-09-30T16:30", rate=0.3))
        self.assertEqual(w["detail"], "Not enough drying time left today")
        self.assertEqual(w["when"], "tomorrow")
        rain = {f"2026-09-30T{h}:00": {"prob": 80} for h in range(14, 19)}
        w = wash(forecast(now="2026-09-30T12:00", rate=0.3, overrides=rain))
        self.assertEqual(w["detail"], "Rain from 14:00")

    def test_duration_wording(self):
        from datetime import timedelta as td
        self.assertEqual(transform.duration(td(minutes=40)), "under an hour")
        self.assertEqual(transform.duration(td(minutes=65)), "about 1 hour")
        self.assertEqual(transform.duration(td(hours=2, minutes=20)), "about 2½ hours")


class DaySummary(unittest.TestCase):
    def today(self, codes):
        return transform.run(forecast(codes=codes))["today"]

    def test_uses_daylight_not_overnight_weather(self):
        # Drizzle overnight only: the daily code would say drizzle.
        codes = {h: 51 for h in range(0, 7)} | {h: 0 for h in range(7, 19)}
        self.assertEqual(self.today(codes)["text"], "Sunny")

    def test_sky_that_changes_at_midday(self):
        codes = {h: 3 for h in range(7, 12)} | {h: 0 for h in range(12, 19)}
        self.assertEqual(self.today(codes)["text"], "Overcast, then sunny")

    def test_afternoon_showers(self):
        codes = {h: 2 for h in range(7, 15)} | {h: 80 for h in range(15, 19)}
        t = self.today(codes)
        self.assertEqual(t["text"], "Partly cloudy, then showers")
        self.assertEqual(t["icon"], "wi-day-showers")

    def test_a_single_shower_hour_is_ignored(self):
        codes = {h: 1 for h in range(7, 19)} | {13: 80}
        self.assertEqual(self.today(codes)["text"], "Mostly sunny")

    def test_wet_all_day(self):
        self.assertEqual(self.today({h: 63 for h in range(7, 19)})["text"], "Rain")

    def test_rain_chance_is_daytime_only(self):
        rain = {"2026-09-30T03:00": {"prob": 90}, "2026-09-30T13:00": {"prob": 30}}
        self.assertEqual(transform.run(forecast(overrides=rain))["today"]["rain"], 30)


class NightLows(unittest.TestCase):
    def test_low_is_the_coming_night_not_the_calendar_day(self):
        temps = {"2026-09-30T04:00": {"temp": 6.0},     # last night: ignored
                 "2026-10-01T05:00": {"temp": 8.4},     # tonight
                 "2026-10-02T06:00": {"temp": 7.2}}     # tomorrow night
        out = transform.run(forecast(overrides=temps))
        self.assertEqual(out["today"]["lo"], 8)
        self.assertEqual(out["tomorrow"]["lo"], 7)

    def test_after_sunset_counts_from_now(self):
        temps = {"2026-09-30T19:00": {"temp": 5.0}, "2026-10-01T05:00": {"temp": 9.0}}
        out = transform.run(forecast(now="2026-09-30T21:00", overrides=temps))
        self.assertEqual(out["today"]["lo"], 9)

    def test_falls_back_to_daily_min_without_the_next_sunrise(self):
        # The fixture has two days, so tomorrow night can't be bounded.
        out = transform.run(json.loads((FIXTURES / "sunny_autumn.json").read_text()))
        self.assertEqual(out["today"]["lo"], 13)     # tonight, from hourly temps
        self.assertEqual(out["tomorrow"]["lo"], 13)  # daily min for 1 Oct (13.1)


# The house to the north-west of the line, 4 m away (a south-east facing
# garden), plus a 6 ft fence 2 m to the south-west.
HOUSE_NW = {"obstruction_1_type": "house3", "obstruction_1_direction": "nw",
            "obstruction_1_distance": "4"}
WITH_FENCE = dict(HOUSE_NW, obstruction_2_type="fence", obstruction_2_direction="sw",
                  obstruction_2_distance="2")


def with_garden(data, fields):
    data["trmnl"] = {"plugin_settings": {"custom_fields_values": fields}}
    return data


def reading():
    return json.loads((FIXTURES / "reading_full.json").read_text())


def garden(fields):
    return transform.parse_garden(fields)


class Garden(unittest.TestCase):
    def test_settings_accept_values_or_labels(self):
        by_value = garden(HOUSE_NW)
        by_label = garden({"obstruction_1_type": "House, 3 storeys (~11 m to ridge)",
                           "obstruction_1_direction": "North-west",
                           "obstruction_1_distance": "4 m"})
        self.assertEqual(by_value, by_label)
        ob = by_value["obstructions"][0]
        self.assertEqual((ob["azimuth"], ob["name"], ob["distance"]), (315, "house", 4))

    def test_every_type_label_is_recognised(self):
        labels = {"Fence or wall (~1.8 m)": "fence", "Tall hedge (~3 m)": "hedge",
                  "Shed or garage (~2.5 m)": "shed", "Bungalow, 1 storey (~5.5 m to ridge)": "bungalow",
                  "House, 2 storeys (~8.5 m to ridge)": "house2",
                  "House, 3 storeys (~11 m to ridge)": "house3", "Tree (~10 m)": "tree", "None": None}
        for label, key in labels.items():
            self.assertEqual(transform.obstruction_type(label), key, label)

    def test_older_single_house_settings_still_work(self):
        old = garden({"garden_faces": "se", "house_height": "3", "line_distance": "4"})
        self.assertEqual(old, garden(HOUSE_NW))

    def test_nothing_set_means_no_adjustment(self):
        self.assertIsNone(garden({}))
        self.assertIsNone(garden({"obstruction_1_type": "none", "obstruction_1_direction": "n"}))
        self.assertEqual(garden({"shelter": "some"}), {"obstructions": [], "exposure": 0.85})

    def test_sun_position_at_solar_noon(self):
        # Alton, 30 Sep: sun due south at about 36 degrees, near 12:55 BST.
        elev, az = transform.sun_position(datetime(2026, 9, 30, 12, 55), 3600, 51.15, -0.97)
        self.assertAlmostEqual(elev, 36.4, delta=0.5)
        self.assertAlmostEqual(az, 180, delta=2)

    def test_house_horizon(self):
        house = garden(HOUSE_NW)["obstructions"][0]
        # Straight at it, the eaves (7.8 m, 4 m away) top the ridge (11 m,
        # 8 m away): atan((7.8 - 1.3) / 4) ~ 58 deg.
        self.assertAlmostEqual(transform.horizon(house, 315), 58.4, delta=0.5)
        self.assertEqual(transform.horizon(house, 135), 0)          # behind the line
        self.assertAlmostEqual(transform.horizon(house, 250), 34.5, delta=0.5)  # obliquely: lower

    def test_narrow_things_only_block_nearby_directions(self):
        shed = garden({"obstruction_1_type": "shed", "obstruction_1_direction": "w",
                       "obstruction_1_distance": "2"})["obstructions"][0]
        self.assertGreater(transform.horizon(shed, 270), 0)
        self.assertEqual(transform.horizon(shed, 225), 0)           # past its side

    def test_shading(self):
        g = garden(WITH_FENCE)
        self.assertEqual(transform.shading(30, 180, g), (0.0, None))    # sun to the south
        self.assertEqual(transform.shading(20, 300, g), (1.0, "house"))
        # 1.8 m fence 2 m away: blocks the sky up to atan(0.5 / 2) = 14 deg.
        self.assertEqual(transform.shading(10, 225, g), (1.0, "fence"))
        self.assertEqual(transform.shading(15, 225, g), (0.0, None))
        tree = garden({"obstruction_1_type": "tree", "obstruction_1_direction": "s",
                       "obstruction_1_distance": "6"})
        self.assertEqual(transform.shading(30, 180, tree), (0.7, "tree"))

    def test_wind_over_obstructions_is_cut(self):
        g = garden(HOUSE_NW)
        self.assertAlmostEqual(transform.wind_factor(315, g), 0.4, delta=0.01)   # NW: over the house
        self.assertEqual(transform.wind_factor(135, g), 1.0)                    # SE: open side
        both = garden(WITH_FENCE)
        self.assertLess(transform.wind_factor(225, both), 0.6)                  # SW: over the fence
        sheltered = garden(dict(HOUSE_NW, shelter="sheltered"))
        self.assertAlmostEqual(transform.wind_factor(135, sheltered), 0.7)

    def test_own_et0_matches_open_meteo(self):
        data = reading()
        hours = transform.build_hours(data)
        ours = sum(h["rate"] for h in hours if h["day"])
        theirs = sum(e for e, d in zip(data["hourly"]["et0_fao_evapotranspiration"], hours) if d["day"])
        self.assertAlmostEqual(ours / theirs, 1.0, delta=0.03)

    def test_values_describe_the_preceding_hour(self):
        data = reading()
        hours = transform.build_hours(data)
        i = data["hourly"]["time"].index("2026-09-30T13:00")
        self.assertEqual(hours[i]["start"], datetime(2026, 9, 30, 12, 0))
        self.assertEqual(hours[i]["prob"], data["hourly"]["precipitation_probability"][i])

    def test_shade_and_shelter_slow_drying(self):
        data = reading()
        data["hourly"]["wind_direction_10m"] = [315] * len(data["hourly"]["time"])
        open_rate = sum(h["rate"] for h in transform.build_hours(data) if h["day"])
        garden_rate = sum(h["rate"] for h in transform.build_hours(data, garden(WITH_FENCE)) if h["day"])
        self.assertLess(garden_rate, open_rate * 0.95)

    def test_verdict_names_what_shades_it_and_title_shows_garden(self):
        out = transform.run(with_garden(reading(), WITH_FENCE))
        self.assertRegex(out["wash"]["why"], r"house shade from 1[56]:\d\d")
        self.assertEqual(out["garden"], "NW house 4 m, SW fence 2 m")
        self.assertIsNone(transform.run(reading())["garden"])


class Mowing(unittest.TestCase):
    def mow(self, **kw):
        return transform.run(forecast(**kw))

    def test_dry_day_mows_from_now_until_an_hour_before_sunset(self):
        out = self.mow(rate=0.3)
        self.assertEqual(out["today"]["mow"], "Mow 10:00–18:00")
        self.assertEqual(out["tomorrow"]["mow"], "Mow 07:00–18:00")

    def test_dew_after_a_humid_night(self):
        night = {f"2026-10-01T{h:02d}:00": {"relative_humidity_2m": 95} for h in range(0, 7)}
        # 0.3 mm of dew at 0.3 mm/h: dry after the first hour of daylight.
        self.assertEqual(self.mow(rate=0.3, overrides=night)["tomorrow"]["mow"], "Mow 08:00–18:00")

    def test_heavy_morning_rain_delays_mowing(self):
        rain = {f"2026-09-30T{h}:00": {"mm": 5.0} for h in ("09", "10")}
        # 0.4 + 0.15 * 5 = 1.15 mm to dry at 0.3 mm/h: dry after 4 hours.
        self.assertEqual(self.mow(rate=0.3, overrides=rain)["today"]["mow"], "Mow 15:00–18:00")

    def test_wet_all_day(self):
        self.assertEqual(self.mow(rate=0.3, prob=80)["today"]["mow"], "Too wet to mow")

    def test_short_gap_isnt_a_window(self):
        rain = {f"2026-09-30T{h}:00": {"mm": 1.0} for h in ("11", "14", "17")}
        self.assertEqual(self.mow(rate=0.3, overrides=rain)["today"]["mow"], "Too wet to mow")

    def test_too_late_today(self):
        self.assertEqual(self.mow(now="2026-09-30T18:30", rate=0.3)["today"]["mow"], "Too late to mow")

    def test_window_starts_on_a_quarter_hour(self):
        self.assertEqual(self.mow(now="2026-09-30T13:05", rate=0.3)["today"]["mow"], "Mow 13:15–18:00")


class PastDay(unittest.TestCase):
    def test_yesterday_in_the_daily_data_is_skipped(self):
        data = forecast()
        daily = data["daily"]
        for key in daily:
            daily[key].insert(0, "2026-09-29" if key == "time" else
                              "2026-09-29T07:00" if key == "sunrise" else
                              "2026-09-29T19:00" if key == "sunset" else 99)
        out = transform.run(data)
        self.assertEqual((out["today"]["name"], out["today"]["hi"]), ("Today", 19))
        self.assertEqual(out["tomorrow"]["hi"], 18)


class Output(unittest.TestCase):
    def test_real_forecast_is_json_serialisable(self):
        data = json.loads((FIXTURES / "sunny_autumn.json").read_text())
        out = transform.run(data)
        json.dumps(out)
        self.assertEqual(out["now"]["wind_dir"], "SW")
        self.assertEqual(out["today"]["sunrise"], "07:02")
        self.assertEqual(out["tomorrow"]["name"], "Tomorrow")
        # The daily code says drizzle; the daylight hours were dry.
        self.assertEqual(out["today"]["text"], "Overcast, then sunny")

    def test_timeline_marks_the_drying_window(self):
        out = transform.run(forecast(rate=0.3))
        win = [b["label"] for b in out["chart"]["bars"] if b["win"]]
        self.assertEqual(win, ["10", "11", "12", "13", "14"])
        self.assertEqual(out["chart"]["now"], "10")
        past = [b["label"] for b in out["chart"]["bars"] if b["past"]]
        self.assertEqual(past, ["07", "08", "09"])

    def test_api_error_is_passed_through(self):
        out = transform.run({"error": True, "reason": "Latitude must be in range"})
        self.assertEqual(out["error"], "Latitude must be in range")


if __name__ == "__main__":
    unittest.main()
