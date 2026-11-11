"""Backends: one MiaunifiCommand implementation per kind of device."""

from .asus_rt_ac58u_v3 import AsusRtAc58uV3
from .local import LocalBackend

CLASSES = {"local": LocalBackend, "asus_rt_ac58u_v3": AsusRtAc58uV3}
NAMES = tuple(CLASSES)


def make(name, options):
    """options: the backend's constructor arguments, from config.json."""
    try:
        return CLASSES[name](**options)
    except TypeError as e:
        raise ValueError(f"bad options for backend {name!r}: {e}") from None
