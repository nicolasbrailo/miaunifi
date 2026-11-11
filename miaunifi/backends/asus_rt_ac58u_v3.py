"""ASUS RT-AC58U V3 (stock Asuswrt 3.0.0.4.386, Qualcomm QCA) driven over SSH.

The router reports itself as an RT-AC59U_V2. Stock firmware keeps its config in
nvram and has no way to run our code, so everything is a shell script sent
over `ssh <router> sh -s`. Key login must already work (BatchMode).

Writes are queued and sent in one go on commit: the `nvram set`s, then
`nvram commit` and `service restart_wireless` (clients drop for a few seconds,
as when saving in the web UI). nvram keys and value encodings were checked on
the router and against its web UI (Advanced_Wireless_Content.asp):
    wlN_channel   0 auto, else the channel
    wlN_bw        1 auto, 0 20 MHz, 2 40 MHz, 3 80 MHz (0 is auto only on BLUECAVE)
    wlN_txpower   percent; the UI's levels are 0/25/50/88/100
This firmware has no WPA3 (not in rc_support) and no PMF setting.

State comes from one snapshot script (iwconfig, iwlist, iwpriv, wlanconfig,
hostapd_cli, /sys, /proc), cached briefly so all get_* share one SSH call.

WLAN placement: per band, stock firmware has one main network (wl0, wl1) and
guest networks (wl0.1 .. wl0.3). The first WLAN the controller gives us takes
the main slot, later ones guest slots. Slots are remembered per WLAN id so they
stay put across restarts. Guest interfaces only exist while enabled and their
names weren't seen yet: they're found via nvram wlN.M_ifname, else by SSID.
"""

import json
import logging
import os
import re
import subprocess
import time

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
}
BANDWIDTH = {"20": "0", "40": "2", "80": "3"}  # wl_bw
BANDWIDTH_AUTO = "1"
TXPOWER = {"auto": "100", "high": "100", "medium": "50", "low": "25"}  # percent
MAC_RE = re.compile(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")

CONNECT_TIMEOUT = 5
RUN_TIMEOUT = 30
# One SSH connection, kept open between calls: every new login costs the
# router a handshake and a line in its syslog (which it copies to flash)
CONTROL_PERSIST = 300
SOCKET_PATH_MAX = 107  # sun_path, less its NUL; ssh refuses to run beyond it
SNAPSHOT_MAX_AGE = 3  # seconds; the get_* calls before one inform share a snapshot
NOISE_FLOOR = -95     # QCA's RSSI is relative to the noise floor

# Everything get_* needs, in one round trip. The `Encryption key` line of
# iwconfig is the group key: it never leaves the router.
SNAPSHOT = r"""
for i in $(ls /sys/class/net | grep '^ath'); do
  s=/sys/class/net/$i/statistics
  echo "@@iface $i"
  iwconfig $i 2>/dev/null | grep -v 'Encryption key'
  iwlist $i channel 2>/dev/null | grep -i 'current'
  iwpriv $i get_mode 2>/dev/null
  echo "@@stats $(cat $s/rx_bytes) $(cat $s/tx_bytes) $(cat $s/rx_packets) $(cat $s/tx_packets)"
  echo "@@sta"
  wlanconfig $i list sta 2>/dev/null
  echo "@@hostapd"
  hostapd_cli -p /var/run/hostapd -i $i all_sta 2>/dev/null
done
echo "@@nvram"
for k in {keys}; do echo "$k=$(nvram get $k)"; done
echo "@@uptime $(cat /proc/uptime)"
echo "@@loadavg $(cat /proc/loadavg)"
echo "@@meminfo"
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "@@arp"
cat /proc/net/arp
"""


def quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


# --- snapshot parsing -------------------------------------------------------

def parse_snapshot(text):
    """The SNAPSHOT script's output as a dict; raises ValueError if garbled."""
    snap = {"ifaces": {}, "nvram": {}, "uptime": None, "load": None, "mem": {}, "arp": {}}
    section, iface = None, None
    for line in text.splitlines():
        if line.startswith("@@"):
            tag, _, rest = line[2:].partition(" ")
            if tag == "iface":
                iface = snap["ifaces"][rest.strip()] = {"info": [], "sta": [], "hostapd": [],
                                                         "stats": None}
                section = "info"
            elif tag == "stats":
                values = rest.split()
                if len(values) == 4:
                    iface["stats"] = [int(v) for v in values]
            elif tag == "uptime":
                snap["uptime"] = float(rest.split()[0])
            elif tag == "loadavg":
                snap["load"] = tuple(float(v) for v in rest.split()[:3])
            else:
                section = tag
            continue
        if section in ("info", "sta", "hostapd"):
            iface[section].append(line)
        elif section == "nvram":
            k, _, v = line.partition("=")
            snap["nvram"][k] = v
        elif section == "meminfo":
            k, _, v = line.partition(":")
            snap["mem"][k] = int(v.split()[0]) * 1024
        elif section == "arp":
            parts = line.split()
            # Flags 0x2: a complete entry
            if len(parts) >= 4 and parts[2] == "0x2" and MAC_RE.fullmatch(parts[3]):
                snap["arp"][parts[3].lower()] = parts[0]
    return snap


def _search(pattern, lines, conv=str):
    for line in lines:
        m = re.search(pattern, line)
        if m:
            return conv(m.group(1))
    return None


def iface_info(lines):
    """iwconfig/iwlist/iwpriv lines -> essid, bssid, channel, width, txpower, noise."""
    mode = _search(r"get_mode:(\S+)", lines)
    width = re.search(r"(\d+)(PLUS|MINUS)?$", mode) if mode else None
    bssid = _search(r"Access Point: (\S+)", lines)
    return {
        "essid": _search(r'ESSID:"(.*)"', lines),
        "bssid": bssid.lower() if bssid and MAC_RE.fullmatch(bssid) else None,
        "channel": _search(r"\(Channel (\d+)\)", lines, int),
        "width_mhz": int(width.group(1)) if width else None,
        "txpower_dbm": _search(r"Tx-Power[:=](-?\d+) dBm", lines, int),
        "noise_dbm": _search(r"Noise level[:=](-?\d+) dBm", lines, int),
    }


def _rate_kbps(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)M", text)
    return int(float(m.group(1)) * 1000) if m else None


def stations(lines):
    """`wlanconfig athN list sta` -> {mac: fields}. Some middle columns can be
    empty, so fields are read from the left up to IDLE and around MODE."""
    out = {}
    for line in lines:
        t = line.split()
        if len(t) < 9 or not MAC_RE.fullmatch(t[0]):
            continue
        st = {"tx_rate_kbps": _rate_kbps(t[3]), "rx_rate_kbps": _rate_kbps(t[4]),
              "rssi": int(t[5]) if t[5].isdigit() else None,
              "idle_s": int(t[8]) if t[8].isdigit() else 0,
              "phy": None, "nss": None, "uptime_s": 0}
        for i, tok in enumerate(t):
            if tok.startswith("IEEE80211_MODE_"):
                st["phy"] = ("ax" if "11AX" in tok else "ac" if "11AC" in tok
                             else "n" if "11N" in tok else None)
                nss = [int(v) for v in t[i + 2:i + 4] if v.isdigit()]  # RXNSS TXNSS
                st["nss"] = max(nss) if nss else None
            elif re.fullmatch(r"\d+:\d\d:\d\d", tok):
                h, m, s = (int(v) for v in tok.split(":"))
                st["uptime_s"] = h * 3600 + m * 60 + s
        out[t[0].lower()] = st
    return out


def hostapd_stations(lines):
    """`hostapd_cli all_sta` -> {mac: {key: value}}."""
    out, cur = {}, None
    for line in lines:
        line = line.strip()
        if MAC_RE.fullmatch(line):
            cur = out[line.lower()] = {}
        elif cur is not None and "=" in line:
            k, _, v = line.partition("=")
            cur[k] = v
    return out


def _int(d, key):
    v = d.get(key, "")
    return int(v) if v.isdigit() else None


class AsusRtAc58uV3(MiaunifiCommand):
    def __init__(self, host, port=22, state_path="miaunifi-asus.state.json", dry_run=False):
        """host: SSH destination ([user@]address or an ~/.ssh/config alias).
        dry_run: read state for real, but only log what would be written."""
        self.host = host
        self.port = port
        self.state_path = state_path
        # Next to our state; %C (a hash of host, port and user) keeps the socket
        # path short, as unix sockets need. Too deep a directory: no sharing.
        self.control_path = os.path.join(os.path.dirname(os.path.abspath(state_path)), ".ssh-%C")
        if len(self.control_path) - len("%C") + 40 > SOCKET_PATH_MAX:
            log.warning("asus: %s is too long a path for an SSH control socket; "
                        "every call opens a new connection", self.control_path)
            self.control_path = None
        self.dry_run = dry_run
        self.slots = self._load()  # wlan id -> slot (0 main, 1-3 guest)
        self.pending = []          # commands for the next commit
        self._snap = None          # (monotonic time, snapshot or exception)

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

    def _run(self, script):
        """Run a shell script on the router, return its output. Raises OSError.
        The script goes over stdin, so values (passphrases) aren't in our argv."""
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
                "-o", "LogLevel=ERROR"]
        if self.control_path:
            argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={self.control_path}",
                     "-o", f"ControlPersist={CONTROL_PERSIST}"]
        argv += ["-p", str(self.port), self.host, "sh", "-s"]
        try:
            p = subprocess.run(argv, input=script.encode(), capture_output=True, timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise OSError(f"ssh {self.host}: no answer after {RUN_TIMEOUT}s") from None
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip().splitlines()
            raise OSError(f"ssh {self.host}: exit {p.returncode}: {' / '.join(err[-2:])}")
        return p.stdout.decode(errors="replace")

    def _write(self, cmds):
        if self.dry_run:
            log.info("asus: dry run, not sending %d commands", len(cmds))
            return
        self._run("set -e\n" + "\n".join(cmds) + "\n")

    def _set(self, key, value, secret=False):
        log.info("asus: nvram set %s=%s", key, "<redacted>" if secret else quote(value))
        self.pending.append(f"nvram set {key}={quote(value)}")

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
        log.warning("asus: %s not supported by this firmware, ignoring", what)

    def _write_wlan(self, wlan, band, slot):
        p = self._prefix(band, slot)
        self._set(f"{p}_ssid", wlan.ssid)
        self._write_security(p, wlan, wlan.security)
        self._set(f"{p}_closed", "1" if wlan.hidden else "0")
        self._set(f"{p}_ap_isolate", "1" if wlan.isolate else "0")
        if slot == 0:
            if not wlan.enabled:
                self._unsupported(f"disabling the main network on {band} (only the radio can be turned off)")
        else:
            self._set(f"{p}_bss_enabled", "1" if wlan.enabled else "0")
            self._set(f"{p}_lanaccess", "off" if wlan.guest else "on")
        if wlan.pmf:
            self._unsupported(f"PMF (802.11w) on {wlan.ssid!r}")
        if wlan.vlan is not None:
            self._unsupported(f"VLAN {wlan.vlan} on {wlan.ssid!r}")
        if wlan.roaming_ft or wlan.roaming_rrm or wlan.roaming_btm:
            self._unsupported(f"802.11r/k/v on {wlan.ssid!r}")

    def _write_security(self, p, wlan, security):
        if security == model.WPA2_WPA3:
            log.warning("asus: no WPA3 on this firmware, using WPA2 for %r", wlan.ssid)
            security = model.WPA2
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
        log.info("asus: rebooting the router")
        # In the background, so our SSH session ends cleanly first
        self._write(["(sleep 1; reboot) >/dev/null 2>&1 &"])

    def locate(self, on):
        self._unsupported("locate (LED blinking)")

    def upgrade(self, url, version):
        log.warning("asus: refusing firmware upgrade to %s: never flash UniFi firmware", version)

    def kick_client(self, mac):
        if not mac or not MAC_RE.fullmatch(mac):
            log.warning("asus: kick_client with bad MAC %r", mac)
            return
        log.info("asus: kicking %s", mac)
        self._write([f"for i in $(ls /sys/class/net | grep '^ath'); do "
                     f"hostapd_cli -p /var/run/hostapd -i $i deauthenticate {mac} >/dev/null || true; done"])

    def set_led(self, on):
        self._unsupported("LED control")

    def set_country(self, code):
        log.info("asus: regulatory domain is fixed by the firmware, ignoring country=%s", code)

    # --- radios ------------------------------------------------------------

    def set_radio_enabled(self, band, on):
        self._set(f"wl{UNITS[band]}_radio", "1" if on else "0")

    def set_channel(self, band, channel):
        self._set(f"wl{UNITS[band]}_channel", "0" if channel == "auto" else channel)

    def set_channel_width(self, band, width):
        bw = BANDWIDTH.get(re.sub(r"\D", "", width))
        if bw is None:
            self._unsupported(f"channel width {width} on {band}, using auto")
            bw = BANDWIDTH_AUTO
        self._set(f"wl{UNITS[band]}_bw", bw)

    def set_tx_power(self, band, level, dbm):
        if level == "custom":
            self._unsupported(f"txpower in dBm ({dbm}) on {band}, using max")
        self._set(f"wl{UNITS[band]}_txpower", TXPOWER.get(level, "100"))

    # --- WLANs -------------------------------------------------------------

    def add_wlan(self, wlan):
        # Already placed (a resync, or a retry after a failed apply): keep its
        # slot, or WLANs would trade places with each other
        slot = self.slots.get(wlan.id)
        if slot is None:
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
        if mode:
            self._unsupported(f"PMF (802.11w) on {wlan.ssid!r}")

    def set_roaming(self, wlan, ft, rrm, btm):
        if ft or rrm or btm:
            self._unsupported(f"802.11r/k/v on {wlan.ssid!r}")

    def commit(self):
        if not self.pending:
            return
        # Taken first: if sending fails, the handler re-issues the whole change
        cmds, self.pending = self.pending, []
        log.info("asus: applying %d settings (nvram commit, restart_wireless)", len(cmds))
        self._write(cmds + ["nvram commit", "service restart_wireless"])
        self._snap = None

    # --- state ---------------------------------------------------------------

    def _snapshot(self):
        if self._snap is None or time.monotonic() - self._snap[0] > SNAPSHOT_MAX_AGE:
            keys = [f"wl{u}_radio" for u in UNITS.values()]
            for band in UNITS:
                for slot in range(SLOTS):
                    p = self._prefix(band, slot)
                    keys += [f"{p}_ifname", f"{p}_ssid", f"{p}_bss_enabled", f"{p}_lanaccess"]
            try:
                result = parse_snapshot(self._run(SNAPSHOT.replace("{keys}", " ".join(keys))))
            except (OSError, ValueError) as e:
                result = e
            # Failures are cached too: a dead router costs one timeout per inform, not
            # four. Timed from the end of the call, which can outlast the max age.
            self._snap = (time.monotonic(), result)
        if isinstance(self._snap[1], Exception):
            raise self._snap[1]
        return self._snap[1]

    def _vap_ifaces(self, snap):
        """(wlan_id, band, slot, ifname) for each of our WLANs that is up on the router."""
        nv, ifaces = snap["nvram"], snap["ifaces"]
        main = {nv.get(f"wl{u}_ifname") for u in UNITS.values()}
        out = []
        for wlan_id, slot in self.slots.items():
            for band in UNITS:
                p = self._prefix(band, slot)
                ifname = nv.get(f"{p}_ifname")
                if slot != 0:
                    if nv.get(f"{p}_bss_enabled") != "1":
                        continue
                    if not ifname:
                        ssid = nv.get(f"{p}_ssid")
                        ifname = next((i for i, d in ifaces.items() if i not in main
                                       and iface_info(d["info"])["essid"] == ssid), None)
                if ifname in ifaces:
                    out.append((wlan_id, band, slot, ifname))
        return out

    def get_radios(self):
        snap = self._snapshot()
        out = []
        for band, unit in UNITS.items():
            iface = snap["ifaces"].get(snap["nvram"].get(f"wl{unit}_ifname"))
            if iface is None or snap["nvram"].get(f"wl{unit}_radio") == "0":
                continue
            info = iface_info(iface["info"])
            out.append(model.RadioState(band=band, channel=info["channel"], width_mhz=info["width_mhz"],
                                        txpower_dbm=info["txpower_dbm"]))
        return out

    def get_vaps(self):
        snap = self._snapshot()
        out = []
        for wlan_id, band, slot, ifname in self._vap_ifaces(snap):
            iface = snap["ifaces"][ifname]
            info = iface_info(iface["info"])
            if info["bssid"] is None:
                continue
            stats = iface["stats"] or [None] * 4
            out.append(model.VapState(
                wlan_id=wlan_id, band=band, ssid=info["essid"] or "", bssid=info["bssid"],
                guest=slot != 0 and snap["nvram"].get(f"{self._prefix(band, slot)}_lanaccess") == "off",
                rx_bytes=stats[0], tx_bytes=stats[1], rx_packets=stats[2], tx_packets=stats[3]))
        return out

    def get_clients(self):
        snap = self._snapshot()
        out = []
        for wlan_id, band, _, ifname in self._vap_ifaces(snap):
            iface = snap["ifaces"][ifname]
            noise = iface_info(iface["info"])["noise_dbm"] or NOISE_FLOOR
            hostapd = hostapd_stations(iface["hostapd"])
            for mac, st in stations(iface["sta"]).items():
                h = hostapd.get(mac, {})
                inactive = _int(h, "inactive_msec")
                # ClientState counts from the client's side; hostapd's from the AP's
                out.append(model.ClientState(
                    mac=mac, wlan_id=wlan_id, band=band,
                    signal_dbm=st["rssi"] + noise if st["rssi"] is not None else None,
                    noise_dbm=noise,
                    tx_rate_kbps=st["tx_rate_kbps"], rx_rate_kbps=st["rx_rate_kbps"],
                    tx_bytes=_int(h, "rx_bytes") or 0, rx_bytes=_int(h, "tx_bytes") or 0,
                    tx_packets=_int(h, "rx_packets") or 0, rx_packets=_int(h, "tx_packets") or 0,
                    uptime_s=_int(h, "connected_time") or st["uptime_s"],
                    idle_s=inactive // 1000 if inactive is not None else st["idle_s"],
                    phy=st["phy"], nss=st["nss"], ip=snap["arp"].get(mac)))
        return out

    def get_system(self):
        snap = self._snapshot()
        if snap["uptime"] is None:
            return None
        mem = snap["mem"]
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        return model.SystemState(uptime_s=int(snap["uptime"]), load=snap["load"], mem_total=total,
                                 mem_used=total - avail if total and avail is not None else None)
