# Familiar setup runbook (agent-readable)

You are an AI agent (likely Hermes) setting up a "familiar" — an ESP32-S3 desk
companion device — for your operator. Their machine runs a Hermes gateway.
Follow these steps in order. Each step has a verification; do not proceed past
a failed verification without telling your operator.

## Preconditions

- The device is flashed (https://buildafamiliar.com/flash.html) and connected by
  USB to the machine where the Hermes gateway runs.
- You can run shell commands on that machine.
- Voice requires the familiar to have a speaker connected and to be provisioned
  onto the same LAN as the gateway. Banner/chirp still work over USB without
  Wi-Fi.

## Step 1 — install the plugin

```bash
hermes plugins install webdevtodayjason/hermes/plugin --enable
~/.hermes/hermes-agent/venv/bin/pip install pyserial
```

Verify: `hermes plugins list` shows `familiar | enabled`.

## Step 2 — restart the gateway

```bash
hermes gateway restart
```

Verify (within ~30s): the gateway log contains `familiar connected on /dev/...`:

```bash
grep -a "familiar" ~/.hermes/logs/agent.log | tail -5
```

If it shows `familiar serial link started` but no `connected`: the device may
need a replug, or another process holds the port. The plugin self-recovers;
wait 60s and re-check before escalating.

## Step 3 — provision Wi-Fi (enables voice)

Ask your operator for the 2.4/5GHz SSID + password that shares a LAN with this
machine, or ask them to provide those values through their preferred secret
handling method. Do not log or repeat the password back.

With the gateway stopped (`hermes gateway stop`), discover the device port:

```bash
ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null
```

Then send newline-terminated JSON over serial at 115200 baud:

```json
{"type":"config","wifi":{"ssid":"<SSID>","password":"<PASSWORD>"}}
```

Concrete provisioning helper:

```bash
python3 - <<'PY'
import getpass, json, sys, time
try:
    import serial
except ImportError:
    raise SystemExit("pyserial missing; run: ~/.hermes/hermes-agent/venv/bin/pip install pyserial")

port = input("Device port (/dev/cu.usbmodem... or /dev/ttyACM...): ").strip()
ssid = input("Wi-Fi SSID: ").strip()
password = getpass.getpass("Wi-Fi password: ")
payload = {"type": "config", "wifi": {"ssid": ssid, "password": password}}

with serial.Serial(port, 115200, timeout=2) as s:
    s.write((json.dumps(payload) + "\n").encode())
    deadline = time.time() + 45
    saw_wifi = False
    while time.time() < deadline:
        line = s.readline().decode(errors="replace").strip()
        if not line:
            continue
        print(line)
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("ack") == "config" and msg.get("ok") is not True:
            sys.exit("Device rejected config")
        if msg.get("wifi") == "ok":
            saw_wifi = True
            break
    if not saw_wifi:
        sys.exit("Timed out waiting for wifi ok")
PY
```

Expect `{"ack":"config","ok":true}` then `{"wifi":"ok","ip":"<addr>"}`.
Restart the gateway (`hermes gateway start`).

Verify: `curl http://<device-ip>/status` returns JSON with
`"wifi":"WiFi:<ip>"`.

## Step 4 — self-test

- `/familiar` in any Hermes chat → link status line
- `/familiar ping` → device shows a banner and chirps
- `/familiar say hello operator` → device speaks (requires speaker + Wi-Fi)

## Step 5 — tell your operator

Report: link state, device IP, whether speech played, and that the
`familiar_notify` tool is now available to you. Use it whenever you need
their attention at the desk: long task done, blocked, approval waiting.

## Config reference

`~/.hermes/familiar_actions.json` — action buttons, voice enable/port,
ritual time (`{"ritual":{"time":"18:30"}}`), loom board path. Full docs:
https://github.com/webdevtodayjason/hermes
