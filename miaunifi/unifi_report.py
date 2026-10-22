"""Polls a backend's get_* methods and builds the inform's state fields.

The reverse of unifi_cfg.py: the only module that knows how UniFi expects
radios, VAPs and clients to be reported. Field names follow a real UAP's
mca-dump (tests/fixtures/mca_dump_uap_ac.json).
"""

import logging

log = logging.getLogger("miaunifi.report")

# band -> (UniFi radio, radio name); matches device.radio_table and system_cfg's phyname
RADIOS = {"2g": ("ng", "wifi0"), "5g": ("na", "wifi1")}

# UniFi's rssi is signal relative to -96 dBm (seen in mca-dump: rssi = signal + 96)
RSSI_FLOOR = -96

GETTERS = {"radios": [], "vaps": [], "clients": [], "system": None}


def _client(c):
    s = {
        "mac": c.mac,
        "authorized": True,
        "state": 15,  # associated + authorized, as most clients in the dump
        "tx_bytes": c.tx_bytes,
        "rx_bytes": c.rx_bytes,
        "tx_packets": c.tx_packets,
        "rx_packets": c.rx_packets,
        "uptime": c.uptime_s,
        "idletime": c.idle_s,
    }
    if c.signal_dbm is not None:
        s["signal"] = c.signal_dbm
        s["rssi"] = max(0, c.signal_dbm - RSSI_FLOOR)
    if c.noise_dbm is not None:
        s["noise"] = c.noise_dbm
    if c.tx_rate_kbps is not None:
        s["tx_rate"] = c.tx_rate_kbps
    if c.rx_rate_kbps is not None:
        s["rx_rate"] = c.rx_rate_kbps
    if c.phy:
        s["is_11n"] = True
        s["state_ht"] = True
        if c.phy in ("ac", "ax"):
            s["is_11ac"] = True
        if c.phy == "ax":
            s["is_11ax"] = True
    if c.nss is not None:
        s["nss"] = c.nss
    if c.ip:
        s["ip"] = c.ip
    if c.hostname:
        s["hostname"] = c.hostname
    if c.vlan is not None:
        s["vlan_id"] = c.vlan
    return s


def vap_table(radios, vaps, clients):
    radio_by_band = {r.band: r for r in radios}
    by_vap = {}
    for c in clients:
        by_vap.setdefault((c.wlan_id, c.band), []).append(c)

    table, per_band = [], {}
    for vap in vaps:
        if vap.band not in RADIOS:
            continue
        radio, radio_name = RADIOS[vap.band]
        n = per_band[vap.band] = per_band.get(vap.band, -1) + 1
        stas = by_vap.pop((vap.wlan_id, vap.band), [])
        signals = [c.signal_dbm for c in stas if c.signal_dbm is not None]

        def total(field, own):
            return own if own is not None else sum(getattr(c, field) for c in stas)

        e = {
            "id": vap.wlan_id,
            "essid": vap.ssid,
            "bssid": vap.bssid,
            "radio": radio,
            "radio_name": radio_name,
            "name": f"{radio_name}ap{n}",
            "usage": "user",
            "state": "RUN" if vap.up else "DOWN",
            "up": vap.up,
            "num_sta": len(stas),
            "avg_client_signal": round(sum(signals) / len(signals)) if signals else 0,
            # A VAP's tx is what its clients receive, and the other way round
            "tx_bytes": total("rx_bytes", vap.tx_bytes),
            "rx_bytes": total("tx_bytes", vap.rx_bytes),
            "tx_packets": total("rx_packets", vap.tx_packets),
            "rx_packets": total("tx_packets", vap.rx_packets),
            "sta_table": [_client(c) for c in stas],
        }
        r = radio_by_band.get(vap.band)
        if r is not None:
            if r.channel is not None:
                e["channel"] = r.channel
            if r.width_mhz is not None:
                e["bw"] = r.width_mhz
            if r.txpower_dbm is not None:
                e["tx_power"] = r.txpower_dbm
        table.append(e)

    for (wlan_id, band), stas in by_vap.items():
        log.debug("%d clients on unknown VAP %s/%s, not reported", len(stas), wlan_id, band)
    return table


def system_fields(system):
    fields = {"uptime": system.uptime_s}
    stats = {"uptime": str(system.uptime_s)}
    sys_stats = {}
    if system.load is not None:
        for minutes, value in zip((1, 5, 15), system.load):
            sys_stats[f"loadavg_{minutes}"] = f"{value:.2f}"
    if system.mem_total and system.mem_used is not None:
        sys_stats.update(mem_total=system.mem_total, mem_used=system.mem_used, mem_buffer=0)
        stats["mem"] = f"{100 * system.mem_used / system.mem_total:.1f}"
    if system.cpu_pct is not None:
        stats["cpu"] = f"{system.cpu_pct:.1f}"
    if sys_stats:
        fields["sys_stats"] = sys_stats
    fields["system-stats"] = stats
    return fields


class Reporter:
    def __init__(self, backend):
        self.backend = backend
        self.last = dict(GETTERS)
        self.failing = set()

    def _poll(self, name):
        try:
            self.last[name] = getattr(self.backend, f"get_{name}")()
            if name in self.failing:
                log.info("get_%s works again", name)
                self.failing.discard(name)
        except Exception as e:
            if name not in self.failing:
                log.warning("get_%s failed (%s); reporting the last known state", name, e)
                self.failing.add(name)
        return self.last[name]

    def poll(self):
        """Inform fields for the backend's current state."""
        radios, vaps, clients, system = (self._poll(name) for name in GETTERS)
        fields = {"vap_table": vap_table(radios or [], vaps or [], clients or [])}
        if system is not None:
            fields.update(system_fields(system))
        return fields
