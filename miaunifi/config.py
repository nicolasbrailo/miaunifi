"""Service settings, read from a JSON file (config.json; see config.example.json).

Relative paths are relative to the working directory. `backends` holds each
backend's constructor arguments by backend name; only the selected one is used.
"""

import dataclasses
import json
from dataclasses import dataclass, field

from . import backends


class ConfigError(Exception):
    pass


@dataclass
class Config:
    inform_url: str
    backend: str                # one of backends.NAMES
    hostname: str = "Miaunifi device"
    mac: str | None = None      # None: random, persisted in the state file
    interval: int = 10          # seconds between informs, until the controller sets one
    adopt_helper: str = "sudo -n /usr/local/bin/miaunifi-adopt"  # "" disables adoption
    state: str = "miaunifi-state.json"
    system_cfg: str = "system_cfg.txt"            # last config from the controller (has secrets)
    applied_state: str = "miaunifi-applied.state.json"  # last config applied (has secrets)
    backends: dict = field(default_factory=dict)

    def backend_options(self):
        return self.backends.get(self.backend, {})


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; start from config.example.json") from None
    except ValueError as e:
        raise ConfigError(f"{path}: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")

    known = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"{path}: unknown settings {', '.join(unknown)}")
    try:
        cfg = Config(**data)
    except TypeError:
        missing = sorted(f.name for f in dataclasses.fields(Config)
                         if f.default is dataclasses.MISSING
                         and f.default_factory is dataclasses.MISSING and f.name not in data)
        raise ConfigError(f"{path}: missing {', '.join(missing)}") from None

    if cfg.backend not in backends.NAMES:
        raise ConfigError(f"{path}: backend {cfg.backend!r} is not one of {', '.join(backends.NAMES)}")
    if not isinstance(cfg.interval, int) or cfg.interval <= 0:
        raise ConfigError(f"{path}: interval must be a positive number of seconds")
    if not isinstance(cfg.backends, dict) or not isinstance(cfg.backend_options(), dict):
        raise ConfigError(f"{path}: backends must map backend names to their options")
    return cfg
