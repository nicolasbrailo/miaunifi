"""Local-only backend: logs what the controller asked for, touches nothing."""

import logging

from ..command import MiaunifiCommand

log = logging.getLogger("miaunifi.backend.local")


def _wlan(w):
    return f"wlan {w.id[-6:]}"


class LocalBackend(MiaunifiCommand):
    def _req(self, what):
        log.info("Unifi requests %s", what)

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

    def set_radio_enabled(self, band, on):
        self._req(f"radio {band} enabled={on}")

    def set_channel(self, band, channel):
        self._req(f"radio {band} channel={channel}")

    def set_channel_width(self, band, width):
        self._req(f"radio {band} width={width}")

    def set_tx_power(self, band, level, dbm):
        self._req(f"radio {band} txpower={level}" + (f" ({dbm} dBm)" if dbm is not None else ""))

    def add_wlan(self, wlan):
        self._req(f"new {_wlan(wlan)}: ssid={wlan.ssid} bands={'+'.join(wlan.bands)} "
                  f"security={wlan.security} password=<redacted> enabled={wlan.enabled} "
                  f"hidden={wlan.hidden} guest={wlan.guest} isolate={wlan.isolate} "
                  f"vlan={wlan.vlan} pmf={wlan.pmf} roaming(r/k/v)="
                  f"{wlan.roaming_ft}/{wlan.roaming_rrm}/{wlan.roaming_btm}")

    def remove_wlan(self, wlan):
        self._req(f"remove {_wlan(wlan)} (ssid={wlan.ssid})")

    def set_ssid(self, wlan, ssid):
        self._req(f"{_wlan(wlan)} ssid={ssid}")

    def set_password(self, wlan, psk):
        self._req(f"{_wlan(wlan)} password=<redacted>")

    def set_security(self, wlan, security):
        self._req(f"{_wlan(wlan)} security={security}")

    def set_wlan_enabled(self, wlan, on):
        self._req(f"{_wlan(wlan)} enabled={on}")

    def set_wlan_bands(self, wlan, bands):
        self._req(f"{_wlan(wlan)} bands={'+'.join(bands)}")

    def set_hidden(self, wlan, hidden):
        self._req(f"{_wlan(wlan)} hidden={hidden}")

    def set_guest(self, wlan, guest):
        self._req(f"{_wlan(wlan)} guest={guest}")

    def set_isolation(self, wlan, isolate):
        self._req(f"{_wlan(wlan)} isolate={isolate}")

    def set_vlan(self, wlan, vlan):
        self._req(f"{_wlan(wlan)} vlan={vlan}")

    def set_pmf(self, wlan, mode):
        self._req(f"{_wlan(wlan)} pmf={mode}")

    def set_roaming(self, wlan, ft, rrm, btm):
        self._req(f"{_wlan(wlan)} roaming 802.11r={ft} 802.11k={rrm} 802.11v={btm}")

    def commit(self):
        self._req("apply changes")
