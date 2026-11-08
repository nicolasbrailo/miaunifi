import importlib.util
import json
import os
import unittest
from unittest import mock

# The helper is standalone (installed root-owned, not part of the package)
_path = os.path.join(os.path.dirname(__file__), "..", "helper", "miaunifi_adopt.py")
_spec = importlib.util.spec_from_file_location("miaunifi_adopt", _path)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)

DEFAULTS = [{"dst": "default", "gateway": "10.0.0.1", "dev": "wlan0", "metric": 600},
            {"dst": "default", "gateway": "10.0.0.1", "dev": "eno1", "metric": 100}]


def fake_ip(route_get, defaults=DEFAULTS):
    def run(*cmd):
        if cmd[:4] == ("ip", "-j", "route", "get"):
            return json.dumps([route_get])
        if cmd == ("ip", "-j", "-4", "route", "show", "default"):
            return json.dumps(defaults)
        raise AssertionError(cmd)
    return mock.patch.object(helper, "run", run)


class TestParentInterface(unittest.TestCase):
    def test_remote_controller_uses_its_route(self):
        with fake_ip({"dst": "10.0.0.10", "dev": "eno1", "prefsrc": "10.0.0.63"}):
            self.assertEqual(helper.parent_interface("10.0.0.10"), "eno1")

    def test_local_controller_uses_default_route(self):
        with fake_ip({"type": "local", "dst": "10.0.0.63", "dev": "lo"}):
            self.assertEqual(helper.parent_interface("10.0.0.63"), "eno1")

    def test_local_controller_without_default_route(self):
        with fake_ip({"type": "local", "dst": "10.0.0.63", "dev": "lo"}, defaults=[]):
            with self.assertRaisesRegex(RuntimeError, "--parent"):
                helper.parent_interface("10.0.0.63")


if __name__ == "__main__":
    unittest.main()
