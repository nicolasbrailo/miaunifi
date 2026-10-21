"""Adoption helper: pretends to be the device's SSH server while adoption is pending.

The UniFi controller adopts a device by SSHing into it (any credentials, it
tries ubnt/ubnt first) and running `syswrapper.sh set-adopt <url> <authkey>`.
This helper:

  1. creates a macvlan interface with the device's MAC and gets an IP via DHCP,
  2. runs a fake SSH server on that IP: it accepts any login, executes nothing,
     logs every command and captures the authkey from set-adopt,
  3. tears the interface down when stdin closes or on SIGTERM.

Runs as root (via sudo) and is installed root-owned; see install.sh. It is
standalone on purpose: it must not import code from a user-writable checkout.

Talks to the main service with one JSON object per line on stdout:
    {"event": "ready", "ip": "10.0.0.82"}
    {"event": "ssh_auth", "user": "ubnt", "peer": "10.0.0.10"}
    {"event": "ssh_cmd", "cmd": "..."}
    {"event": "adopted", "url": "http://...", "authkey": "..."}
    {"event": "error", "msg": "..."}
"""

import argparse
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import paramiko

log = logging.getLogger("miaunifi-adopt")

STATE_DIR = "/var/lib/miaunifi-adopt"
SET_ADOPT_RE = re.compile(r"set-adopt\s+(\S+)\s+([0-9a-fA-F]{32})\b")

_emit_lock = threading.Lock()


def emit(event, **fields):
    with _emit_lock:
        print(json.dumps({"event": event, **fields}), flush=True)


def run(*cmd):
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


# --- network identity -------------------------------------------------------

class MacvlanIdentity:
    """A macvlan interface with the device's MAC, configured via DHCP."""

    def __init__(self, ifname, mac, controller):
        self.ifname = ifname
        self.mac = mac
        self.controller = controller
        self.created = False
        self.dhcp = None

    def up(self, timeout=30):
        parent = json.loads(run("ip", "-j", "route", "get", self.controller))[0]["dev"]
        log.info("creating %s on %s with MAC %s", self.ifname, parent, self.mac)
        run("ip", "link", "add", self.ifname, "link", parent, "address", self.mac,
            "type", "macvlan", "mode", "bridge")
        self.created = True
        # Keep NetworkManager from running its own DHCP on it
        try:
            run("nmcli", "device", "set", self.ifname, "managed", "no")
        except (OSError, subprocess.CalledProcessError):
            pass
        run("ip", "link", "set", self.ifname, "up")
        # No default route, no DNS changes, low priority routes: this interface
        # must not change how the host itself reaches the network. dhcpcd runs
        # in the foreground as our child, so it lives exactly as long as we do.
        argv = ["dhcpcd", "-4", "--nobackground", "--nogateway", "--nohook", "resolv.conf",
                "--noipv4ll", "--metric", "4000", "--hostname=miaunifi", self.ifname]
        log.debug("run: %s", " ".join(argv))
        self.dhcp = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.dhcp.poll() is not None:
                # A dhcpcd manager already running takes over the interface and
                # our process exits right away, without waiting for a lease
                raise RuntimeError(
                    f"dhcpcd exited early (rc={self.dhcp.returncode}); if another dhcpcd "
                    f"manager is running (pgrep -a dhcpcd), stop it with: sudo dhcpcd -x")
            ip = self.ipv4()
            if ip:
                return ip
            time.sleep(0.5)
        raise RuntimeError(f"no DHCP lease on {self.ifname} after {timeout}s")

    def ipv4(self):
        for link in json.loads(run("ip", "-j", "-4", "addr", "show", "dev", self.ifname)):
            for a in link.get("addr_info", []):
                return a["local"]
        return None

    def down(self):
        if self.dhcp is not None and self.dhcp.poll() is None:
            try:
                run("dhcpcd", "-k", self.ifname)  # release the lease; the daemon then exits
                self.dhcp.wait(5)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                log.warning("dhcpcd release failed (%s), killing it", e)
                self.dhcp.terminate()
                self.dhcp.wait(5)
        self.dhcp = None
        if self.created:
            try:
                run("ip", "link", "del", self.ifname)
            except (OSError, subprocess.CalledProcessError) as e:
                log.warning("removing %s failed: %s", self.ifname, e)
            self.created = False
            log.info("removed %s", self.ifname)


# --- fake SSH server --------------------------------------------------------

class FakeDevice(paramiko.ServerInterface):
    """Accepts any login and records channel requests; never runs anything."""

    def __init__(self, peer):
        self.peer = peer
        self.requests = {}
        self.lock = threading.Lock()

    def request_queue(self, chanid):
        with self.lock:
            return self.requests.setdefault(chanid, [threading.Event(), None])

    def _request(self, channel, req):
        entry = self.request_queue(channel.get_id())
        entry[1] = req
        entry[0].set()
        return True

    def get_allowed_auths(self, username):
        return "password,keyboard-interactive"

    def check_auth_password(self, username, password):
        emit("ssh_auth", user=username, peer=self.peer)
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_env_request(self, *args):
        return True

    def check_channel_exec_request(self, channel, command):
        return self._request(channel, ("exec", command.decode(errors="replace")))

    def check_channel_shell_request(self, channel):
        return self._request(channel, ("shell", None))


def respond(cmd):
    """Output for a command the controller runs. Nothing is ever executed."""
    emit("ssh_cmd", cmd=cmd)
    m = SET_ADOPT_RE.search(cmd)
    if m:
        emit("adopted", url=m.group(1), authkey=m.group(2).lower())
    return ""


def serve_channel(server, chan):
    entry = server.request_queue(chan.get_id())
    if not entry[0].wait(10):
        chan.close()
        return
    kind, cmd = entry[1]
    try:
        if kind == "exec":
            chan.sendall(respond(cmd).encode())
        else:
            for line in chan.makefile("r"):
                line = line.strip()
                if line in ("exit", "logout"):
                    break
                if line:
                    chan.sendall(respond(line).encode())
        chan.send_exit_status(0)
    except (OSError, EOFError, paramiko.SSHException) as e:
        log.debug("channel error: %s", e)
    finally:
        chan.close()


def serve_connection(sock, peer, host_key):
    transport = paramiko.Transport(sock)
    transport.add_server_key(host_key)
    server = FakeDevice(peer)
    try:
        transport.start_server(server=server)
        while transport.is_active():
            chan = transport.accept(timeout=1)
            if chan is not None:
                threading.Thread(target=serve_channel, args=(server, chan), daemon=True).start()
    except (OSError, EOFError, paramiko.SSHException) as e:
        log.info("ssh session from %s ended: %s", peer, e)
    finally:
        transport.close()


def load_host_key(state_dir):
    path = os.path.join(state_dir, "ssh_host_rsa_key")
    try:
        return paramiko.RSAKey(filename=path)
    except FileNotFoundError:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(path)
        os.chmod(path, 0o600)
        return key


def serve(ip, port, host_key, stop):
    """Accept SSH connections on ip:port until stop is set."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((ip, port))
        srv.listen(4)
        srv.settimeout(1)
        log.info("fake ssh listening on %s:%d", ip, port)
        while not stop.is_set():
            try:
                sock, (peer, _) = srv.accept()
            except socket.timeout:
                continue
            log.info("ssh connection from %s", peer)
            threading.Thread(target=serve_connection, args=(sock, peer, host_key),
                             daemon=True).start()


# --- main -------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(prog="miaunifi-adopt")
    ap.add_argument("--mac", required=True)
    ap.add_argument("--controller", required=True, help="controller IPv4 address")
    ap.add_argument("--ifname", default="miau0")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # We run as root with arguments from an unprivileged caller: validate everything
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", args.mac.lower()):
        ap.error("bad --mac")
    args.mac = args.mac.lower()
    try:
        args.controller = str(ipaddress.IPv4Address(args.controller))
    except ValueError:
        ap.error("--controller must be an IPv4 address")
    if not re.fullmatch(r"[a-z][a-z0-9]{0,14}", args.ifname):
        ap.error("bad --ifname")
    if not 1 <= args.port <= 65535:
        ap.error("bad --port")
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s adopt-helper: %(message)s")
    if "ssh-rsa" not in paramiko.Transport._key_info:
        emit("error", msg=f"paramiko {paramiko.__version__} has no ssh-rsa, which the controller needs; use paramiko<5")
        return 1

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    # The main service closes our stdin to stop us; also covers it dying
    def watch_stdin():
        sys.stdin.read()
        stop.set()
    threading.Thread(target=watch_stdin, daemon=True).start()

    identity = MacvlanIdentity(args.ifname, args.mac, args.controller)
    try:
        host_key = load_host_key(STATE_DIR)
        ip = identity.up()
        emit("ready", ip=ip)
        serve(ip, args.port, host_key, stop)
    except Exception as e:
        log.exception("adopt helper failed")
        emit("error", msg=str(e))
        return 1
    finally:
        identity.down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
