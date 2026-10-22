"""ASUS RT-AC58U V3 (stock Asuswrt, Broadcom) driven over SSH.

Stock firmware keeps its config in nvram and has no way to run our code, so
everything is `ssh <router> nvram set ...`, then `nvram commit` and a service
restart. For now commands are only printed, not sent.

nvram key names are from memory of Asuswrt and not yet checked on this
router: verify with `nvram show` before sending anything.

WLAN placement: per band, stock firmware has one main network (wl0, wl1) and
three guest networks (wl0.1 .. wl0.3). The first WLAN the controller gives us
takes the main slot, later ones guest slots. Slots are remembered per WLAN id
so they stay put across restarts.
"""

import json
import logging
import os
import re

from .. import model
from ..command import MiaunifiCommand

log = logging.getLogger("miaunifi.backend.asus")

UNITS = {"2g": 0, "5g": 1}
SLOTS = 4  # main + 3 guest networks

# security -> (wl_auth_mode_x, wl_crypto)
AUTH = {
    model.OPEN: ("open", None),
    model.WPA: ("psk", "tkip"),
    model.WPA_WPA2: ("pskpsk2", "tkip+aes"),
    model.WPA2: ("psk2", "aes"),
    model.WPA2_WPA3: ("psk2sae", "aes"),
    model.WPA3: ("sae", "aes"),
}
BANDWIDTH = {"20": "1", "40": "2", "80": "3"}  # wl_bw; 0 is auto
TXPOWER = {"auto": "100", "high": "100", "medium": "50", "low": "25"}  # percent, approximate
MAC_RE = re.compile(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


def quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


class AsusRtAc58uV3(MiaunifiCommand):
    def __init__(self, host="$asus", state_path="miaunifi-asus.state.json"):
        self.host = host
        self.state_path = state_path
        self.slots = self._load()  # wlan id -> slot (0 main, 1-3 guest)
        self.restart = set()       # services to restart on commit
        self._warned = set()

    # --- plumbing ----------------------------------------------------------

    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.slots, f, indent=2)
        os.replace(tmp, self.state_path)

    def _ssh(self, cmd, shown=None):
        log.info("ssh %s %s", self.host, shown or cmd)

    def _set(self, key, value, secret=False, service="wireless"):
        self._ssh(f"nvram set {key}={quote(value)}",
                  f"nvram set {key}=<redacted>" if secret else None)
        self.restart.add(service)

    @staticmethod
    def _prefix(band, slot):
        unit = UNITS[band]
        return f"wl{unit}" if slot == 0 else f"wl{unit}.{slot}"

    def _prefixes(self, wlan):
        slot = self.slots.get(wlan.id)
        if slot is None:
            log.warning("wlan %s (%s) has no slot on this router, ignoring", wlan.id, wlan.ssid)
            return []
        return [(band, slot, self._prefix(band, slot)) for band in wlan.bands if band in UNITS]

    def _unsupported(self, what):
        log.warning("asus: %s not supported by stock firmware, ignoring", what)

    def _write_wlan(self, wlan, band, slot):
        p = self._prefix(band, slot)
        self._set(f"{p}_ssid", wlan.ssid)
        self._write_security(p, wlan, wlan.security)
        self._set(f"{p}_closed", "1" if wlan.hidden else "0")
        self._set(f"{p}_ap_isolate", "1" if wlan.isolate else "0")
        self._set(f"{p}_mfp", str(wlan.pmf))
        if slot == 0:
            if not wlan.enabled:
                self._unsupported(f"disabling the main network on {band} (only the radio can be turned off)")
        else:
            self._set(f"{p}_bss_enabled", "1" if wlan.enabled else "0")
            self._set(f"{p}_lanaccess", "off" if wlan.guest else "on")
        if wlan.vlan is not None:
            self._unsupported(f"VLAN {wlan.vlan} on {wlan.ssid!r}")
        if wlan.roaming_ft or wlan.roaming_rrm or wlan.roaming_btm:
            self._unsupported(f"802.11r/k/v on {wlan.ssid!r}")

    def _write_security(self, p, wlan, security):
        if security not in AUTH:
            self._unsupported(f"security {security} on {wlan.ssid!r}")
            return
        auth, crypto = AUTH[security]
        self._set(f"{p}_auth_mode_x", auth)
        if crypto:
            self._set(f"{p}_crypto", crypto)
            if wlan.psk:
                self._set(f"{p}_wpa_psk", wlan.psk, secret=True)

    def _disable_on(self, wlan, band, slot):
        if slot == 0:
            self._unsupported(f"removing the main network from {band}")
        else:
            self._set(f"{self._prefix(band, slot)}_bss_enabled", "0")

    # --- device ------------------------------------------------------------

    def reboot(self):
        self._ssh("reboot")

    def locate(self, on):
        self._unsupported("locate (LED blinking)")

    def upgrade(self, url, version):
        log.warning("asus: refusing firmware upgrade to %s: never flash UniFi firmware", version)

    def kick_client(self, mac):
        if not mac or not MAC_RE.fullmatch(mac):
            log.warning("asus: kick_client with bad MAC %r", mac)
            return
        for unit in UNITS.values():
            self._ssh(f"wl -i $(nvram get wl{unit}_ifname) deauthenticate {mac}")

    def set_led(self, on):
        self._set("led_disable", "0" if on else "1", service="leds")

    def set_country(self, code):
        log.info("asus: regulatory domain is fixed by the firmware, ignoring country=%s", code)

    # --- radios ------------------------------------------------------------

    def set_radio_enabled(self, band, on):
        self._set(f"wl{UNITS[band]}_radio", "1" if on else "0")

    def set_channel(self, band, channel):
        self._set(f"wl{UNITS[band]}_chanspec", "0" if channel == "auto" else channel)

    def set_channel_width(self, band, width):
        bw = BANDWIDTH.get(re.sub(r"\D", "", width))
        if bw is None:
            self._unsupported(f"channel width {width} on {band}, using auto")
            bw = "0"
        self._set(f"wl{UNITS[band]}_bw", bw)

    def set_tx_power(self, band, level, dbm):
        if level == "custom":
            self._unsupported(f"txpower in dBm ({dbm}) on {band}, using max")
        self._set(f"wl{UNITS[band]}_txpower", TXPOWER.get(level, "100"))

    # --- WLANs -------------------------------------------------------------

    def add_wlan(self, wlan):
        used = set(self.slots.values())
        free = [s for s in range(SLOTS) if s not in used]
        if not free:
            log.warning("asus: no free network slot for %r (max %d per band), ignoring", wlan.ssid, SLOTS)
            return
        self.slots[wlan.id] = slot = free[0]
        self._save()
        for band in UNITS:
            if band in wlan.bands:
                self._write_wlan(wlan, band, slot)
            elif slot != 0:
                self._disable_on(wlan, band, slot)

    def remove_wlan(self, wlan):
        slot = self.slots.pop(wlan.id, None)
        self._save()
        if slot is None:
            return
        if slot == 0:
            log.warning("asus: the main network can't be removed; %r stays up until another "
                        "network takes its slot", wlan.ssid)
            return
        for band in UNITS:
            self._set(f"{self._prefix(band, slot)}_bss_enabled", "0")

    def set_ssid(self, wlan, ssid):
        for _, _, p in self._prefixes(wlan):
            self._set(f"{p}_ssid", ssid)

    def set_password(self, wlan, psk):
        for _, _, p in self._prefixes(wlan):
            self._set(f"{p}_wpa_psk", psk, secret=True)

    def set_security(self, wlan, security):
        for _, _, p in self._prefixes(wlan):
            self._write_security(p, wlan, security)

    def set_wlan_enabled(self, wlan, on):
        for band, slot, p in self._prefixes(wlan):
            if slot == 0:
                self._unsupported(f"disabling the main network on {band} (only the radio can be turned off)")
            else:
                self._set(f"{p}_bss_enabled", "1" if on else "0")

    def set_wlan_bands(self, wlan, bands):
        slot = self.slots.get(wlan.id)
        if slot is None:
            return
        for band in UNITS:
            if band in bands:
                self._write_wlan(wlan, band, slot)
            else:
                self._disable_on(wlan, band, slot)

    def set_hidden(self, wlan, hidden):
        for _, _, p in self._prefixes(wlan):
            self._set(f"{p}_closed", "1" if hidden else "0")

    def set_guest(self, wlan, guest):
        for band, slot, p in self._prefixes(wlan):
            if slot == 0:
                self._unsupported(f"guest mode on the main network ({band})")
            else:
                self._set(f"{p}_lanaccess", "off" if guest else "on")

    def set_isolation(self, wlan, isolate):
        for _, _, p in self._prefixes(wlan):
            self._set(f"{p}_ap_isolate", "1" if isolate else "0")

    def set_vlan(self, wlan, vlan):
        if vlan is not None:
            self._unsupported(f"VLAN {vlan} on {wlan.ssid!r}")

    def set_pmf(self, wlan, mode):
        for _, _, p in self._prefixes(wlan):
            self._set(f"{p}_mfp", str(mode))

    def set_roaming(self, wlan, ft, rrm, btm):
        if ft or rrm or btm:
            self._unsupported(f"802.11r/k/v on {wlan.ssid!r}")

    def commit(self):
        if not self.restart:
            return
        self._ssh("nvram commit")
        for service in sorted(self.restart):
            self._ssh(f"service restart_{service}")
        self.restart.clear()

    # --- state ---------------------------------------------------------------
    # Needs real SSH: reading state can't be print-only. Planned sources:
    #   radios:  wl -i <ifname> status / noise / txpwr1
    #   vaps:    nvram wlN[.M]_ssid, _bss_enabled; wl -i <ifname> bssid
    #   clients: wl -i <ifname> assoclist + sta_info <mac>; arp -an for IPs
    #   system:  /proc/uptime, /proc/loadavg, /proc/meminfo

    def _not_yet(self, what):
        if what not in self._warned:
            log.warning("asus: %s not implemented yet (needs real SSH), reporting nothing", what)
            self._warned.add(what)

    def get_radios(self):
        self._not_yet("get_radios")
        return []

    def get_vaps(self):
        self._not_yet("get_vaps")
        return []

    def get_clients(self):
        self._not_yet("get_clients")
        return []

    def get_system(self):
        self._not_yet("get_system")
        return None
