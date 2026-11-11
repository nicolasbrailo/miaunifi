import json
import os
import tempfile
import unittest

from miaunifi import backends, config
from miaunifi.backends.local import LocalBackend

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "config.example.json")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")

    def tearDown(self):
        self.dir.cleanup()

    def load(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)
        return config.load(self.path)

    def test_example_loads(self):
        cfg = config.load(EXAMPLE)
        self.assertEqual(cfg.backend, "local")
        self.assertIsInstance(backends.make(cfg.backend, cfg.backend_options()), LocalBackend)

    def test_defaults(self):
        cfg = self.load({"inform_url": "http://c:8080/inform", "backend": "local"})
        self.assertEqual(cfg.interval, 10)
        self.assertEqual(cfg.backend_options(), {})

    def test_backend_options(self):
        cfg = self.load({"inform_url": "u", "backend": "local",
                         "backends": {"local": {"fake_clients": 3}}})
        self.assertEqual(backends.make(cfg.backend, cfg.backend_options()).fake_clients, 3)
        with self.assertRaisesRegex(ValueError, "bad options"):
            backends.make("local", {"nope": 1})

    def test_errors(self):
        for data, msg in [
            ({"backend": "local"}, "missing inform_url"),
            ({"inform_url": "u", "backend": "local", "inform-url": "x"}, "unknown settings inform-url"),
            ({"inform_url": "u", "backend": "cisco"}, "not one of"),
            ({"inform_url": "u", "backend": "local", "interval": "10"}, "interval"),
            ([], "JSON object"),
        ]:
            with self.subTest(msg=msg), self.assertRaisesRegex(config.ConfigError, msg):
                self.load(data)

    def test_missing_file(self):
        with self.assertRaisesRegex(config.ConfigError, "config.example.json"):
            config.load(os.path.join(self.dir.name, "nope.json"))


if __name__ == "__main__":
    unittest.main()
