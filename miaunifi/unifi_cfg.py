"""Parse the controller's system_cfg and translate it into a DeviceConfig.

system_cfg is a flat `key=value` list describing the whole device, e.g.

    radio.1.phyname=wifi0
    wireless.1.parent=wifi0
    wireless.1.id=<wlan id>
    aaa.1.devname=ath0
    aaa.1.wpa.psk=...

A WLAN appears once per radio it's on (wireless.N), sharing an `id`. Its
security lives in the aaa.N entry with the same devname.

This is the only module that knows UniFi's key names.
"""

import re

from . import model

SECRET_RE = re.compile(r"(^|\.)(ssid|psk|password|passphrase|key|authkey|iapp_key|secret|"
                       r"x_passphrase|x_password)$")

# Sections that describe a standalone AP's own networking and accounts. On a
# device that's also a router, applying them would break the network, so we
# ignore them on purpose.
IGNORED_SECTIONS = {
    "unifi", "system", "locale", "users", "sshd", "netconf", "bridge", "dhcpc",
    "route", "resolv", "ebtables", "iptables", "ipset", "redirector", "dnsmasq",
    "qos", "ntpclient", "switch", "dpi", "macacl", "mesh", "stamgr",
    "connectivity", "vlan",
}

# ISO 3166 numeric -> alpha-2, for the countries most likely to show up
COUNTRIES = {
    32: "AR", 36: "AU", 40: "AT", 56: "BE", 76: "BR", 124: "CA", 152: "CL",
    156: "CN", 158: "TW", 203: "CZ", 208: "DK", 246: "FI", 250: "FR",
    276: "DE", 300: "GR", 344: "HK", 348: "HU", 356: "IN", 372: "IE",
    376: "IL", 380: "IT", 392: "JP", 410: "KR", 484: "MX", 528: "NL",
    554: "NZ", 578: "NO", 616: "PL", 620: "PT", 642: "RO", 643: "RU",
    702: "SG", 710: "ZA", 724: "ES", 752: "SE", 756: "CH", 784: "AE",
    792: "TR", 804: "UA", 826: "GB", 840: "US",
}

IEEE_MODE_RE = re.compile(r"^11(ng|na|ac|ax|be|g|a)(ht|vht|he|eht)(\d+)(plus|minus)?$")

FT_BASE = {"FT-PSK": "WPA-PSK", "FT-SAE": "SAE", "FT-EAP": "WPA-EAP"}

SECURITY_BY_MGMT = {
    (frozenset({"WPA-PSK"}), 1): model.WPA,
    (frozenset({"WPA-PSK"}), 2): model.WPA2,
    (frozenset({"WPA-PSK"}), 3): model.WPA_WPA2,
    (frozenset({"SAE"}), 2): model.WPA3,
    (frozenset({"WPA-PSK", "SAE"}), 2): model.WPA2_WPA3,
    (frozenset({"WPA-EAP"}), 2): model.WPA2_ENTERPRISE,
}


def parse(text):
    """Parse a key=value config blob (system_cfg, mgmt_cfg) into a dict."""
    cfg = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.lstrip().startswith("#"):
            cfg[k.strip()] = v.strip()
    return cfg


def redact(text):
    """A key=value blob with secret values masked, for logging."""
    out = []
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and v and SECRET_RE.search(k.strip()):
            line = f"{k}=<redacted>"
        out.append(line)
    return "\n".join(out)


def collapse(key):
    """wireless.2.ssid -> wireless.N.ssid"""
    return re.sub(r"\.\d+(?=\.|$)", ".N", key)


class _Reader:
    """Dict access that remembers which keys were looked at."""

    def __init__(self, flat):
        self.flat = flat
        self.used = set()

    def get(self, key, default=None):
        self.used.add(key)
        return self.flat.get(key, default)

    def enabled(self, key, default=False):
        v = self.get(key)
        if v is None:
            return default
        return v in ("enabled", "true", "1")

    def indices(self, section):
        found = set()
        for k in self.flat:
            m = re.match(rf"{re.escape(section)}\.(\d+)\.", k)
            if m:
                found.add(int(m.group(1)))
        return sorted(found)

    def unused(self):
        return sorted({collapse(k) for k in self.flat
                       if k not in self.used and k.split(".", 1)[0] not in IGNORED_SECTIONS})


def _band(proto):
    if proto in ("ng", "g"):
        return "2g"
    if proto in ("na", "ac", "a"):
        return "5g"
    return None


def _radio(r, n, out):
    """Translate radio.n. Returns (phyname, Radio) or None."""
    p = f"radio.{n}."
    phy = r.get(p + "phyname")
    mode = r.get(p + "ieee_mode", "")
    m = IEEE_MODE_RE.match(mode)
    band = _band(m.group(1)) if m else None
    if band is None:
        out.warnings.append(f"radio {phy}: can't tell band from ieee_mode {mode!r}, skipped")
        return None
    width = f"{m.group(2).upper()}{m.group(3)}"

    level = r.get(p + "txpower_mode", "auto")
    dbm = None
    if level == "custom":
        try:
            dbm = int(r.get(p + "txpower"))
        except (TypeError, ValueError):
            out.warnings.append(f"radio {phy}: custom txpower without a dBm value")
            level = "auto"
    else:
        r.get(p + "txpower")

    channel = r.get(p + "channel", "auto")
    return phy, model.Radio(band=band, enabled=r.enabled(p + "status", True),
                            channel=channel if channel != "0" else "auto",
                            width=width, txpower_level=level, txpower_dbm=dbm)


def _security(r, a, wlan, out):
    """Fill wlan's security from aaa.a (None: open network)."""
    if a is None:
        wlan.security = model.OPEN
        return True
    p = f"aaa.{a}."
    mgmt = set()
    for k in r.indices(p + "wpa.key"):
        mgmt.update(r.get(f"{p}wpa.key.{k}.mgmt", "").split())
    # FT variants just mean 802.11r on top of the base method
    ft = any(m in FT_BASE for m in mgmt)
    mgmt = frozenset(FT_BASE.get(m, m) for m in mgmt)
    try:
        wpa = int(r.get(p + "wpa", "0"))
    except ValueError:
        wpa = 0
    sec = SECURITY_BY_MGMT.get((mgmt, wpa))
    if sec is None:
        out.warnings.append(f"wlan {wlan.ssid!r}: unknown security wpa={wpa} mgmt={sorted(mgmt)}")
        return False
    wlan.security = sec
    wlan.psk = r.get(p + "wpa.psk")
    pmf_mode = r.get(p + "pmf.mode", "0")
    wlan.pmf = int(pmf_mode) if r.enabled(p + "pmf.status") and pmf_mode.isdigit() else 0
    wlan.roaming_ft = ft or r.enabled(p + "ft.status")
    wlan.roaming_rrm = r.enabled(p + "11k.status")
    wlan.roaming_btm = r.enabled(p + "bss_transition")
    # Read but not mapped: these are implied by the above or always the same
    for k in ("status", "devname", "ssid", "id", "is_guest", "hide_ssid", "pmf.cipher",
              "eapol_version", "wpa.group_rekey"):
        r.get(p + k)
    for k in r.indices(p + "wpa"):
        r.get(f"{p}wpa.{k}.pairwise")
    return True


def translate(flat):
    """Build a DeviceConfig from a parsed system_cfg."""
    r = _Reader(flat)
    out = model.DeviceConfig()

    code = r.get("radio.countrycode")
    if code is not None:
        out.country = COUNTRIES.get(int(code)) if code.isdigit() else None
        if out.country is None:
            out.warnings.append(f"unknown country code {code}")

    radios_on = r.enabled("radio.status", True)
    phy_band = {}
    for n in r.indices("radio"):
        res = _radio(r, n, out)
        if res:
            phy, radio = res
            radio.enabled = radio.enabled and radios_on
            phy_band[phy] = radio.band
            out.radios.append(radio)
        r.get(f"radio.{n}.countrycode")

    aaa_by_dev = {r.get(f"aaa.{a}.devname"): a for a in r.indices("aaa")}
    r.get("aaa.status")

    wifi_on = r.enabled("wireless.status", True)
    wlans = {}
    for n in r.indices("wireless"):
        vap = _vap(r, n, phy_band, aaa_by_dev, out)
        if vap is None:
            continue
        vap.enabled = vap.enabled and wifi_on
        wlan = wlans.setdefault(vap.id, vap)
        if wlan is not vap:
            # Same WLAN on another radio. We keep one setting per WLAN.
            band = vap.bands[0]
            vap.bands = wlan.bands
            if vap != wlan:
                out.warnings.append(f"wlan {wlan.ssid!r}: {band} settings differ from "
                                    f"{wlan.bands[0]}; using {wlan.bands[0]}'s")
            if band not in wlan.bands:
                wlan.bands.append(band)

    out.wlans = list(wlans.values())
    out.unmapped = r.unused()
    return out


def _vap(r, n, phy_band, aaa_by_dev, out):
    """One WLAN on one radio (wireless.n), as a single-band Wlan."""
    p = f"wireless.{n}."
    band = phy_band.get(r.get(p + "parent"))
    if band is None:
        out.warnings.append(f"wireless.{n}: unknown radio {r.get(p + 'parent')!r}, skipped")
        return None
    wlan = model.Wlan(id=r.get(p + "id") or f"wireless{n}", ssid=r.get(p + "ssid", ""),
                      bands=[band],
                      enabled=r.enabled(p + "status", True),
                      hidden=r.enabled(p + "hide_ssid"),
                      guest=r.enabled(p + "is_guest"),
                      isolate=r.enabled(p + "l2_isolation"))
    # Not seen yet with VLANs configured: verify against a capture
    vlan = r.get(p + "vlan")
    if vlan and vlan.isdigit() and int(vlan) > 0:
        wlan.vlan = int(vlan)
    r.get(p + "mode")
    r.get(p + "security")  # always "none"; the real security is in aaa
    if not _security(r, aaa_by_dev.get(r.get(p + "devname")), wlan, out):
        return None
    return wlan
