"""Unit tests for the VictoriaMetrics queries - run without Home Assistant.

    python -m unittest discover -s tests -v
"""

import importlib.util
import json
import pathlib
import sys
import types
import unittest

# vm.py imports .query: load both by path as a package that does not pull in Home Assistant.
_DIR = pathlib.Path(__file__).parents[1] / "custom_components" / "influx_proxy"
_pkg = types.ModuleType("influx_proxy_vm_pkg")
_pkg.__path__ = [str(_DIR)]
sys.modules[_pkg.__name__] = _pkg
for _name in ("query", "vm"):
    _spec = importlib.util.spec_from_file_location(f"{_pkg.__name__}.{_name}", _DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _mod
    _spec.loader.exec_module(_mod)
vm = sys.modules[f"{_pkg.__name__}.vm"]


class Selector(unittest.TestCase):
    def test_labels_and_db(self):
        self.assertEqual(
            vm.selector("°C_value", "sensor.living_room", "homeassistant"),
            '{__name__="°C_value",domain="sensor",entity_id="living_room",db="homeassistant"}',
        )
        self.assertNotIn("db=", vm.selector("x_value", "light.a"))

    def test_quotes_stay_inside_the_label(self):
        sel = vm.selector('m"} or vector(1) #_value', "light.a")
        self.assertIn('__name__="m\\"} or vector(1) #_value"', sel)


class Series(unittest.TestCase):
    def test_params(self):
        p = vm.series_params("sensor.a", "°C", 1, (1790000000000, 1790086400000), 0, "ha")
        self.assertEqual(p["step"], "5m")
        self.assertEqual((p["start"], p["end"]), ("1790000300", "1790086700"))
        for fn, agg in (("avg_over_time", "mean"), ("min_over_time", "min"), ("max_over_time", "max")):
            self.assertIn(f'label_set({fn}({{__name__="°C_value",domain="sensor",entity_id="a",db="ha"}}[5m]), "agg", "{agg}")', p["query"])

    def test_last_days_from_now(self):
        p = vm.series_params("sensor.a", "°C", 30, None, 1790000000000)
        self.assertEqual(p["step"], "2h")
        self.assertEqual(int(p["end"]) - int(p["start"]), 30 * 86400)

    def test_parse_labels_points_with_their_start(self):
        payload = {"status": "success", "data": {"result": [
            {"metric": {"agg": "mean"}, "values": [[1790000300, "21.5"], [1790000600, "22"]]},
            {"metric": {"agg": "min"}, "values": [[1790000300, "21"], [1790000600, "22"]]},
            {"metric": {"agg": "max"}, "values": [[1790000300, "22"]]},
        ]}}
        # a point for the step before the range is dropped
        self.assertEqual(len(vm.parse_series(payload, "5m", 1790000000000 + 300_000)), 0)
        # the second point lacks max: left out rather than half a point
        self.assertEqual(vm.parse_series(payload, "5m"), [
            {"start": 1790000000000, "end": 1790000000000, "mean": 21.5, "min": 21.0, "max": 22.0},
        ])


class States(unittest.TestCase):
    def test_export_params(self):
        p = vm.export_params("cover.k", "state", ("current_position",), 1790000000000, 1790086400000, "ha")
        self.assertEqual(p, [
            ("match[]", '{__name__="state_value",domain="cover",entity_id="k",db="ha"}'),
            ("match[]", '{__name__="state_current_position",domain="cover",entity_id="k",db="ha"}'),
            ("match[]", '{__name__="ha_state_code",domain="cover",entity_id="k",db="ha"}'),
            ("start", "1790000000.000"),
            ("end", "1790086400.000"),
        ])
        self.assertNotIn("start", dict(vm.export_params("light.a", "state", (), None, 1790086400000)))

    def test_state_text_by_domain(self):
        cases = [
            ("light.a", 1, "on"), ("light.a", 0, "off"), ("binary_sensor.a", 1, "on"),
            ("cover.a", 1, "open"), ("cover.a", 0, "closed"), ("lock.a", 1, "locked"),
            ("person.a", 0, "not_home"), ("sun.sun", 1, "above_horizon"),
            ("sensor.a", 1, 1), ("sensor.a", 21.5, 21.5), ("climate.a", 21.5, 21.5),
            ("select.mode", 1, 1), ("switch.a", 0, "off"), ("update.a", 1, "on"),
        ]
        for entity_id, value, want in cases:
            self.assertEqual(vm.state_text(entity_id, float(value)), want, (entity_id, value))

    def test_parse_export_matches_attributes_by_time(self):
        body = "\n".join(json.dumps(x) for x in (
            {"metric": {"__name__": "cover.k_value"}, "values": [1, 0], "timestamps": [200, 100]},
            {"metric": {"__name__": "cover.k_current_position"}, "values": [100], "timestamps": [200]},
            {"metric": {"__name__": "other_value"}, "values": [5], "timestamps": [150]},
        )).encode()
        self.assertEqual(vm.parse_export(body, "cover.k", "cover.k", ("current_position",)),
                         [[100, "closed", None], [200, "open", 100]])

    def test_codes_win_over_value_and_keep_attributes(self):
        body = "\n".join(json.dumps(x) for x in (
            {"metric": {"__name__": "media_player.tv_value"}, "values": [1], "timestamps": [100]},
            {"metric": {"__name__": "media_player.tv_volume_level"}, "values": [0.3, 0.4], "timestamps": [100, 200]},
            {"metric": {"__name__": "ha_state_code"}, "values": [1, 2, 9], "timestamps": [100, 200, 300]},
        )).encode()
        rows = vm.parse_export(body, "media_player.tv", "media_player.tv", ("volume_level",), {1: "on", 2: "playing"})
        # code 9 is not in the dictionary: no row rather than a wrong one
        self.assertEqual(rows, [[100, "on", 0.3], [200, "playing", 0.4]])

    def test_rows_before_and_limit(self):
        rows = [[50, "on"], [90, "off"], [120, "on"], [130, "off"]]
        self.assertEqual(vm.states_rows(rows, rows, 100, 10), [[90, "off"], [120, "on"], [130, "off"]])
        # the limit cut the range: newest kept, no row before
        self.assertEqual(vm.states_rows(rows, rows, 100, 1), [[130, "off"]])
        self.assertEqual(vm.states_rows([], [], 100, 10), [])



class Dictionary(unittest.TestCase):
    def test_lines_escape_labels(self):
        self.assertEqual(vm.code_line("media_player.tv", 7, 1790000000123, "ha"),
                         'ha_state_code{domain="media_player",entity_id="tv",db="ha"} 7 1790000000123')
        self.assertEqual(vm.label_line('a "b"\\c\nd', 3, 5), 'ha_state_label{label="a \\"b\\"\\\\c\\nd",code="3"} 3 5')

    def test_series_and_parse(self):
        self.assertEqual(vm.dictionary_params("ha"), [("match[]", 'ha_state_label{db="ha"}'), ("start", "0")])
        payload = {"status": "success", "data": [
            {"__name__": "ha_state_label", "label": "playing", "code": "2", "db": "ha"},
            {"__name__": "ha_state_label", "label": "idle", "code": "1", "db": "ha"},
            {"__name__": "ha_state_label", "label": "broken"},
        ]}
        dictionary = vm.parse_dictionary(payload)
        self.assertEqual(dictionary.codes, {"playing": 2, "idle": 1})
        self.assertEqual(dictionary.labels, {1: "idle", 2: "playing"})
        self.assertEqual(dictionary.ambiguous, {})

    def test_two_codes_for_a_label_and_two_labels_for_a_code(self):
        payload = {"status": "success", "data": [
            {"label": "heat", "code": "5"}, {"label": "heat", "code": "160"},  # harmless: both read as heat
            {"label": "on", "code": "7"}, {"label": "off", "code": "7"},  # two writers: 7 is unusable
        ]}
        dictionary = vm.parse_dictionary(payload)
        self.assertEqual(dictionary.labels, {5: "heat", 160: "heat"})
        self.assertEqual(dictionary.codes["heat"], 5)
        self.assertEqual(dictionary.ambiguous, {7: ["off", "on"]})
        self.assertNotIn(7, dictionary.labels)
        # the shared code stays taken for both labels, never handed out again
        self.assertEqual((dictionary.codes["on"], dictionary.codes["off"]), (7, 7))

    def test_last_before(self):
        p = vm.last_before_params("cover.k", "state", 1790000000123, "ha")
        self.assertEqual(p["time"], "1790000000.123")
        self.assertIn('tlast_over_time({__name__="state_value",domain="cover",entity_id="k",db="ha"}[36500d])', p["query"])
        self.assertIn('tlast_over_time({__name__="ha_state_code",domain="cover",entity_id="k",db="ha"}[36500d])', p["query"])
        self.assertIn('"f", "value"', p["query"]); self.assertIn('"f", "code"', p["query"])
        self.assertEqual(vm.parse_last_before({"data": {"result": [{"value": [1790000000, "1789999999.457"]}]}}), 1789999999457)
        self.assertIsNone(vm.parse_last_before({"data": {"result": []}}))

    def test_state_labels(self):
        for state in ["playing", "bawełna 60", 'pranie "extra"', "heat_cool", "x" * 100]:
            self.assertTrue(vm.is_state_label(state), state)
        for state in ["", "x" * 101, "2026-09-29T21:30:34.703485+00:00", "2026-10-21", "2026-10-21 07:15", "07:15", "7:15:02"]:
            self.assertFalse(vm.is_state_label(state), state)


if __name__ == "__main__":
    unittest.main()
