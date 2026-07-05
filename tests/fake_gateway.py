"""Fake familiar gateway for iOS UI tests (run with the gateway venv python).

Speaks the WS leg of the protocol on :8999: expects an auth first frame,
pushes deck + permission + state, heartbeats every 2s (keeps the app's
silence watchdog quiet), and echoes every received cmd back as a notify the
UI test can assert on screen. Prints RX lines to stdout for wire asserts.

Usage: python fake_gateway.py [port]   (Ctrl-C / kill to stop)
"""
import json
import sys
import threading
import time

from websockets.sync.server import serve

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8999

STATE = {"type": "state", "total": 1, "running": 0, "waiting": 1, "msg": "fake gateway",
         "entries": [], "tokens_today": 42, "tools_today": 7,
         "job_state": "idle", "job_label": ""}


def handler(conn):
    first = json.loads(conn.recv(timeout=10))
    print("RX", json.dumps(first), flush=True)
    assert first.get("type") == "auth", first
    conn.send('{"type":"auth","ok":true}\n')
    conn.send(json.dumps({"type": "deck", "buttons": [
        {"i": 0, "label": "GOBTN", "color": "green", "confirm": False},
        {"i": 1, "label": "RISKBTN", "color": "red", "confirm": True}]}))
    conn.send(json.dumps({"type": "permission", "id": "sess_test",
                          "text": "rm -rf /tmp/x — allow?", "choices": ["once", "deny"]}))
    conn.send(json.dumps(STATE))

    stop = threading.Event()

    def beat():
        while not stop.is_set():
            time.sleep(2)
            try:
                conn.send(json.dumps(STATE))
            except Exception:
                return

    threading.Thread(target=beat, daemon=True).start()
    try:
        for msg in conn:
            evt = json.loads(msg)
            print("RX", json.dumps(evt, sort_keys=True), flush=True)
            if evt.get("cmd") == "deck":
                conn.send(json.dumps({"type": "notify", "msg": f"deck-ok-{evt['i']}"}))
            elif evt.get("cmd") == "permission":
                conn.send(json.dumps({"type": "notify", "msg": f"resolved-{evt['decision']}"}))
    finally:
        stop.set()


print(f"fake gateway on :{PORT}", flush=True)
serve(handler, "127.0.0.1", PORT).serve_forever()
