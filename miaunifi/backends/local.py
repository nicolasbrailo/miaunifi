"""Local-only backend: logs what the controller asked for, touches nothing.

It keeps a simulated device: commands update an in-memory config, and the
get_* methods report that config back, plus optional fake clients so the
report path can be tried against a real controller without hardware.
"""

import dataclasses
import hashlib
import logging
import os
import time

from .. import model
from ..command import MiaunifiCommand

log = logging.getLogger("miaunifi.backend.local")

DEFAULT_CHANNEL = {"2g": 6, "5g": 36}
MAX_TXPOWER = 22


def _wlan(w):
    return f"wlan {w.id[-6:]}"


def _fake_mac(seed):
    """Stable, locally administered MAC."""
    return "02:" + ":".join(f"{b:02x}" for b in hashlib.md5(seed.encode()).digest()[:5])


class LocalBackend(MiaunifiCommand):
    def __init__(self, fake_clients=0):
        self.config = model.DeviceConfig()
        self.fake_clients = fake_clients
        self.started = time.monotonic()

    def _req(self, what):
        log.info("Unifi requests %s", what)

    def _find(self, wlan):
        for w in self.config.wlans:
            if w.id == wlan.id:
                return w
        return None

    def _update(self, wlan, **changes):
        w = self._find(wlan)
        if w is not None:
            for k, v in changes.items():
                setattr(w, k, v)

    def _radio(self, band):
        for r in self.config.radios:
            if r.band == band:
                return r
        r = model.Radio(band=band)
        self.config.radios.append(r)
        return r

    def restore(self, config):
        self.config = dataclasses.replace(
            config, radios=[dataclasses.replace(r) for r in config.radios],
            wlans=[dataclasses.replace(w, bands=list(w.bands)) for w in config.wlans])

    # --- commands ------------------------------------------------------------

    def reboot(self):
        self._req("reboot")

    def locate(self, on):
        self._req(f"locate={'on' if on else 'off'}")

    def upgrade(self, url, version):
        self._req(f"upgrade to {version} from {url} (refused: never flash UniFi firmware)")

    def kick_client(self, mac):
        self._req(f"kick client {mac}")

    def set_led(self, on):
        self._req(f"led={'on' if on else 'off'}")

    def set_country(self, code):
        self._req(f"country={code}")
        self.config.country = code

    def set_radio_enabled(self, band, on):
        self._req(f"radio {band} enabled={on}")
        self._radio(band).enabled = on

    def set_channel(self, band, channel):
        self._req(f"radio {band} channel={channel}")
        self._radio(band).channel = channel

    def set_channel_width(self, band, width):
        self._req(f"radio {band} width={width}")
        self._radio(band).width = width

    def set_tx_power(self, band, level, dbm):
        self._req(f"radio {band} txpower={level}" + (f" ({dbm} dBm)" if dbm is not None else ""))
        r = self._radio(band)
        r.txpower_level, r.txpower_dbm = level, dbm

    def add_wlan(self, wlan):
        self._req(f"new {_wlan(wlan)}: ssid={wlan.ssid} bands={'+'.join(wlan.bands)} "
                  f"security={wlan.security} password=<redacted> enabled={wlan.enabled} "
                  f"hidden={wlan.hidden} guest={wlan.guest} isolate={wlan.isolate} "
                  f"vlan={wlan.vlan} pmf={wlan.pmf} roaming(r/k/v)="
                  f"{wlan.roaming_ft}/{wlan.roaming_rrm}/{wlan.roaming_btm}")
        self.config.wlans = [w for w in self.config.wlans if w.id != wlan.id]
        self.config.wlans.append(dataclasses.replace(wlan, bands=list(wlan.bands)))

    def remove_wlan(self, wlan):
        self._req(f"remove {_wlan(wlan)} (ssid={wlan.ssid})")
        self.config.wlans = [w for w in self.config.wlans if w.id != wlan.id]

    def set_ssid(self, wlan, ssid):
        self._req(f"{_wlan(wlan)} ssid={ssid}")
        self._update(wlan, ssid=ssid)

    def set_password(self, wlan, psk):
        self._req(f"{_wlan(wlan)} password=<redacted>")
        self._update(wlan, psk=psk)

    def set_security(self, wlan, security):
        self._req(f"{_wlan(wlan)} security={security}")
        self._update(wlan, security=security)

    def set_wlan_enabled(self, wlan, on):
        self._req(f"{_wlan(wlan)} enabled={on}")
        self._update(wlan, enabled=on)

    def set_wlan_bands(self, wlan, bands):
        self._req(f"{_wlan(wlan)} bands={'+'.join(bands)}")
        self._update(wlan, bands=list(bands))

    def set_hidden(self, wlan, hidden):
        self._req(f"{_wlan(wlan)} hidden={hidden}")
        self._update(wlan, hidden=hidden)

    def set_guest(self, wlan, guest):
        self._req(f"{_wlan(wlan)} guest={guest}")
        self._update(wlan, guest=guest)

    def set_isolation(self, wlan, isolate):
        self._req(f"{_wlan(wlan)} isolate={isolate}")
        self._update(wlan, isolate=isolate)

    def set_vlan(self, wlan, vlan):
        self._req(f"{_wlan(wlan)} vlan={vlan}")
        self._update(wlan, vlan=vlan)

    def set_pmf(self, wlan, mode):
        self._req(f"{_wlan(wlan)} pmf={mode}")
        self._update(wlan, pmf=mode)

    def set_roaming(self, wlan, ft, rrm, btm):
        self._req(f"{_wlan(wlan)} roaming 802.11r={ft} 802.11k={rrm} 802.11v={btm}")
        self._update(wlan, roaming_ft=ft, roaming_rrm=rrm, roaming_btm=btm)

    def commit(self):
        self._req("apply changes")

    # --- state ---------------------------------------------------------------

    def get_radios(self):
        out = []
        for r in self.config.radios:
            if not r.enabled:
                continue
            channel = int(r.channel) if r.channel.isdigit() else DEFAULT_CHANNEL[r.band]
            width = "".join(c for c in r.width if c.isdigit())
            out.append(model.RadioState(
                band=r.band, channel=channel, width_mhz=int(width) if width else 20,
                txpower_dbm=r.txpower_dbm if r.txpower_dbm is not None else MAX_TXPOWER,
                min_txpower_dbm=6, max_txpower_dbm=MAX_TXPOWER, nss=2))
        return out

    def get_vaps(self):
        radios_up = {r.band for r in self.config.radios if r.enabled}
        return [model.VapState(wlan_id=w.id, band=band, ssid=w.ssid, guest=w.guest,
                               bssid=_fake_mac(f"bssid:{w.id}:{band}"))
                for w in self.config.wlans if w.enabled
                for band in w.bands if band in radios_up]

    def get_clients(self):
        vaps = self.get_vaps()
        if not vaps:
            return []
        elapsed = int(time.monotonic() - self.started)
        out = []
        for i in range(self.fake_clients):
            vap = vaps[i % len(vaps)]
            rate = 20_000 + 5_000 * i  # bytes/s, so the controller's graphs move
            out.append(model.ClientState(
                mac=_fake_mac(f"client:{i}"), wlan_id=vap.wlan_id, band=vap.band,
                hostname=f"miaunifi-fake-{i}",
                signal_dbm=-45 - 7 * (i % 6), noise_dbm=-95,
                tx_rate_kbps=72_200 if vap.band == "2g" else 433_300,
                rx_rate_kbps=65_000 if vap.band == "2g" else 390_000,
                tx_bytes=rate * elapsed, rx_bytes=rate * elapsed // 3,
                tx_packets=rate * elapsed // 1200, rx_packets=rate * elapsed // 3600,
                uptime_s=elapsed, idle_s=i,
                phy="n" if vap.band == "2g" else "ac", nss=2))
        return out

    def get_system(self):
        mem_total = mem_used = None
        try:
            with open("/proc/meminfo") as f:
                info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f}
            mem_total = info["MemTotal"]
            mem_used = mem_total - info["MemAvailable"]
        except (OSError, KeyError, ValueError):
            pass
        return model.SystemState(uptime_s=int(time.monotonic() - self.started),
                                 load=os.getloadavg(), mem_total=mem_total, mem_used=mem_used)
