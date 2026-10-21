"""Inform loop: periodically report to the controller and log its replies."""

import argparse
import http.client
import json
import logging
import os
import random
import socket
import time
from urllib.parse import urlparse

from . import device, packet
from .adopt import AdoptHelper

log = logging.getLogger("miaunifi")


def local_ip_for(host):
    """IP of the interface we'd use to reach host."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 8080))
        return s.getsockname()[0]


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def random_mac():
    # Locally administered, unicast
    octets = [random.randint(0, 255) for _ in range(6)]
    octets[0] = (octets[0] & 0xFC) | 0x02
    return ":".join(f"{o:02x}" for o in octets)


def parse_cfg(text):
    """Parse the controller's key=value config blobs (mgmt_cfg, system_cfg)."""
    cfg = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            cfg[k.strip()] = v.strip()
    return cfg


def handle_setparam(reply, state, state_path):
    """Apply a setparam reply. Returns True if state changed."""
    if "mgmt_cfg" not in reply:
        log.info("setparam without mgmt_cfg, ignoring keys: %s", sorted(reply))
        return False
    mgmt = parse_cfg(reply["mgmt_cfg"])
    log.debug("mgmt_cfg: %s", mgmt)
    changed = False
    for src, dst in (("authkey", "authkey"), ("cfgversion", "cfgversion"),
                     ("use_aes_gcm", "use_aes_gcm")):
        if src in mgmt and state.get(dst) != mgmt[src]:
            state[dst] = mgmt[src]
            changed = True
    if changed:
        save_state(state_path, state)
        log.info("adopted: got authkey, cfgversion=%s, gcm=%s",
                 state.get("cfgversion"), state.get("use_aes_gcm"))
    return changed


def send_inform(url, mac, payload, key, gcm=False, source_ip=None):
    u = urlparse(url)
    body = packet.encode(mac, json.dumps(payload).encode(), key, gcm)
    conn = http.client.HTTPConnection(u.hostname, u.port or 8080, timeout=10,
                                      source_address=(source_ip, 0) if source_ip else None)
    try:
        conn.request("POST", u.path or "/inform", body, {
            "Content-Type": "application/x-binary",
            "User-Agent": "AirControl Agent v1.0",
        })
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(prog="miaunifi")
    ap.add_argument("--inform-url", default="http://10.0.0.10:8080/inform")
    ap.add_argument("--hostname", default="Miaunifi device")
    ap.add_argument("--mac", help="device MAC (default: random, persisted in state file)")
    ap.add_argument("--state", default="miaunifi-state.json")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--once", action="store_true", help="send a single inform and exit")
    ap.add_argument("--adopt-helper", default="sudo -n /usr/local/bin/miaunifi-adopt",
                    help="command to run the adoption helper while pending; empty disables it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    state = load_state(args.state)
    if args.mac:
        state["mac"] = args.mac.lower()
    state.setdefault("mac", random_mac())
    save_state(args.state, state)

    mac = state["mac"]
    url = state.get("inform_url", args.inform_url)
    host_ip = local_ip_for(urlparse(url).hostname)
    log.info("device %s (%s) at %s -> %s (%s)", mac, device.MODEL, host_ip, url,
             "adopted" if "authkey" in state else "not adopted")

    helper = None
    if args.adopt_helper and not args.once:
        helper = AdoptHelper(args.adopt_helper, mac, urlparse(url).hostname)
    # While the helper runs, the device lives at the helper's IP: the
    # controller SSHes there to adopt us, so informs must report (and come from) it
    device_ip = None

    interval = args.interval
    pending = False
    try:
        while True:
            key = bytes.fromhex(state["authkey"]) if "authkey" in state else packet.DEFAULT_KEY
            gcm = state.get("use_aes_gcm") == "true"
            payload = device.inform_payload(mac, device_ip or host_ip, args.hostname, url,
                                            state.get("cfgversion"))
            log.debug("inform: %s", json.dumps(payload))
            try:
                status, body = send_inform(url, mac, payload, key, gcm, device_ip)
                # The controller answers 404 to every inform until the device is adopted
                if status == 404:
                    if not pending:
                        log.info("pending adoption (controller replies 404 until adopted)")
                        pending = True
                    else:
                        log.debug("still pending adoption")
                    if helper:
                        helper.start()
                elif status != 200:
                    log.warning("controller replied HTTP %d (%d bytes)", status, len(body))
                else:
                    pending = False
                    _, flags, data = packet.decode(body, key)
                    reply = json.loads(data)
                    rtype = reply.get("_type")
                    log.info("reply %s (flags=%#x)", rtype, flags)
                    log.debug("reply: %s", json.dumps(reply))
                    if rtype == "noop" and "interval" in reply:
                        interval = int(reply["interval"])
                    elif rtype == "setparam":
                        handle_setparam(reply, state, args.state)
                    else:
                        log.info("unhandled reply: %s", json.dumps(reply))
                    if helper and helper.running() and "authkey" in state:
                        helper.stop()
                        device_ip = None
            except (OSError, packet.PacketError, ValueError) as e:
                log.error("inform failed: %s", e)

            if args.once:
                break

            # Sleep until the next inform, handling helper events meanwhile
            deadline = time.monotonic() + interval
            while (left := deadline - time.monotonic()) > 0:
                if helper is None:
                    time.sleep(left)
                    break
                ev = helper.next_event(left)
                if ev is None:
                    break
                kind = ev.get("event")
                if kind == "ready":
                    device_ip = ev["ip"]
                    log.info("adoption helper ready, device now at %s", device_ip)
                elif kind == "adopted":
                    log.info("controller sent set-adopt (inform url %s)", ev["url"])
                    state["authkey"] = ev["authkey"]
                    state["inform_url"] = url = ev["url"]
                    save_state(args.state, state)
                    break  # inform right away with the new key
                elif kind == "exited":
                    log.warning("adoption helper exited (rc=%s)", ev["rc"])
                    device_ip = None
                elif kind == "error":
                    log.error("adoption helper: %s", ev["msg"])
                else:
                    log.info("adoption helper: %s", json.dumps(ev))
    finally:
        if helper:
            helper.stop()


if __name__ == "__main__":
    main()
