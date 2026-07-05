# Familiar setup runbook (agent-readable)

You are an AI agent (likely Hermes) setting up a "familiar" — an ESP32-S3 desk
companion device — for your operator. Their machine runs a Hermes gateway.
Follow these steps in order. Each step has a verification; do not proceed past
a failed verification without telling your operator.

## Preconditions

- The device is flashed (buildafamiliar.com/flash.html) and connected by USB
  to the machine where your gateway runs.
- You can run shell commands on that machine.

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
machine. Then, with the gateway STOPPED (`hermes gateway stop`), send over
serial (115200 baud, newline-terminated JSON) to the device port:

```json
{"type":"config","wifi":{"ssid":"<SSID>","password":"<PASSWORD>"}}
```

Expect `{"ack":"config","ok":true}` then `{"wifi":"ok","ip":"<addr>"}`.
Restart the gateway (`hermes gateway start`).

Verify: `curl http://<device-ip>/status` returns JSON with `"wifi":"WiFi:<ip>"`.

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
github.com/webdevtodayjason/hermes
