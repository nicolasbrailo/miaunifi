import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from miaunifi import device
from miaunifi.__main__ import handle_upgrade, reported_version


class TestVersion(unittest.TestCase):
    def test_newer_version(self):
        self.assertTrue(device.newer_version("6.6.77.15402", "6.6.55.15189"))
        self.assertTrue(device.newer_version("6.10.0", "6.9.9"))
        self.assertFalse(device.newer_version("6.6.55.15189", "6.6.55.15189"))
        self.assertFalse(device.newer_version("6.5.0", "6.6.55"))
        self.assertFalse(device.newer_version(None, "6.6.55"))


class TestUpgrade(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.args = SimpleNamespace(state=os.path.join(self.dir.name, "state.json"))
        self.state = {}

    def tearDown(self):
        self.dir.cleanup()

    def test_claims_newer_version(self):
        self.assertEqual(reported_version(self.state), device.VERSION)
        handle_upgrade({"_type": "upgrade", "version": "99.0.1"}, self.state, self.args)
        self.assertEqual(reported_version(self.state), "99.0.1")
        with open(self.args.state) as f:
            self.assertEqual(json.load(f)["version"], "99.0.1")

    def test_ignores_older_version(self):
        handle_upgrade({"_type": "upgrade", "version": "99.0.1"}, self.state, self.args)
        handle_upgrade({"_type": "upgrade", "version": "98.0"}, self.state, self.args)
        self.assertEqual(reported_version(self.state), "99.0.1")

    def test_code_version_wins_over_stale_state(self):
        self.assertEqual(reported_version({"version": "1.0"}), device.VERSION)


if __name__ == "__main__":
    unittest.main()
