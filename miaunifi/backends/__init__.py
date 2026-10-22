"""Backends: one MiaunifiCommand implementation per kind of device."""

from .asus_rt_ac58u_v3 import AsusRtAc58uV3
from .local import LocalBackend


def make(name, args):
    if name == "local":
        return LocalBackend(fake_clients=args.fake_clients)
    if name == "asus_rt_ac58u_v3":
        return AsusRtAc58uV3(host=args.asus_host, state_path=args.asus_state)
    raise ValueError(f"unknown backend {name!r}")


NAMES = ("local", "asus_rt_ac58u_v3")
