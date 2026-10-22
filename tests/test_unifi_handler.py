import os
import tempfile
import unittest
from unittest import mock

from miaunifi import model
from miaunifi.command import MiaunifiCommand
from miaunifi.unifi_handler import UnifiCommandHandler

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
WLAN_ID = "657a28d202733c254962c1e8"


def fixture_text(**changes):
    with open(os.path.join(FIXTURES, "system_cfg_2wlan.txt")) as f:
        text = f.read()
    for key, value in changes.items():
        lines = [l for l in text.splitlines() if not l.startswith(key + "=")]
        text = "\n".join(lines + [f"{key}={value}"])
    return text


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "applied.state.json")
        self.backend = mock.create_autospec(MiaunifiCommand, instance=True)
        self.handler = UnifiCommandHandler(self.backend, self.path)

    def tearDown(self):
        self.dir.cleanup()

    def calls(self):
        names = [c[0] for c in self.backend.method_calls]
        self.backend.reset_mock()
        return names

    def test_first_config_sets_everything(self):
        self.handler.apply_system_cfg(fixture_text())
        names = self.calls()
        self.assertEqual(names[0], "set_country")
        self.assertEqual(names.count("set_channel"), 2)
        self.assertEqual(names.count("add_wlan"), 1)
        self.assertEqual(names[-1], "commit")

    def test_add_wlan_gets_full_wlan(self):
        self.handler.apply_system_cfg(fixture_text())
        wlan = self.backend.add_wlan.call_args.args[0]
        self.assertEqual((wlan.id, wlan.ssid, wlan.bands, wlan.security), (WLAN_ID, "SSID_1", ["2g", "5g"], model.WPA2))

    def test_same_config_does_nothing(self):
        self.handler.apply_system_cfg(fixture_text())
        self.calls()
        self.handler.apply_system_cfg(fixture_text())
        self.assertEqual(self.calls(), [])

    def test_only_changes_are_sent(self):
        self.handler.apply_system_cfg(fixture_text())
        self.calls()
        self.handler.apply_system_cfg(fixture_text(**{
            "wireless.1.ssid": "NEW", "wireless.2.ssid": "NEW",
            "aaa.1.ssid": "NEW", "aaa.2.ssid": "NEW",
            "radio.2.channel": "36"}))
        self.assertEqual(self.calls(), ["set_channel", "set_ssid", "commit"])

    def test_change_args(self):
        self.handler.apply_system_cfg(fixture_text())
        self.handler.apply_system_cfg(fixture_text(**{"aaa.1.wpa.psk": "NEWPSK", "aaa.2.wpa.psk": "NEWPSK",
                                                      "radio.2.channel": "36"}))
        self.backend.set_channel.assert_called_with("5g", "36")
        wlan, psk = self.backend.set_password.call_args.args
        self.assertEqual((wlan.id, psk), (WLAN_ID, "NEWPSK"))

    def test_state_survives_restart(self):
        self.handler.apply_system_cfg(fixture_text())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.calls()
        handler = UnifiCommandHandler(self.backend, self.path)
        handler.apply_system_cfg(fixture_text())
        self.assertEqual(self.calls(), [])
        handler.forget()
        handler.apply_system_cfg(fixture_text())
        self.assertIn("add_wlan", self.calls())

    def test_removed_wlan(self):
        self.handler.apply_system_cfg(fixture_text())
        self.calls()
        text = "\n".join(l for l in fixture_text().splitlines()
                         if not l.startswith(("wireless.", "aaa.")))
        self.handler.apply_system_cfg(text)
        self.assertEqual(self.calls(), ["remove_wlan", "commit"])

    def test_backend_failure_is_not_remembered(self):
        self.backend.add_wlan.side_effect = OSError("router unreachable")
        with self.assertRaises(OSError):
            self.handler.apply_system_cfg(fixture_text())
        self.backend.add_wlan.side_effect = None
        self.calls()
        self.handler.apply_system_cfg(fixture_text())
        self.assertIn("add_wlan", self.calls())

    def test_led(self):
        self.handler.apply_mgmt({"led_enabled": "true"})
        self.handler.apply_mgmt({"led_enabled": "true"})
        self.assertEqual(self.calls(), ["set_led", "commit"])
        self.handler.apply_mgmt({"led_enabled": "false"})
        self.backend.set_led.assert_called_with(False)

    def test_commands(self):
        self.handler.handle_command({"_type": "reboot"})
        self.handler.handle_command({"_type": "cmd", "cmd": "set-locate"})
        self.handler.handle_command({"_type": "cmd", "cmd": "kick-sta", "mac": "aa:bb:cc:dd:ee:ff"})
        self.handler.handle_command({"_type": "upgrade", "url": "http://x/fw.bin", "version": "9.9"})
        with self.assertLogs("miaunifi.handler"):
            self.handler.handle_command({"_type": "something-new"})
        self.assertEqual(self.calls(), ["reboot", "locate", "kick_client", "upgrade"])


if __name__ == "__main__":
    unittest.main()
