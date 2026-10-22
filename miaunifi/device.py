"""What our fake device reports to the controller.

We pose as a UAP-AC-Pro (U7PG2). Data is static for now; later it comes from
the OpenWrt router via ubus/iwinfo.
"""

import time

MODEL = "U7PG2"
MODEL_DISPLAY = "UAP-AC-Pro-Gen2"
VERSION = "6.6.55.15189"

_BOOT = time.time()


def inform_payload(mac, ip, hostname, inform_url, cfgversion=None, netmask="255.255.255.0",
                   report=None):
    """report: state fields from unifi_report (vap_table, sys_stats, ...); they win."""
    payload = _base_payload(mac, ip, hostname, inform_url, cfgversion, netmask)
    payload.update(report or {})
    return payload


def _base_payload(mac, ip, hostname, inform_url, cfgversion, netmask):
    uptime = int(time.time() - _BOOT)
    return {
        "mac": mac,
        "serial": mac.replace(":", "").upper(),
        "model": MODEL,
        "model_display": MODEL_DISPLAY,
        "board_rev": 21,
        "version": VERSION,
        "required_version": "3.4.1",
        "bootrom_version": "unifi-v1.6.7.249-gb74e0282",
        "hostname": hostname,
        "ip": ip,
        "netmask": netmask,
        "inform_url": inform_url,
        "inform_ip": inform_url.split("//", 1)[-1].split(":", 1)[0],
        # default=True means "factory default, not adopted"
        "default": cfgversion is None,
        "cfgversion": cfgversion or "?",
        "state": 1,
        "isolated": False,
        "locating": False,
        "discovery_response": False,
        "selfrun_beacon": False,
        "fw_caps": 0,
        "has_eth1": False,
        "has_speaker": False,
        "time": int(time.time()),
        "uptime": uptime,
        "uplink": "eth0",
        "if_table": [
            {
                "name": "eth0",
                "mac": mac,
                "ip": ip,
                "netmask": netmask,
                "up": True,
                "full_duplex": True,
                "speed": 1000,
            }
        ],
        "radio_table": [
            {"name": "wifi0", "radio": "ng", "builtin_ant_gain": 3, "max_txpower": 22, "min_txpower": 6, "nss": 3},
            {"name": "wifi1", "radio": "na", "builtin_ant_gain": 3, "max_txpower": 22, "min_txpower": 6, "nss": 3,
             "is_11ac": True, "has_dfs": True},
        ],
        "vap_table": [],
    }
