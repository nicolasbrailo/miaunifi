"""Runs the privileged adoption helper while the device is pending adoption.

The helper (helper/miaunifi_adopt.py) owns everything adoption needs and we
don't: a network identity for the device and a fake SSH server for the
controller to log into. We read its JSON events from stdout and stop it by
closing its stdin.
"""

import json
import logging
import queue
import shlex
import subprocess
import threading
import time

log = logging.getLogger("miaunifi.adopt")

RESTART_DELAY = 60


class AdoptHelper:
    def __init__(self, cmd, mac, controller):
        self.argv = shlex.split(cmd) + ["--mac", mac, "--controller", controller]
        self.proc = None
        self.events = queue.Queue()
        self.last_start = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Start the helper unless it is running or was started too recently."""
        if self.running():
            return
        if self.last_start is not None and time.monotonic() - self.last_start < RESTART_DELAY:
            return
        self.last_start = time.monotonic()
        log.info("starting adoption helper: %s", " ".join(self.argv))
        try:
            # stderr is inherited so the helper's logs show up next to ours
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, text=True)
        except OSError as e:
            log.error("can't start adoption helper: %s", e)
            self.proc = None
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                log.warning("helper: %s", line.rstrip())
        rc = proc.wait()
        self.events.put({"event": "exited", "rc": rc})

    def stop(self):
        if not self.running():
            return
        log.info("stopping adoption helper")
        self.proc.stdin.close()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(5)

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None
