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

import atexit
import glob
import hmac
import json
import logging
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("familiar.serial")

_PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")
_PROBE_SECS = 2.0
_RESCAN_SECS = 3.0
# ESP32-S3 USB-CDC can wedge (deaf serial, Wi-Fi fine) when the host process
# dies abruptly while holding the port. An esptool-level reset revives it —
# after this many consecutive failed scan rounds with a port present, try it.
_UNWEDGE_AFTER_FAILS = 8
_UNWEDGE_COOLDOWN = 300.0


def _find_esptool() -> str | None:
    for cand in sorted(Path.home().glob(".platformio/packages/tool-esptoolpy*/esptool.py")):
        return str(cand)
    return None


class SerialLink:
    """Owns the serial port. ``on_line(dict, origin)`` is called from the link
    thread for every JSON line a client sends (origin: "usb" | "tcp" | "ws");
    ``make_heartbeat()`` (if given) must return a dict to transmit every
    ``heartbeat`` seconds while connected."""

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
        self._scan_fails = 0
        self._last_unwedge = 0.0
        # network surfaces (device-over-TCP, phones-over-WS): (kind, send_fn)
        self._net_clients: list = []
        self._net_lock = threading.Lock()
        self._last_net_beat = 0.0
        # optional () -> list[dict]: snapshot frames pushed to each client on
        # connect (deck layout, pending approvals, …) — broadcast only carries
        # what happens AFTER a client joins
        self.on_client = None
        self.port: str | None = None      # active port when connected

    # -- public ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ser is not None or bool(self._net_clients)

    @property
    def transport(self) -> str:
        with self._net_lock:
            kinds = sorted({k for k, _ in self._net_clients})
        if self._ser is not None:
            return "+".join([f"usb:{self.port}"] + kinds)
        return "+".join(kinds) if kinds else "none"

    def send(self, obj: dict, leg: str | None = None) -> None:
        """Queue a frame. Never blocks; drops oldest on overflow.

        leg: None = every surface; "desk" = USB serial + device-over-TCP;
        "phone" = WS clients only. Lets the host send per-surface variants
        (e.g. a silent notify to the desk while the phone gets the loud one).
        """
        item = (leg, obj)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
            except queue.Full:
                pass

    def start(self) -> None:
        atexit.register(self._drop)   # release the port with proper line-state on exit
        threading.Thread(target=self._run, name="familiar-serial", daemon=True).start()

    # -- network legs (untethered device via TCP, phones/PWA via WS) ------
    # Same newline-JSON protocol on both. Every outbound frame is broadcast
    # to all authed network clients, even while USB is up (a phone is a
    # second surface, not a fallback). With transport.token set, a client's
    # FIRST line/message must be {"type":"auth","token":"…"} or it is dropped
    # — a network client can approve destructive commands.

    @staticmethod
    def _auth_ok(raw, token: str) -> bool:
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            evt = json.loads(raw)
        except Exception:
            return False
        return (isinstance(evt, dict) and evt.get("type") == "auth"
                and hmac.compare_digest(str(evt.get("token") or ""), token))

    def _net_add(self, kind: str, sender) -> None:
        with self._net_lock:
            self._net_clients.append((kind, sender))

    def _net_del(self, sender) -> None:
        with self._net_lock:
            self._net_clients = [c for c in self._net_clients if c[1] is not sender]

    def _send_welcome(self, sender) -> None:
        cb = self.on_client
        if not cb:
            return
        try:
            for frame in cb() or []:
                sender((json.dumps(frame, separators=(",", ":")) + "\n").encode())
        except Exception:
            logger.exception("familiar welcome push failed")

    def start_tcp(self, port: int, token: str | None = None):
        """Newline-JSON TCP listener. Returns the bound port, or None."""
        link = self

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self):
                peer = f"{self.client_address[0]}:{self.client_address[1]}"
                if token and not link._auth_ok(self.rfile.readline(), token):
                    logger.warning("familiar tcp auth failed from %s", peer)
                    return
                w = self.wfile

                def sender(data: bytes) -> None:
                    w.write(data)
                    w.flush()

                if token:
                    sender(b'{"type":"auth","ok":true}\n')
                link._net_add("tcp", sender)
                link._send_welcome(sender)
                logger.info("familiar connected via tcp %s", peer)
                try:
                    for raw in self.rfile:
                        try:
                            evt = json.loads(raw.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if isinstance(evt, dict):
                            try:
                                link._on_line(evt, "tcp")
                            except Exception:
                                logger.exception("familiar tcp on_line failed")
                finally:
                    link._net_del(sender)
                    logger.info("familiar tcp %s disconnected", peer)

        class _Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            srv = _Srv(("0.0.0.0", int(port)), _Handler)
        except OSError as e:
            logger.warning("familiar tcp port %s unavailable: %s", port, e)
            return None
        threading.Thread(target=srv.serve_forever, name="familiar-tcp", daemon=True).start()
        bound = srv.server_address[1]
        logger.info("familiar tcp transport listening on :%s%s", bound,
                    "" if token else " (NO TOKEN — open)")
        return bound

    def start_ws(self, port: int, token: str | None = None):
        """WebSocket listener speaking the identical frames (for phones/PWA).
        Returns the bound port, or None (missing lib / port busy)."""
        link = self
        try:
            from websockets.sync.server import serve
        except ImportError:
            logger.warning("websockets not installed in gateway venv — familiar ws transport disabled")
            return None

        def handler(conn):
            try:
                peer = "%s:%s" % conn.remote_address[:2]
            except Exception:
                peer = "?"
            if token:
                try:
                    first = conn.recv(timeout=10)
                except Exception:
                    return
                if not link._auth_ok(first, token):
                    logger.warning("familiar ws auth failed from %s", peer)
                    return
                try:
                    conn.send('{"type":"auth","ok":true}\n')
                except Exception:
                    return

            def sender(data: bytes) -> None:
                conn.send(data.decode("utf-8"))

            link._net_add("ws", sender)
            link._send_welcome(sender)
            logger.info("familiar connected via ws %s", peer)
            try:
                for msg in conn:
                    try:
                        evt = json.loads(msg)
                    except Exception:
                        continue
                    if isinstance(evt, dict):
                        try:
                            link._on_line(evt, "ws")
                        except Exception:
                            logger.exception("familiar ws on_line failed")
            except Exception:
                pass
            finally:
                link._net_del(sender)
                logger.info("familiar ws %s disconnected", peer)

        try:
            srv = serve(handler, "0.0.0.0", int(port))
        except OSError as e:
            logger.warning("familiar ws port %s unavailable: %s", port, e)
            return None
        threading.Thread(target=srv.serve_forever, name="familiar-ws", daemon=True).start()
        bound = srv.socket.getsockname()[1]
        logger.info("familiar ws transport listening on :%s%s", bound,
                    "" if token else " (NO TOKEN — open)")
        return bound

    _LEG_KINDS = {None: ("tcp", "ws"), "desk": ("tcp",), "phone": ("ws",)}

    def _net_send(self, data: bytes, kinds=("tcp", "ws")) -> bool:
        with self._net_lock:
            clients = list(self._net_clients)
        sent = False
        for kind, send_fn in clients:
            if kind not in kinds:
                continue
            try:
                send_fn(data)
                sent = True
            except Exception:
                self._net_del(send_fn)
        return sent

    def _try_unwedge(self, port: str) -> None:
        """Hard-reset a present-but-deaf device via esptool (rate-limited)."""
        now = time.time()
        if now - self._last_unwedge < _UNWEDGE_COOLDOWN:
            return
        self._last_unwedge = now
        esptool = _find_esptool()
        if not esptool:
            logger.warning("familiar port %s deaf but esptool not found — replug to recover", port)
            return
        logger.warning("familiar port %s deaf — attempting esptool hard reset", port)
        try:
            subprocess.run(
                [sys.executable, esptool, "--port", port,
                 "--before", "default_reset", "--after", "hard_reset", "chip_id"],
                capture_output=True, timeout=45)
        except Exception as e:
            logger.warning("familiar unwedge attempt failed: %s", e)

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
        candidates = self._candidates()
        for port in candidates:
            ser = self._probe(serial_mod, port)
            if ser is not None:
                self._ser = ser
                self.port = port
                self._scan_fails = 0
                logger.info("familiar connected on %s", port)
                return
        if candidates:
            self._scan_fails += 1
            if self._scan_fails >= _UNWEDGE_AFTER_FAILS:
                self._scan_fails = 0
                self._try_unwedge(candidates[0])

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
                    # no USB — drain the queue to network clients only
                    while not self._q.empty():
                        try:
                            leg, obj = self._q.get_nowait()
                        except queue.Empty:
                            break
                        self._net_send((json.dumps(obj, separators=(",", ":")) + "\n").encode(),
                                       self._LEG_KINDS.get(leg, ("tcp", "ws")))
                    now = time.time()
                    if self._make_heartbeat and self._net_clients and now - self._last_net_beat >= self._heartbeat:
                        self._last_net_beat = now
                        hb = self._make_heartbeat()
                        if hb:
                            self._net_send((json.dumps(hb, separators=(",", ":")) + "\n").encode())
                    time.sleep(1.0 if self._net_clients else _RESCAN_SECS)
                    continue
                buf = b""
                last_beat = 0.0
            try:
                # outbound — serial plus every network surface (leg-filtered)
                while True:
                    try:
                        leg, obj = self._q.get_nowait()
                    except queue.Empty:
                        break
                    data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
                    if leg in (None, "desk"):
                        self._ser.write(data)
                    self._net_send(data, self._LEG_KINDS.get(leg, ("tcp", "ws")))
                self._ser.flush()
                # heartbeat
                now = time.time()
                if self._make_heartbeat and now - last_beat >= self._heartbeat:
                    last_beat = now
                    hb = self._make_heartbeat()
                    if hb:
                        data = (json.dumps(hb, separators=(",", ":")) + "\n").encode()
                        self._ser.write(data)
                        self._ser.flush()
                        self._net_send(data)
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
                                self._on_line(evt, "usb")
                            except Exception:
                                logger.exception("familiar on_line handler failed")
            except Exception:
                self._drop()
                time.sleep(_RESCAN_SECS)
