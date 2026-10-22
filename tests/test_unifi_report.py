import json
import os
import unittest

from miaunifi import model, unifi_cfg, unifi_report
from miaunifi.backends.local import LocalBackend
from miaunifi.unifi_report import Reporter

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def mca_dump():
    with open(os.path.join(FIXTURES, "mca_dump_uap_ac.json")) as f:
        return json.load(f)


def local_backend(fake_clients=3):
    with open(os.path.join(FIXTURES, "system_cfg_2wlan.txt")) as f:
        cfg = unifi_cfg.translate(unifi_cfg.parse(f.read()))
    b = LocalBackend(fake_clients=fake_clients)
    b.restore(cfg)
    return b


def keys_and_types(entries):
    out = {}
    for e in entries:
        for k, v in e.items():
            out.setdefault(k, set()).add(type(v))
    return out


class TestMatchesRealAp(unittest.TestCase):
    """Everything we report must exist, with the same type, in a real UAP's mca-dump."""

    def setUp(self):
        self.dump = mca_dump()
        self.report = Reporter(local_backend()).poll()

    def assert_subset(self, ours, theirs, what):
        ours, theirs = keys_and_types(ours), keys_and_types(theirs)
        for k, types in ours.items():
            self.assertIn(k, theirs, f"{what}: real AP has no {k!r}")
            self.assertTrue(types <= theirs[k], f"{what}.{k}: {types} vs real {theirs[k]}")

    def test_vap_fields(self):
        vaps = [v for v in self.dump["vap_table"] if v["usage"] == "user"]
        ours = [{k: v for k, v in e.items()} for e in self.report["vap_table"]]
        self.assertTrue(ours)
        self.assert_subset(ours, vaps, "vap")

    def test_client_fields(self):
        theirs = [s for v in self.dump["vap_table"] for s in v.get("sta_table", [])]
        ours = [s for v in self.report["vap_table"] for s in v["sta_table"]]
        self.assertEqual(len(ours), 3)
        self.assert_subset(ours, theirs, "sta")

    def test_system_fields(self):
        self.assert_subset([self.report["sys_stats"]], [self.dump["sys_stats"]], "sys_stats")
        self.assert_subset([self.report["system-stats"]], [self.dump["system-stats"]], "system-stats")
        self.assertIsInstance(self.report["uptime"], type(self.dump["uptime"]))

    def test_vap_names_follow_real_pattern(self):
        self.assertEqual([v["name"] for v in self.report["vap_table"]], ["wifi0ap0", "wifi1ap0"])


class TestVapTable(unittest.TestCase):
    def test_clients_grouped_and_summed(self):
        vaps = [model.VapState(wlan_id="w", band="2g", ssid="Home", bssid="02:00:00:00:00:01")]
        clients = [
            model.ClientState(mac="02:00:00:00:00:0a", wlan_id="w", band="2g", signal_dbm=-60,
                              tx_bytes=100, rx_bytes=10),
            model.ClientState(mac="02:00:00:00:00:0b", wlan_id="w", band="2g", signal_dbm=-70,
                              tx_bytes=200, rx_bytes=20),
            model.ClientState(mac="02:00:00:00:00:0c", wlan_id="other", band="2g"),
        ]
        radios = [model.RadioState(band="2g", channel=11, width_mhz=20, txpower_dbm=17)]
        [vap] = unifi_report.vap_table(radios, vaps, clients)
        self.assertEqual(vap["num_sta"], 2)
        self.assertEqual(vap["avg_client_signal"], -65)
        self.assertEqual((vap["rx_bytes"], vap["tx_bytes"]), (300, 30))
        self.assertEqual((vap["channel"], vap["bw"], vap["tx_power"]), (11, 20, 17))
        self.assertEqual([s["rssi"] for s in vap["sta_table"]], [36, 26])

    def test_own_counters_win(self):
        vaps = [model.VapState(wlan_id="w", band="5g", ssid="x", bssid="b", tx_bytes=5, rx_bytes=6,
                               tx_packets=7, rx_packets=8)]
        [vap] = unifi_report.vap_table([], vaps, [])
        self.assertEqual((vap["tx_bytes"], vap["rx_bytes"], vap["tx_packets"], vap["rx_packets"]),
                         (5, 6, 7, 8))
        self.assertNotIn("channel", vap)


class TestReporter(unittest.TestCase):
    def test_failing_getter_reports_last_known(self):
        b = local_backend(fake_clients=1)
        r = Reporter(b)
        self.assertEqual(len(r.poll()["vap_table"][0]["sta_table"]), 1)
        b.get_clients = lambda: 1 / 0
        with self.assertLogs("miaunifi.report", "WARNING"):
            fields = r.poll()
        self.assertEqual(len(fields["vap_table"][0]["sta_table"]), 1)
        with self.assertNoLogs("miaunifi.report", "WARNING"):
            r.poll()

    def test_nothing_configured(self):
        self.assertEqual(Reporter(LocalBackend(fake_clients=5)).poll()["vap_table"], [])


if __name__ == "__main__":
    unittest.main()
