"""Tests for the washing engine. Run: python3 -m unittest discover tests"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import transform  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def forecast(now="2026-09-30T10:00", rate=0.3, prob=0, mm=0.0, overrides=None,
             sunrise="07:00", sunset="19:00", raining_now=False):
    """A synthetic two-day Open-Meteo response.

    Every daylight hour gets `rate` ET0, `prob` rain chance and `mm` rain,
    except hours listed in `overrides`: {"2026-09-30T15:00": {"prob": 80}}.
    """
    overrides = overrides or {}
    hourly = {k: [] for k in ("time", "et0_fao_evapotranspiration", "precipitation_probability",
                              "precipitation", "is_day", "relative_humidity_2m", "wind_speed_10m")}
    for day in ("2026-09-30", "2026-10-01"):
        for hr in range(24):
            ts = f"{day}T{hr:02d}:00"
            is_day = int(sunrise[:2]) <= hr < int(sunset[:2])
            h = {"et0_fao_evapotranspiration": rate if is_day else 0.0,
                 "precipitation_probability": prob, "precipitation": mm,
                 "is_day": int(is_day), "relative_humidity_2m": 60, "wind_speed_10m": 8}
            h.update({{"rate": "et0_fao_evapotranspiration", "prob": "precipitation_probability",
                       "mm": "precipitation"}.get(k, k): v
                      for k, v in overrides.get(ts, {}).items()})
            hourly["time"].append(ts)
            for k, v in h.items():
                hourly[k].append(v)
    return {
        "current": {"time": now, "temperature_2m": 18.2, "apparent_temperature": 16.6,
                    "relative_humidity_2m": 65, "weather_code": 61 if raining_now else 2,
                    "is_day": 1, "precipitation": 0.4 if raining_now else 0.0,
                    "wind_speed_10m": 9.0, "wind_gusts_10m": 20.0, "wind_direction_10m": 225},
        "hourly": hourly,
        "daily": {"time": ["2026-09-30", "2026-10-01"], "weather_code": [2, 3],
                  "temperature_2m_max": [19.4, 17.6], "temperature_2m_min": [10.5, 9.4],
                  "sunrise": [f"2026-09-30T{sunrise}", f"2026-10-01T{sunrise}"],
                  "sunset": [f"2026-09-30T{sunset}", f"2026-10-01T{sunset}"],
                  "precipitation_probability_max": [prob, prob]},
    }


def wash(data, load="normal"):
    data["trmnl"] = {"plugin_settings": {"custom_fields_values": {"load": load}}}
    return transform.run(data)["wash"]


class Verdicts(unittest.TestCase):
    def test_good_day_says_go(self):
        w = wash(forecast(rate=0.3))
        self.assertEqual(w["code"], "go")
        self.assertEqual(w["out_at"], "10:00")
        # normal = 1.3 mm at 0.3 mm/h = 4h20m
        self.assertEqual(w["dry_by"], "14:20")
        self.assertEqual(w["in_by"], "19:00")
        self.assertEqual(w["in_why"], "sunset")

    def test_heavier_loads_take_longer(self):
        light = wash(forecast(rate=0.3), "light")["dry_by"]
        heavy = wash(forecast(rate=0.3), "heavy")["dry_by"]
        self.assertLess(light, heavy)

    def test_load_label_from_trmnl_select_is_normalised(self):
        data = forecast(rate=0.3)
        data["trmnl"] = {"plugin_settings": {"custom_fields_values": {"load": "Heavy"}}}
        self.assertEqual(transform.run(data)["load"], "heavy")

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

    def test_rain_all_week_says_indoors(self):
        w = wash(forecast(rate=0.3, prob=90))
        self.assertEqual(w["code"], "no")
        self.assertIsNone(w["out_at"])


class Output(unittest.TestCase):
    def test_real_forecast_is_json_serialisable(self):
        data = json.loads((FIXTURES / "sunny_autumn.json").read_text())
        out = transform.run(data)
        json.dumps(out)
        self.assertEqual(out["now"]["wind_dir"], "SW")
        self.assertEqual(out["today"]["sunrise"], "07:02")
        self.assertEqual(out["tomorrow"]["name"], "Tomorrow")

    def test_timeline_marks_the_drying_window(self):
        out = transform.run(forecast(rate=0.3))
        win = [b["label"] for b in out["chart"]["bars"] if b["win"]]
        self.assertEqual(win, ["10", "11", "12", "13", "14"])
        self.assertEqual(out["chart"]["now"], "10")

    def test_api_error_is_passed_through(self):
        out = transform.run({"error": True, "reason": "Latitude must be in range"})
        self.assertEqual(out["error"], "Latitude must be in range")


if __name__ == "__main__":
    unittest.main()
