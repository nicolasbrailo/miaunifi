import os
import unittest

from miaunifi import model, unifi_cfg

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return unifi_cfg.parse(f.read())


class TestParse(unittest.TestCase):
    def test_parse_skips_comments_and_keeps_equals_in_values(self):
        cfg = unifi_cfg.parse("# radio\nradio.1.channel=auto\nx.cmd=-a b=c\n")
        self.assertEqual(cfg, {"radio.1.channel": "auto", "x.cmd": "-a b=c"})

    def test_redact(self):
        text = "aaa.1.wpa.psk=hunter2\naaa.1.ssid=Home\nusers.1.password=x\nradio.1.channel=6"
        out = unifi_cfg.redact(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("Home", out)
        self.assertIn("radio.1.channel=6", out)


class TestTranslate(unittest.TestCase):
    """Golden test against a real (redacted) UniFi Network 8.0.28 config."""

    def setUp(self):
        self.cfg = unifi_cfg.translate(load("system_cfg_2wlan.txt"))

    def test_country(self):
        self.assertEqual(self.cfg.country, "GB")

    def test_radios(self):
        self.assertEqual(self.cfg.radios, [
            model.Radio(band="2g", channel="auto", width="HT20"),
            model.Radio(band="5g", channel="auto", width="HT40"),
        ])

    def test_one_wlan_on_both_bands(self):
        self.assertEqual(self.cfg.wlans, [model.Wlan(
            id="657a28d202733c254962c1e8", ssid="SSID_1", bands=["2g", "5g"],
            security=model.WPA2, psk="PSK_1", roaming_btm=True,
        )])

    def test_no_warnings(self):
        self.assertEqual(self.cfg.warnings, [])

    def test_ignored_sections_are_not_reported_unmapped(self):
        for key in self.cfg.unmapped:
            self.assertNotIn(key.split(".")[0], unifi_cfg.IGNORED_SECTIONS, key)
        self.assertIn("wireless.N.dtim_period", self.cfg.unmapped)

    def test_security_variants(self):
        base = load("system_cfg_2wlan.txt")
        cases = {
            ("SAE", "2"): model.WPA3,
            ("WPA-PSK SAE", "2"): model.WPA2_WPA3,
            ("WPA-PSK", "3"): model.WPA_WPA2,
        }
        for (mgmt, wpa), want in cases.items():
            cfg = dict(base)
            for a in (1, 2):
                cfg[f"aaa.{a}.wpa.key.1.mgmt"] = mgmt
                cfg[f"aaa.{a}.wpa"] = wpa
            self.assertEqual(unifi_cfg.translate(cfg).wlans[0].security, want, mgmt)

    def test_ft_mgmt_means_80211r(self):
        cfg = load("system_cfg_2wlan.txt")
        for a in (1, 2):
            cfg[f"aaa.{a}.wpa.key.1.mgmt"] = "WPA-PSK FT-PSK"
        wlan = unifi_cfg.translate(cfg).wlans[0]
        self.assertEqual(wlan.security, model.WPA2)
        self.assertTrue(wlan.roaming_ft)

    def test_open_network_has_no_aaa(self):
        cfg = {k: v for k, v in load("system_cfg_2wlan.txt").items() if not k.startswith("aaa.")}
        wlan = unifi_cfg.translate(cfg).wlans[0]
        self.assertEqual(wlan.security, model.OPEN)
        self.assertIsNone(wlan.psk)

    def test_per_band_difference_warns(self):
        cfg = load("system_cfg_2wlan.txt")
        cfg["wireless.2.hide_ssid"] = "true"
        out = unifi_cfg.translate(cfg)
        self.assertFalse(out.wlans[0].hidden)
        self.assertEqual(len(out.warnings), 1)


if __name__ == "__main__":
    unittest.main()
