import dataclasses
import json
import os
import tempfile
import unittest
from unittest import mock

from miaunifi import model
from miaunifi.backends.asus_rt_ac58u_v3 import AsusRtAc58uV3, parse_snapshot
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


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class TestAsus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.dir.name, "asus.state.json")
        self.b = self.make()

    def make(self, **kw):
        b = AsusRtAc58uV3(host="router", port=2222, state_path=self.state, **kw)
        b._run = mock.Mock(return_value="")
        return b

    def tearDown(self):
        self.dir.cleanup()

    def logged(self, fn, *args):
        with self.assertLogs("miaunifi.backend.asus") as logs:
            fn(*args)
        return [line.split(":", 2)[2] for line in logs.output]

    def sent(self):
        """Commands of the last script sent to the router."""
        return self.b._run.call_args.args[0].splitlines()

    def test_first_wlan_takes_main_slot(self):
        out = self.logged(self.b.add_wlan, wlan())
        self.assertIn("asus: nvram set wl0_ssid='Home'", out)
        self.assertIn("asus: nvram set wl1_ssid='Home'", out)
        self.assertIn("asus: nvram set wl0_auth_mode_x='psk2'", out)
        self.assertIn("asus: nvram set wl0_wpa_psk=<redacted>", out)
        self.assertFalse(any("hunter2" in line for line in out))

    def test_second_wlan_is_a_guest_slot(self):
        self.b.add_wlan(wlan())
        self.b.add_wlan(wlan(id="w2", ssid="Guests", bands=["2g"], guest=True))
        self.assertIn("nvram set wl0.1_ssid='Guests'", self.b.pending)
        self.assertIn("nvram set wl0.1_bss_enabled='1'", self.b.pending)
        self.assertIn("nvram set wl0.1_lanaccess='off'", self.b.pending)
        self.assertIn("nvram set wl1.1_bss_enabled='0'", self.b.pending)  # not on 5g

    def test_slots_persist(self):
        self.b.add_wlan(wlan())
        self.b.add_wlan(wlan(id="w2", ssid="Guests"))
        b = self.make()
        b.set_ssid(wlan(id="w2"), "Visitors")
        self.assertEqual(b.pending, ["nvram set wl0.1_ssid='Visitors'", "nvram set wl1.1_ssid='Visitors'"])

    def test_readding_keeps_slots(self):
        # --resync, or a retry after a failed apply, adds every WLAN again
        self.b.add_wlan(wlan())
        self.b.add_wlan(wlan(id="w2", ssid="Guests"))
        self.b.pending.clear()
        self.b.add_wlan(wlan())
        self.b.add_wlan(wlan(id="w2", ssid="Guests"))
        self.assertEqual(self.b.slots, {"w1": 0, "w2": 1})
        self.assertIn("nvram set wl0.1_ssid='Guests'", self.b.pending)

    def test_commit_sends_one_script(self):
        self.b.add_wlan(wlan())
        self.b.set_channel("5g", "36")
        self.b.commit()
        self.b._run.assert_called_once()
        cmds = self.sent()
        self.assertEqual(cmds[0], "set -e")
        self.assertIn("nvram set wl0_wpa_psk='hunter2'", cmds)
        self.assertIn("nvram set wl1_channel='36'", cmds)
        self.assertEqual(cmds[-2:], ["nvram commit", "service restart_wireless"])
        # Nothing left to send
        self.b._run.reset_mock()
        self.b.commit()
        self.b._run.assert_not_called()

    def test_commit_failure_raises_and_drops_queue(self):
        self.b.add_wlan(wlan())
        self.b._run.side_effect = OSError("ssh router: exit 255")
        with self.assertRaises(OSError):
            self.b.commit()
        self.assertEqual(self.b.pending, [])

    def test_dry_run_sends_nothing(self):
        b = self.make(dry_run=True)
        b.add_wlan(wlan())
        b.commit()
        b.kick_client("aa:bb:cc:dd:ee:ff")
        b._run.assert_not_called()

    def test_radio(self):
        self.b.set_channel("5g", "36")
        self.b.set_channel("2g", "auto")
        self.b.set_channel_width("2g", "HT20")
        self.b.set_channel_width("5g", "VHT80")
        self.b.set_channel_width("5g", "VHT160")  # not on this router: auto
        self.assertEqual(self.b.pending, ["nvram set wl1_channel='36'", "nvram set wl0_channel='0'",
                                          "nvram set wl0_bw='0'", "nvram set wl1_bw='3'",
                                          "nvram set wl1_bw='1'"])

    def test_quoting(self):
        self.b.add_wlan(wlan())
        self.b.pending.clear()
        self.b.set_ssid(wlan(), "it's")
        self.assertIn("nvram set wl0_ssid='it'\\''s'", self.b.pending)

    def test_no_wpa3(self):
        with self.assertLogs("miaunifi.backend.asus"):
            self.b.add_wlan(dataclasses.replace(wlan(), security=model.WPA2_WPA3))
        self.assertIn("nvram set wl0_auth_mode_x='psk2'", self.b.pending)
        out = self.logged(self.b.set_security, wlan(), model.WPA3)
        self.assertIn("not supported", out[0])

    def test_kick(self):
        out = self.logged(self.b.kick_client, "aa:bb; reboot")
        self.assertIn("bad MAC", out[0])
        self.b._run.assert_not_called()
        self.b.kick_client("aa:bb:cc:dd:ee:ff")
        self.assertIn("deauthenticate aa:bb:cc:dd:ee:ff", self.sent()[1])

    def test_unsupported_warns(self):
        self.b.add_wlan(wlan())
        for fn, args in [(self.b.set_roaming, (wlan(), True, False, False)),
                         (self.b.set_pmf, (wlan(), 2)), (self.b.set_led, (False,))]:
            self.assertIn("not supported", self.logged(fn, *args)[0])

    def test_remove_main_wlan_warns(self):
        self.b.add_wlan(wlan())
        out = self.logged(self.b.remove_wlan, wlan())
        self.assertIn("can't be removed", out[0])

    def test_ssh_reuses_one_connection(self):
        b = AsusRtAc58uV3(host="router", port=2222, state_path=self.state)
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"ok", stderr=b"")
            self.assertEqual(b._run("true"), "ok")
        argv = run.call_args.args[0]
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn(f"ControlPath={self.dir.name}/.ssh-%C", argv)
        self.assertEqual(argv[-4:], ["2222", "router", "sh", "-s"])

    def test_no_connection_sharing_from_too_deep_a_directory(self):
        deep = os.path.join(self.dir.name, "d" * 100, "asus.state.json")
        with self.assertLogs("miaunifi.backend.asus"):
            b = AsusRtAc58uV3(host="router", state_path=deep)
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"", stderr=b"")
            b._run("true")
        self.assertFalse(any("Control" in a for a in run.call_args.args[0]))

    def test_upgrade_refused(self):
        out = self.logged(self.b.upgrade, "http://x/fw.bin", "9.9")
        self.assertIn("refusing", out[0])
        self.b._run.assert_not_called()


class TestAsusState(unittest.TestCase):
    """Against a snapshot in the router's real output format (MACs made up)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        state = os.path.join(self.dir.name, "asus.state.json")
        with open(state, "w") as f:
            json.dump({"w1": 0}, f)
        self.b = AsusRtAc58uV3(host="router", state_path=state)
        with open(os.path.join(FIXTURES, "asus_snapshot.txt")) as f:
            self.b._run = mock.Mock(return_value=f.read())

    def tearDown(self):
        self.dir.cleanup()

    def test_radios(self):
        self.assertEqual([(r.band, r.channel, r.width_mhz, r.txpower_dbm) for r in self.b.get_radios()],
                         [("2g", 11, 20, 17), ("5g", 100, 80, 25)])

    def test_vaps(self):
        vaps = self.b.get_vaps()
        self.assertEqual([(v.wlan_id, v.band, v.ssid, v.bssid) for v in vaps],
                         [("w1", "2g", "Batiseñal", "02:10:c9:32:0f:d0"),
                          ("w1", "5g", "Batiseñal", "02:10:c9:32:0f:d4")])
        self.assertEqual((vaps[0].rx_bytes, vaps[0].tx_bytes), (13141730651, 96070066685))

    def test_clients(self):
        clients = {c.mac: c for c in self.b.get_clients()}
        self.assertEqual(set(clients), {"02:3a:dd:7f:dd:9d", "02:0e:2b:4a:36:9d"})
        c = clients["02:3a:dd:7f:dd:9d"]
        self.assertEqual((c.band, c.wlan_id, c.signal_dbm, c.noise_dbm), ("2g", "w1", -46, -95))
        self.assertEqual((c.tx_rate_kbps, c.rx_rate_kbps, c.phy, c.nss), (72000, 71000, "n", 1))
        # From the client's side: what the AP sent is what the client received
        self.assertEqual((c.rx_bytes, c.tx_bytes), (10585855028, 415475332))
        self.assertEqual((c.uptime_s, c.idle_s, c.ip), (1937201, 15, "10.0.0.88"))
        c = clients["02:0e:2b:4a:36:9d"]
        self.assertEqual((c.phy, c.nss, c.rx_bytes, c.ip), ("ac", 2, 0, None))
        # No hostapd uptime: wlanconfig's ASSOCTIME (998:27:26)
        self.assertEqual(c.uptime_s, 998 * 3600 + 27 * 60 + 26)

    def test_system(self):
        s = self.b.get_system()
        self.assertEqual((s.uptime_s, s.load), (5295479, (0.82, 0.62, 0.49)))
        self.assertEqual((s.mem_total, s.mem_used), (124892 * 1024, (124892 - 46572) * 1024))

    def test_one_ssh_call_per_poll(self):
        self.b.get_radios(), self.b.get_vaps(), self.b.get_clients(), self.b.get_system()
        self.b._run.assert_called_once()

    def test_failure_is_cached(self):
        clock = [1000.0]

        def slow_failure(script):
            clock[0] += 5  # a connect timeout, longer than the snapshot's max age
            raise OSError("ssh router: exit 255")
        self.b._run = mock.Mock(side_effect=slow_failure)
        self.enterContext(mock.patch("time.monotonic", lambda: clock[0]))
        for fn in (self.b.get_radios, self.b.get_vaps, self.b.get_clients, self.b.get_system):
            with self.assertRaises(OSError):
                fn()
        self.b._run.assert_called_once()

    def test_unknown_guest_ifname_found_by_ssid(self):
        snap = parse_snapshot(self.b._run.return_value)
        snap["ifaces"]["ath001"] = {"info": ['ath001 IEEE 802.11ng  ESSID:"Visitors"',
                                             "  Mode:Master  Access Point: 02:00:00:00:00:01"],
                                    "sta": [], "hostapd": [], "stats": None}
        snap["nvram"].update({"wl0.1_bss_enabled": "1", "wl0.1_ssid": "Visitors", "wl0.1_ifname": ""})
        self.b.slots["w2"] = 1
        self.assertIn(("w2", "2g", 1, "ath001"), self.b._vap_ifaces(snap))


if __name__ == "__main__":
    unittest.main()
