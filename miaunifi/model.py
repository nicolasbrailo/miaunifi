"""Vendor-neutral description of what the device should look like.

The UniFi translation (unifi_cfg.py) produces this; backends (backends/*)
consume it. Neither side should need to know about the other.
"""

from dataclasses import dataclass, field

BANDS = ("2g", "5g")

# Security modes a WLAN can ask for
OPEN = "open"
WPA = "wpa"                     # WPA1 only, legacy
WPA_WPA2 = "wpa-wpa2"
WPA2 = "wpa2"
WPA2_WPA3 = "wpa2-wpa3"
WPA3 = "wpa3"
WPA2_ENTERPRISE = "wpa2-enterprise"


@dataclass
class Radio:
    band: str                   # one of BANDS
    enabled: bool = True
    channel: str = "auto"       # "auto" or a channel number
    width: str = "HT20"         # HT20/HT40, VHT20/40/80/160, HE...
    txpower_level: str = "auto"  # auto, high, medium, low, custom
    txpower_dbm: int | None = None  # set when txpower_level is custom


@dataclass
class Wlan:
    id: str                     # stable id from the controller
    ssid: str
    bands: list[str] = field(default_factory=list)
    enabled: bool = True
    hidden: bool = False
    security: str = OPEN
    psk: str | None = None
    pmf: int = 0                # 802.11w: 0 off, 1 optional, 2 required
    roaming_ft: bool = False    # 802.11r
    roaming_rrm: bool = False   # 802.11k
    roaming_btm: bool = False   # 802.11v BSS transition
    guest: bool = False
    isolate: bool = False       # no client-to-client traffic
    vlan: int | None = None


# --- state: what the device is doing, reported back to the controller -------

@dataclass
class RadioState:
    band: str
    channel: int | None = None  # operating channel (the real one, also when set to auto)
    width_mhz: int | None = None
    txpower_dbm: int | None = None
    min_txpower_dbm: int | None = None
    max_txpower_dbm: int | None = None
    nss: int | None = None       # spatial streams


@dataclass
class VapState:
    """One WLAN on one band."""
    wlan_id: str
    band: str
    ssid: str
    bssid: str
    up: bool = True
    guest: bool = False
    # None: backend doesn't know; the report sums its clients instead
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None


@dataclass
class ClientState:
    mac: str
    wlan_id: str
    band: str
    signal_dbm: int | None = None
    noise_dbm: int | None = None
    tx_rate_kbps: int | None = None
    rx_rate_kbps: int | None = None
    tx_bytes: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    rx_packets: int = 0
    uptime_s: int = 0
    idle_s: int = 0
    phy: str | None = None       # n, ac, ax
    nss: int | None = None
    ip: str | None = None
    hostname: str | None = None
    vlan: int | None = None


@dataclass
class SystemState:
    uptime_s: int
    load: tuple[float, float, float] | None = None  # 1, 5, 15 minutes
    mem_total: int | None = None  # bytes
    mem_used: int | None = None
    cpu_pct: float | None = None


@dataclass
class DeviceConfig:
    country: str | None = None  # ISO 3166 alpha-2
    radios: list[Radio] = field(default_factory=list)
    wlans: list[Wlan] = field(default_factory=list)
    # Keys the translation didn't map, with indices collapsed (wireless.N.wmm)
    unmapped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
