"""Unit tests for query building - run without Home Assistant.

    python -m unittest discover -s tests -v
"""

import importlib.util
import pathlib
import sys
import unittest

# Load query.py by path: importing the package would pull in Home Assistant.
_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "influx_proxy" / "query.py"
_spec = importlib.util.spec_from_file_location("influx_proxy_query", _PATH)
q = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = q  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(q)


class Quoting(unittest.TestCase):
    def test_identifier_escapes_quotes_and_backslashes(self):
        self.assertEqual(q.quote_identifier('°C'), '"°C"')
        self.assertEqual(q.quote_identifier('a"b'), '"a\\"b"')
        self.assertEqual(q.quote_identifier('a\\'), '"a\\\\"')

    def test_string_escapes_quotes_and_backslashes(self):
        self.assertEqual(q.quote_string("x"), "'x'")
        self.assertEqual(q.quote_string("it's"), "'it\\'s'")
        self.assertEqual(q.quote_string("a\\"), "'a\\\\'")

    def test_injection_attempt_stays_inside_the_identifier(self):
        query = q.series_query("sensor.t", 'x" ; DROP DATABASE homeassistant ; "', 1)
        self.assertIn('FROM "x\\" ; DROP DATABASE homeassistant ; \\""', query)


class EntityIds(unittest.TestCase):
    def test_valid(self):
        for e in ["sensor.salon_temperature", "binary_sensor.door", "light.l1"]:
            self.assertTrue(q.valid_entity_id(e), e)

    def test_invalid(self):
        for e in ["sensor", "sensor.", ".x", "Sensor.x", "sensor.x'y", "sensor.a b",
                  "sensor._x", "sensor.x_", "sen__sor.x", "sensor.x;DROP", "sensor.x\n", "sensor.x\x00"]:
            self.assertFalse(q.valid_entity_id(e), e)


class Measurement(unittest.TestCase):
    N = q.MeasurementNaming

    def test_unit_mode_is_home_assistant_default(self):
        self.assertEqual(q.measurement_for("sensor.t", "°C", "temperature", self.N()), "°C")

    def test_no_unit_falls_back_to_entity_id_like_home_assistant(self):
        self.assertEqual(q.measurement_for("sensor.s", None, None, self.N()), "sensor.s")

    def test_default_measurement_when_attribute_empty(self):
        n = self.N(default_measurement="state")
        self.assertEqual(q.measurement_for("sensor.s", "", None, n), "state")
        self.assertEqual(q.measurement_for("sensor.t", "W", None, n), "W")

    def test_domain_device_class(self):
        n = self.N(mode=q.MEASUREMENT_DOMAIN_DEVICE_CLASS)
        self.assertEqual(q.measurement_for("sensor.t", "°C", "temperature", n), "sensor__temperature")
        # like HA: no device class -> the domain alone, not the entity_id
        self.assertEqual(q.measurement_for("sensor.t", "°C", None, n), "sensor")
        self.assertEqual(q.measurement_for("binary_sensor.d", None, None, n), "binary_sensor")

    def test_mode_value_passes_hassfest_key_rule(self):
        import re
        rule = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
        for mode in q.MEASUREMENT_MODES:
            self.assertRegex(mode, rule)

    def test_entity_id_mode(self):
        n = self.N(mode=q.MEASUREMENT_ENTITY_ID)
        self.assertEqual(q.measurement_for("sensor.t", "°C", None, n), "sensor.t")

    def test_override_wins(self):
        n = self.N(override_measurement="ha", default_measurement="state")
        self.assertEqual(q.measurement_for("sensor.t", "°C", None, n), "ha")


class SeriesQuery(unittest.TestCase):
    def test_filters_on_domain_and_object_id(self):
        query = q.series_query("binary_sensor.door", "state", 7)
        self.assertIn('"domain" = \'binary_sensor\'', query)
        self.assertIn('"entity_id" = \'door\'', query)

    def test_duration_is_integer_hours(self):
        self.assertIn("now() - 720h", q.series_query("sensor.t", "°C", 30))
        self.assertIn("now() - 1h", q.series_query("sensor.t", "°C", 0.01))
        self.assertIn("now() - 12h", q.series_query("sensor.t", "°C", 0.5))

    def test_group_interval(self):
        self.assertEqual(q.group_interval(1), "5m")
        self.assertEqual(q.group_interval(7), "30m")
        self.assertEqual(q.group_interval(30), "2h")
        self.assertEqual(q.group_interval(90), "6h")
        self.assertEqual(q.group_interval(365), "1d")


class UnsafeMeasurement(unittest.TestCase):
    def test_control_characters_are_unsafe(self):
        for bad in ["°C\n", "a\rb", "x\x00", "\x7f"]:
            self.assertTrue(q.unsafe_measurement(bad), repr(bad))
        for ok in ["°C", "W", "µg/m³", 'a"b', "a\\b", "state"]:
            self.assertFalse(q.unsafe_measurement(ok), repr(ok))


class Results(unittest.TestCase):
    def test_parse_keeps_order_and_skips_empty(self):
        payload = {"results": [
            {"statement_id": 0, "series": [{"values": [[1000, 20.5, 20.0, 21.0], [2000, None, None, None]]}]},
            {"statement_id": 1},
        ]}
        out = q.parse_results(payload, ["sensor.a", "sensor.b"])
        self.assertEqual(out, {"sensor.a": [{"start": 1000, "end": 1000, "mean": 20.5, "min": 20.0, "max": 21.0}]})

    def test_first_error(self):
        self.assertIsNone(q.first_error({"results": [{"statement_id": 0}]}))
        self.assertEqual(q.first_error({"results": [{"error": "database not found: x"}]}), "database not found: x")
        self.assertEqual(q.first_error({"error": "boom"}), "boom")
        self.assertEqual(q.first_error([1, 2]), "unexpected response")


class States(unittest.TestCase):
    def test_two_statements_before_and_inside_the_range(self):
        before, inside = q.states_queries("cover.kitchen", "state", 90, ("current_position",), 500)
        for query in (before, inside):
            self.assertIn('SELECT "state", "value", "current_position" FROM "state"', query)
            self.assertIn("\"domain\" = 'cover' AND \"entity_id\" = 'kitchen'", query)
        self.assertIn("time <= now() - 2160h ORDER BY time DESC LIMIT 1", before)
        self.assertIn("time > now() - 2160h ORDER BY time DESC LIMIT 500", inside)

    def test_attribute_names(self):
        for a in ["current_position", "brightness", "a1"]:
            self.assertTrue(q.valid_attribute(a), a)
        for a in ["", "Brightness", 'x"', "a b", "a;b", "x" * 65, "a\n"]:
            self.assertFalse(q.valid_attribute(a), a)

    def test_injection_attempt_stays_inside_quotes(self):
        query = q.states_queries("light.l", 'm" ; DROP DATABASE x ; "', 1)[1]
        self.assertIn('FROM "m\\" ; DROP DATABASE x ; \\""', query)

    @staticmethod
    def _result(columns, *rows):
        return {"series": [{"name": "state", "columns": columns, "values": list(rows)}]} if rows else {}

    def test_parse_oldest_first_with_row_before_range(self):
        cols = ["time", "state", "value", "current_position"]
        payload = {"results": [
            self._result(cols, [100, "closed", 0, 0]),
            self._result(cols, [300, "open", 1, 100], [200, "opening", 1, 40]),
            self._result(cols),
            self._result(cols, [250, None, 21.5, None]),
        ]}
        out = q.parse_states(payload, ["cover.a", "sensor.b"], ("current_position",))
        self.assertEqual(out["cover.a"], [[100, "closed", 0], [200, "opening", 40], [300, "open", 100]])
        # numeric state comes from `value`
        self.assertEqual(out["sensor.b"], [[250, 21.5, None]])

    def test_missing_columns_and_empty_rows(self):
        payload = {"results": [
            self._result(["time", "state"], [100, "on"]),
            self._result(["time", "state", "value"], [200, None, None], [150, "off", 0]),
        ]}
        out = q.parse_states(payload, ["light.a"], ("brightness",))
        self.assertEqual(out["light.a"], [[100, "on", None], [150, "off", None]])

    def test_entity_without_rows_is_omitted(self):
        payload = {"results": [{}, {}]}
        self.assertEqual(q.parse_states(payload, ["light.a"]), {})

    def test_hit_limit_drops_row_before_range(self):
        cols = ["time", "state"]
        payload = {"results": [
            self._result(cols, [100, "off"]),
            self._result(cols, [300, "on"], [200, "off"]),
        ]}
        self.assertEqual(q.parse_states(payload, ["light.a"], (), limit=2)["light.a"], [[200, "off"], [300, "on"]])
        self.assertEqual(q.parse_states(payload, ["light.a"], (), limit=3)["light.a"][0], [100, "off"])


if __name__ == "__main__":
    unittest.main()
