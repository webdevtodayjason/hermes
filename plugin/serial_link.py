"""serial_link — resilient USB serial transport for the Hermes Familiar.

One daemon thread owns all serial I/O:

    scan/probe -> connected -> drain outbound queue + read lines + heartbeat
        ^                                   |
        +--------- any I/O error -----------+

Port autodetect: try the configured port first, else every ``/dev/cu.usbmodem*``
(macOS) / ``/dev/ttyACM*`` (Linux). A candidate is accepted when it answers a
``{"cmd":"ping"}`` with any parseable JSON line within the probe window — the
familiar replies ``{"ack":"ping","ok":true}`` immediately.

Hook threads never block on the device: ``send()`` puts on a bounded queue and
drops the oldest state frame when full. pyserial missing or no device present
is a quiet no-op (logged once) so the plugin can never hurt the gateway.
"""
from __future__ import annotations

import glob
import json
import logging
import queue
import threading
import time

logger = logging.getLogger("familiar.serial")

_PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")
_PROBE_SECS = 2.0
_RESCAN_SECS = 3.0


class SerialLink:
    """Owns the serial port. ``on_line(dict)`` is called from the link thread
    for every JSON line the device sends; ``make_heartbeat()`` (if given) must
    return a dict to transmit every ``heartbeat`` seconds while connected."""

    def __init__(self, on_line, port: str | None = None, baud: int = 115200,
                 heartbeat: float = 5.0, make_heartbeat=None):
        self._on_line = on_line
        self._cfg_port = (port or "").strip() or None
        self._baud = int(baud)
        self._heartbeat = float(heartbeat)
        self._make_heartbeat = make_heartbeat
        self._q: queue.Queue[dict] = queue.Queue(maxsize=64)
        self._ser = None
        self._stop = threading.Event()
        self._warned_pyserial = False
        self.port: str | None = None      # active port when connected

    # -- public ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def send(self, obj: dict) -> None:
        """Queue a frame for the device. Never blocks; drops oldest on overflow."""
        try:
            self._q.put_nowait(obj)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(obj)
            except queue.Full:
                pass

    def start(self) -> None:
        threading.Thread(target=self._run, name="familiar-serial", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- link thread -------------------------------------------------------

    def _serial_module(self):
        try:
            import serial  # pyserial
            return serial
        except ImportError:
            if not self._warned_pyserial:
                logger.warning("pyserial not installed — familiar device link disabled "
                               "(run install.sh or: pip install pyserial into the gateway venv)")
                self._warned_pyserial = True
            return None

    def _candidates(self) -> list[str]:
        if self._cfg_port:
            return [self._cfg_port]
        found: list[str] = []
        for g in _PORT_GLOBS:
            found.extend(sorted(glob.glob(g)))
        return found

    def _probe(self, serial_mod, port: str):
        """Open *port* and return the handle iff something JSON-speaking answers."""
        try:
            # exclusive: flock the port so no second Hermes process can grab it
            ser = serial_mod.Serial(port, self._baud, timeout=0.05, exclusive=True)
        except (ValueError, TypeError):
            try:
                ser = serial_mod.Serial(port, self._baud, timeout=0.05)
            except Exception:
                return None
        except Exception:
            return None
        try:
            ser.reset_input_buffer()
            ser.write(b'{"cmd":"ping"}\n')
            ser.flush()
            deadline = time.time() + _PROBE_SECS
            while time.time() < deadline:
                raw = ser.readline()
                if raw:
                    try:
                        json.loads(raw.decode("utf-8", "replace"))
                        return ser
                    except Exception:
                        pass  # boot noise; keep listening
            ser.close()
        except Exception:
            try:
                ser.close()
            except Exception:
                pass
        return None

    def _connect(self) -> None:
        serial_mod = self._serial_module()
        if serial_mod is None:
            return
        for port in self._candidates():
            ser = self._probe(serial_mod, port)
            if ser is not None:
                self._ser = ser
                self.port = port
                logger.info("familiar connected on %s", port)
                return

    def _drop(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            logger.info("familiar disconnected from %s", self.port)
        self._ser = None
        self.port = None

    def _run(self) -> None:
        buf = b""
        last_beat = 0.0
        while not self._stop.is_set():
            if self._ser is None:
                self._connect()
                if self._ser is None:
                    # nothing plugged in — nap, but keep the queue from growing stale
                    while not self._q.empty():
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            break
                    time.sleep(_RESCAN_SECS)
                    continue
                buf = b""
                last_beat = 0.0
            try:
                # outbound
                while True:
                    try:
                        obj = self._q.get_nowait()
                    except queue.Empty:
                        break
                    self._ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
                self._ser.flush()
                # heartbeat
                now = time.time()
                if self._make_heartbeat and now - last_beat >= self._heartbeat:
                    last_beat = now
                    hb = self._make_heartbeat()
                    if hb:
                        self._ser.write((json.dumps(hb, separators=(",", ":")) + "\n").encode())
                        self._ser.flush()
                # inbound (readline honors timeout=0.05, so this loop stays responsive)
                raw = self._ser.readline()
                if raw:
                    buf += raw
                    if buf.endswith(b"\n"):
                        line, buf = buf, b""
                        try:
                            evt = json.loads(line.decode("utf-8", "replace"))
                        except Exception:
                            evt = None
                        if isinstance(evt, dict):
                            try:
                                self._on_line(evt)
                            except Exception:
                                logger.exception("familiar on_line handler failed")
            except Exception:
                self._drop()
                time.sleep(_RESCAN_SECS)
