"""Turns controller messages into MiaunifiCommand calls.

The controller sends the device's whole config (system_cfg) every time
anything changes, not individual changes. We keep the last config we applied
and only issue commands for what differs, so changing an SSID in the UI
becomes a single set_ssid.
"""

import dataclasses
import json
import logging
import os

from . import model, unifi_cfg

log = logging.getLogger("miaunifi.handler")

# Wlan fields -> the command that changes them. Fields not listed (id) never change.
WLAN_SETTERS = (
    (("ssid",), lambda b, w: b.set_ssid(w, w.ssid)),
    (("security",), lambda b, w: b.set_security(w, w.security)),
    (("psk",), lambda b, w: b.set_password(w, w.psk)),
    (("enabled",), lambda b, w: b.set_wlan_enabled(w, w.enabled)),
    (("bands",), lambda b, w: b.set_wlan_bands(w, w.bands)),
    (("hidden",), lambda b, w: b.set_hidden(w, w.hidden)),
    (("guest",), lambda b, w: b.set_guest(w, w.guest)),
    (("isolate",), lambda b, w: b.set_isolation(w, w.isolate)),
    (("vlan",), lambda b, w: b.set_vlan(w, w.vlan)),
    (("pmf",), lambda b, w: b.set_pmf(w, w.pmf)),
    (("roaming_ft", "roaming_rrm", "roaming_btm"),
     lambda b, w: b.set_roaming(w, w.roaming_ft, w.roaming_rrm, w.roaming_btm)),
)


def _radio_commands(backend, old, new):
    if old is None or old.enabled != new.enabled:
        backend.set_radio_enabled(new.band, new.enabled)
    if old is None or old.channel != new.channel:
        backend.set_channel(new.band, new.channel)
    if old is None or old.width != new.width:
        backend.set_channel_width(new.band, new.width)
    if old is None or (old.txpower_level, old.txpower_dbm) != (new.txpower_level, new.txpower_dbm):
        backend.set_tx_power(new.band, new.txpower_level, new.txpower_dbm)


class _Recorder:
    """Wraps a backend, noting whether any command was issued."""

    def __init__(self, backend):
        self.backend = backend
        self.called = False

    def __getattr__(self, name):
        self.called = True
        return getattr(self.backend, name)


class UnifiCommandHandler:
    def __init__(self, backend, state_path):
        self.backend = backend
        self.state_path = state_path
        self.applied = self._load()

    # --- persistence: what we last applied (holds passphrases) -----------

    def _load(self):
        try:
            with open(self.state_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"config": None, "led": None}
        cfg = data.get("config")
        if cfg is not None:
            cfg = model.DeviceConfig(
                country=cfg["country"],
                radios=[model.Radio(**r) for r in cfg["radios"]],
                wlans=[model.Wlan(**w) for w in cfg["wlans"]],
            )
        return {"config": cfg, "led": data.get("led")}

    def _save(self):
        cfg = self.applied["config"]
        data = {"led": self.applied["led"], "config": None}
        if cfg is not None:
            data["config"] = {"country": cfg.country,
                              "radios": [dataclasses.asdict(r) for r in cfg.radios],
                              "wlans": [dataclasses.asdict(w) for w in cfg.wlans]}
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.state_path)

    def forget(self):
        """Treat the next config as brand new: every setting gets a command."""
        self.applied = {"config": None, "led": None}
        self._save()

    # --- setparam ---------------------------------------------------------

    def apply_mgmt(self, mgmt):
        """mgmt_cfg settings that map to commands (just the LED so far)."""
        if "led_enabled" not in mgmt:
            return
        led = mgmt["led_enabled"] == "true"
        if led != self.applied["led"]:
            self.backend.set_led(led)
            self.backend.commit()
            self.applied["led"] = led
            self._save()

    def apply_system_cfg(self, text):
        """Issue commands for what changed. Raises if the backend fails."""
        new = unifi_cfg.translate(unifi_cfg.parse(text))
        for w in new.warnings:
            log.warning("config: %s", w)
        log.info("config: %d radios, %d wlans (%s)", len(new.radios), len(new.wlans),
                 ", ".join(f"{w.ssid!r} {'+'.join(w.bands)} {w.security}" for w in new.wlans))
        log.debug("config: not mapped: %s", ", ".join(new.unmapped))
        old = self.applied["config"] or model.DeviceConfig()

        b = _Recorder(self.backend)
        if new.country and new.country != old.country:
            b.set_country(new.country)

        old_radios = {r.band: r for r in old.radios}
        for radio in new.radios:
            _radio_commands(b, old_radios.get(radio.band), radio)

        old_wlans = {w.id: w for w in old.wlans}
        new_ids = {w.id for w in new.wlans}
        for wlan in old.wlans:
            if wlan.id not in new_ids:
                b.remove_wlan(wlan)
        for wlan in new.wlans:
            prev = old_wlans.get(wlan.id)
            if prev is None:
                b.add_wlan(wlan)
                continue
            for fields, setter in WLAN_SETTERS:
                if any(getattr(prev, f) != getattr(wlan, f) for f in fields):
                    setter(b, wlan)

        if b.called:
            self.backend.commit()
        else:
            log.info("config unchanged")
        self.applied["config"] = dataclasses.replace(new, unmapped=[], warnings=[])
        self._save()

    # --- other replies ----------------------------------------------------

    def handle_command(self, reply):
        """Non-config replies. From memory of the protocol, not yet seen live:
        unknown ones are logged so we can add them."""
        rtype = reply.get("_type")
        if rtype == "reboot":
            self.backend.reboot()
        elif rtype == "upgrade":
            self.backend.upgrade(reply.get("url"), reply.get("version"))
        elif rtype == "cmd" and reply.get("cmd") in ("set-locate", "unset-locate"):
            self.backend.locate(reply["cmd"] == "set-locate")
        elif rtype == "cmd" and reply.get("cmd") == "kick-sta":
            self.backend.kick_client(reply.get("mac"))
        else:
            log.info("unhandled controller message: %s",
                     json.dumps({k: v for k, v in reply.items() if k != "server_time_in_utc"}))
