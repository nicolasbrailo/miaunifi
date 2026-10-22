import os
import tempfile
import unittest

from miaunifi import model
from miaunifi.backends.asus_rt_ac58u_v3 import AsusRtAc58uV3
from miaunifi.backends.local import LocalBackend


def wlan(id="w1", ssid="Home", bands=("2g", "5g"), **kw):
    return model.Wlan(id=id, ssid=ssid, bands=list(bands), security=model.WPA2, psk="hunter2", **kw)


class TestLocal(unittest.TestCase):
    def test_prints_requests_without_secrets(self):
        b = LocalBackend()
        with self.assertLogs("miaunifi.backend.local") as logs:
            b.set_ssid(wlan(), "XXXX")
            b.set_password(wlan(), "hunter2")
            b.add_wlan(wlan())
        self.assertIn("Unifi requests wlan w1 ssid=XXXX", logs.output[0])
        self.assertFalse(any("hunter2" in line for line in logs.output))


class TestLocalState(unittest.TestCase):
    def test_commands_change_reported_state(self):
        b = LocalBackend(fake_clients=4)
        b.set_radio_enabled("2g", True)
        b.set_radio_enabled("5g", True)
        b.set_channel("5g", "44")
        b.add_wlan(wlan())
        self.assertEqual([(v.band, v.ssid) for v in b.get_vaps()], [("2g", "Home"), ("5g", "Home")])
        self.assertEqual({r.band: r.channel for r in b.get_radios()}, {"2g": 6, "5g": 44})
        clients = b.get_clients()
        self.assertEqual(len(clients), 4)
        self.assertEqual(len({c.mac for c in clients}), 4)
        b.set_ssid(wlan(), "Renamed")
        b.set_wlan_bands(wlan(), ["5g"])
        self.assertEqual([(v.band, v.ssid) for v in b.get_vaps()], [("5g", "Renamed")])
        b.remove_wlan(wlan())
        self.assertEqual((b.get_vaps(), b.get_clients()), ([], []))

    def test_restore_does_not_alias(self):
        cfg = model.DeviceConfig(radios=[model.Radio(band="2g")], wlans=[wlan(bands=["2g"])])
        b = LocalBackend()
        b.restore(cfg)
        b.set_ssid(cfg.wlans[0], "Changed")
        self.assertEqual(cfg.wlans[0].ssid, "Home")
        self.assertEqual(b.get_vaps()[0].ssid, "Changed")

    def test_system(self):
        s = LocalBackend().get_system()
        self.assertGreaterEqual(s.uptime_s, 0)
        self.assertEqual(len(s.load), 3)


class TestAsus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.dir.name, "asus.state.json")
        self.b = AsusRtAc58uV3(host="$asus", state_path=self.state)

    def tearDown(self):
        self.dir.cleanup()

    def ssh(self, fn, *args):
        with self.assertLogs("miaunifi.backend.asus") as logs:
            fn(*args)
        return [line.split(":", 2)[2] for line in logs.output]

    def test_first_wlan_takes_main_slot(self):
        out = self.ssh(self.b.add_wlan, wlan())
        self.assertIn("ssh $asus nvram set wl0_ssid='Home'", out)
        self.assertIn("ssh $asus nvram set wl1_ssid='Home'", out)
        self.assertIn("ssh $asus nvram set wl0_auth_mode_x='psk2'", out)
        self.assertIn("ssh $asus nvram set wl0_wpa_psk=<redacted>", out)
        self.assertFalse(any("hunter2" in line for line in out))

    def test_second_wlan_is_a_guest_slot(self):
        self.ssh(self.b.add_wlan, wlan())
        out = self.ssh(self.b.add_wlan, wlan(id="w2", ssid="Guests", bands=["2g"], guest=True))
        self.assertIn("ssh $asus nvram set wl0.1_ssid='Guests'", out)
        self.assertIn("ssh $asus nvram set wl0.1_bss_enabled='1'", out)
        self.assertIn("ssh $asus nvram set wl0.1_lanaccess='off'", out)
        self.assertIn("ssh $asus nvram set wl1.1_bss_enabled='0'", out)  # not on 5g

    def test_slots_persist(self):
        self.ssh(self.b.add_wlan, wlan())
        self.ssh(self.b.add_wlan, wlan(id="w2", ssid="Guests"))
        b = AsusRtAc58uV3(host="$asus", state_path=self.state)
        out = self.ssh(b.set_ssid, wlan(id="w2"), "Visitors")
        self.assertEqual(out, ["ssh $asus nvram set wl0.1_ssid='Visitors'",
                               "ssh $asus nvram set wl1.1_ssid='Visitors'"])

    def test_commit_restarts_once(self):
        self.ssh(self.b.add_wlan, wlan())
        self.assertEqual(self.ssh(self.b.commit),
                         ["ssh $asus nvram commit", "ssh $asus service restart_wireless"])
        with self.assertNoLogs("miaunifi.backend.asus"):
            self.b.commit()

    def test_radio(self):
        self.assertEqual(self.ssh(self.b.set_channel, "5g", "36"), ["ssh $asus nvram set wl1_chanspec='36'"])
        self.assertEqual(self.ssh(self.b.set_channel_width, "2g", "HT20"), ["ssh $asus nvram set wl0_bw='1'"])

    def test_quoting(self):
        self.ssh(self.b.add_wlan, wlan())
        self.assertIn("ssh $asus nvram set wl0_ssid='it'\\''s'", self.ssh(self.b.set_ssid, wlan(), "it's"))

    def test_kick_rejects_bad_mac(self):
        out = self.ssh(self.b.kick_client, "aa:bb; reboot")
        self.assertEqual(len(out), 1)
        self.assertIn("bad MAC", out[0])
        out = self.ssh(self.b.kick_client, "aa:bb:cc:dd:ee:ff")
        self.assertIn("ssh $asus wl -i $(nvram get wl0_ifname) deauthenticate aa:bb:cc:dd:ee:ff", out)

    def test_unsupported_warns(self):
        self.ssh(self.b.add_wlan, wlan())
        out = self.ssh(self.b.set_roaming, wlan(), True, False, False)
        self.assertIn("not supported", out[0])

    def test_remove_main_wlan_warns(self):
        self.ssh(self.b.add_wlan, wlan())
        out = self.ssh(self.b.remove_wlan, wlan())
        self.assertIn("can't be removed", out[0])

    def test_upgrade_refused(self):
        out = self.ssh(self.b.upgrade, "http://x/fw.bin", "9.9")
        self.assertIn("refusing", out[0])

    def test_state_not_implemented_warns_once(self):
        with self.assertLogs("miaunifi.backend.asus") as logs:
            self.assertEqual(self.b.get_clients(), [])
            self.assertEqual(self.b.get_clients(), [])
            self.assertIsNone(self.b.get_system())
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
