---
name: familiar
description: Operate the Hermes Familiar desk device — notify it, configure its button deck, provision Wi-Fi, run setup, and troubleshoot the link.
version: 1.0.0
author: Jason Brashear
license: MIT
platforms: [macos, linux]
prerequisites:
  commands: [hermes]
metadata:
  hermes:
    tags: [familiar, hardware, esp32, desk, notify, deck]
    related_skills: []
---

# Hermes Familiar Operator

Use this skill when the operator asks you to do anything with **the familiar** —
the ESP32-S3 desk companion device wired to this gateway. You (Hermes) can reach
it, configure it, and help set it up. The device runs the `familiar` plugin; you
have the `familiar_notify` tool and the `/familiar` command.

## When To Use

- The operator says "ping my desk", "tell the familiar", "make her say…", or
  wants the device to get their attention.
- "Add a button", "set up my deck", "put X on the familiar" → edit the deck.
- "Connect the familiar to Wi-Fi", "provision the device", "set it up".
- The device link seems down, or voice/banner isn't reaching it.
- Anything referencing the desk device, the buddy, or buildafamiliar.com.

## Reaching the desk (familiar_notify)

Use the `familiar_notify` tool proactively when the operator is away from chat
and something deserves their attention: a long task finished, a scheduled job
found something, you are blocked waiting on them, an approval is pending.

- `message` — under ~100 chars, plain text.
- `sound` — `alert` (default), `ack`, `tap`, or `none`.
- `speak: true` — also say it aloud (needs a speaker + Wi-Fi; degrades to
  banner+chirp if either is missing).

Quiet hours (if the operator set `voice.quiet`) suppress sound automatically;
the banner still lands. Don't spam — one notify per genuinely notable event.

## The button deck (Stream-Deck style)

The deck lives in `~/.hermes/familiar_actions.json` under `actions`. Every
**enabled** action becomes a button on the device's OPS tab (max 6, 3×2 grid).
Edits go live within ~60s — no restart. Each action:

```json
{
  "id": "unique_id",
  "label": "DEPLOY",            // ≤8 chars, shown uppercase
  "enabled": true,
  "color": "red",              // green | amber | red | cyan
  "confirm": true,             // optional: two-tap arm for dangerous buttons
  "prompt": "Redeploy the site and confirm it's live."   // OR "command": [...]
}
```

- **`prompt`** — fired at Hermes as a task (anything the operator would ask you).
- **`command`** — an argv array run as a subprocess on the gateway machine
  (scripts, git, curl, deploys). Use `["/bin/bash","-lc","…"]` for shell.
- Exactly one of `prompt` / `command`. `confirm: true` for anything destructive.
- One job runs at a time; the running button shows STOP and a second tap cancels.

To add a button: read the file, append an action (or set `enabled: true`),
write it back, keep ≤6 enabled. Confirm to the operator what button you added.

## Wi-Fi / setup provisioning (over USB)

You can configure the device by sending a JSON line to its serial port
(115200 baud, newline-terminated). The plugin owns the port while the gateway
runs, so provisioning is normally done via the plugin, but the raw frames are:

- Wi-Fi: `{"type":"config","wifi":{"ssid":"…","password":"…"}}` →
  acks `{"ack":"config","ok":true}` then `{"wifi":"ok","ip":"…"}`.
- Rotation: `{"type":"config","display":{"rotation":1}}` (1 landscape, 3 flipped).

Ask the operator for the SSID/password of a network that shares a LAN with this
machine (voice needs the device and gateway on the same subnet). Full runbook:
https://buildafamiliar.com/agent.md

## Network transports (phones + untethered device)

The plugin also serves the same newline-JSON protocol over the network,
configured in the `transport` block of `~/.hermes/familiar_actions.json`
(`{"enabled":true,"port":8767,"ws_port":8768,"token":"<secret>"}`):

- TCP :8767 — the device dials home when USB goes silent.
- WebSocket :8768 — the Pocket Familiar phone app / browsers.
- With `token` set (it should be — network clients can approve dangerous
  commands), a client's first message must be `{"type":"auth","token":"…"}`
  or it is dropped; success is acked with `{"type":"auth","ok":true}`.

## Troubleshooting

- **Link status:** `/familiar` shows connected/searching, transport
  (`usb:<port>`, `tcp`, `ws`, or combinations like `usb:...+ws`), pending
  approvals, and job state.
- **Test it:** `/familiar ping` (banner + chirp) or `/familiar say hello`
  (speech — needs speaker + Wi-Fi).
- **Not connecting:** the plugin autodetects on any `/dev/cu.usbmodem*` /
  `/dev/ttyACM*` and self-heals a wedged USB port; a replug or 60s wait usually
  fixes it. Check `grep familiar ~/.hermes/logs/agent.log`.
- **Silent audio but banners work:** confirm a speaker is physically attached
  (8Ω 1W into the board jack) and the device is on Wi-Fi — the whole audio path
  can verify green in software with no speaker plugged in.

## What NOT to do

- Don't flash firmware or restart the gateway unless the operator asks — both
  interrupt the device link.
- Don't put more than 6 enabled actions on the deck (extras are ignored).
- Don't notify on routine turns — the desk is for things that need attention.
