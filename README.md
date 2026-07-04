# Hermes Familiar for Waveshare ESP32-S3-Touch-LCD-2.8

A physical Hermes Agent familiar: green-phosphor portrait, SD-card animations, touch controls, and a bidirectional USB serial bridge into Hermes activity.

## Verified hardware

- Board: Waveshare ESP32-S3-Touch-LCD-2.8
- USB serial: `/dev/cu.usbmodem101`
- ESP32-S3 MAC seen during flash: `28:37:2f:88:dc:3c`
- Display: ST7789, 240x320
- Touch: CST328 over I2C, verified with vendor `0xCACA` check
  - SDA GPIO1, SCL GPIO3, INT GPIO4, RST GPIO2, addr `0x1A`
- SD/TF card: SD_MMC 1-bit, verified with 128GB FAT32 card
  - CLK GPIO14, CMD GPIO17, D0 GPIO16, D3_EN GPIO21
- Power latch:
  - input GPIO6, hold GPIO7

## Current features

- SD-card `.raw4` portrait frame loading from `/hermes-buddy/frames/...`
- Fallback compiled frames if SD assets are missing
- Correct calibrated color path:
  - LCD inversion ON
  - RGB565 primitives normal
  - `pushImage()` buffers use `setSwapBytes(true)`
- Random idle blinking
- One-shot touch expressions:
  - wink hold -> neutral
  - smile/happy hold -> neutral
- Touch pages in the bottom terminal band:
  - Page 0: live familiar/Hermes status
  - Page 1: recent Hermes messages
  - Page 2: action controls
  - Page 3: device diagnostics
- Bidirectional USB JSON bridge:
  - device receives Hermes state/events
  - device sends touch/action/permission events
  - bridge can start/pause/resume/cancel one configured Hermes action job

## Build

```bash
pio run
```

## Flash

```bash
pio run -t upload --upload-port /dev/cu.usbmodem101
```

Monitor boot:

```bash
pio device monitor -p /dev/cu.usbmodem101 -b 115200
```

Expected boot lines:

```json
{"touch":"ok","driver":"cst328"}
{"imu":"ok|off","imu_addr":106,"rtc":"ok|off"}
{"audio":"ok","mode":"i2s-chirps"}
{"sd":"ok","mb":122024}
{"wifi":"no-config|no-ssid|ok"}
{"hello":"hermes-buddy","transport":"serial+ble-nus"}
```

## Run the USB bridge

See also [`docs/HERMES_INTEGRATION.md`](docs/HERMES_INTEGRATION.md) for the native Hermes API/event/cron integration contract and launchd helper.

```bash
python3 scripts/hermes_serial_bridge.py --port /dev/cu.usbmodem101 --interval 1
```

The bridge reads `~/.hermes/state.db` in read-only mode and creates/uses:

```text
~/.hermes/familiar_actions.json
```

Optional native Hermes API mode:

```bash
API_SERVER_KEY=<key> python3 scripts/hermes_serial_bridge.py \
  --port /dev/cu.usbmodem101 \
  --api-url http://127.0.0.1:8642
```

When `--api-url` and `--api-key`/`API_SERVER_KEY` are present, the bridge runs in native Hermes API mode:

- Page 2 `start` can create a real `/v1/runs` server-side Hermes run.
- The bridge subscribes to `/v1/runs/{run_id}/events` SSE and forwards compact tool/lifecycle/approval events to the device.
- Page 2 `cancel` uses `/v1/runs/{run_id}/stop`.
- Permission decisions use `/v1/runs/{run_id}/approval`.
- Configured cron jobs can be triggered/paused/resumed through `/api/jobs/*`.

Without API mode, the bridge preserves the local CLI subprocess fallback.

Default action slot:

```json
{
  "id": "status_brief",
  "label": "Status brief",
  "enabled": true,
  "type": "run",
  "prompt": "Give me a concise current Hermes work/status brief. Include active tasks, blockers, and next best action. Keep it under 120 words.",
  "command": [
    "hermes",
    "chat",
    "-q",
    "Give me a concise current Hermes work/status brief. Include active tasks, blockers, and next best action. Keep it under 120 words."
  ]
}
```

Action config semantics:

- `type: "run"` / `"api_run"`: in API mode, starts `/v1/runs` using `prompt`; otherwise falls back to `command`.
- `type: "cron_job"` / `"api_job"`: uses `job_id` with `/api/jobs/{job_id}/run`, and Page 2 pause/resume toggles `/pause` and `/resume`.
- `type: "command"`: always uses the local subprocess `command` fallback.
- Legacy `command: ["hermes", "chat", "-q", "..."]` entries are treated as `run` in API mode unless you explicitly set `type: "command"`.

Example cron-backed action:

```json
{
  "id": "nightly_review",
  "label": "Nightly review",
  "enabled": true,
  "type": "cron_job",
  "job_id": "aabbccddeeff"
}
```

Disable or edit that config if you do not want the device to be able to spawn a Hermes run/job.

## Touch controls

### Portrait area

Tap the portrait area for local personality reaction.

### Bottom terminal band and swipes

There are no required side buttons. Use the touchscreen:

- Swipe left anywhere on the screen: next page.
- Swipe right anywhere on the screen: previous page.
- Tap the bottom band on non-action pages: next page.
- Tap the portrait area: local personality reaction, or jump to approval controls if Hermes is waiting.

Page 0: status

```text
FAMILIAR:AWAKE/THINK/WAIT
sessions/running/waiting/tokens
latest message or touch
```

Page 1: recent messages

Shows recent Hermes user/assistant lines from the local state DB.

Page 2: actions

```text
START    PAUSE    CANCEL
```

Touch zones on the bottom action band:

- x < 80: start first enabled action from `~/.hermes/familiar_actions.json`
- 80 <= x < 160: pause/resume running action
- x >= 160: cancel running action

When Hermes is waiting for approval, Page 2 becomes:

```text
TAP: ALLOW        DENY
```

- x < 120: allow once
- x >= 120: deny

Swipe left/right to leave the action page.

Page 3: device diagnostics

Shows battery voltage, RTC status/time if present, Wi-Fi/IP, IMU availability, and I2S audio availability. If `/hermes-buddy/config.json` on the SD card contains Wi-Fi credentials, the device also exposes:

```text
GET /status
GET /action?name=start|pause|cancel
GET /chirp
```

Example SD Wi-Fi config:

```json
{"wifi":{"ssid":"YourNetwork","password":"YourPassword"}}
```

## Bridge protocol examples

Host to device:

```json
{"type":"state","running":1,"waiting":0,"mood":"thinking","msg":"...","job_state":"running","job_label":"Status brief"}
{"type":"event","event":"message","role":"assistant","msg":"..."}
{"type":"ack","msg":"started: Status brief"}
```

Device to host:

```json
{"cmd":"touch","x":58,"y":121}
{"cmd":"swipe","dx":-72,"dy":4}
{"cmd":"action","action":"start"}
{"cmd":"action","action":"pause"}
{"cmd":"action","action":"cancel"}
{"cmd":"permission","decision":"once"}
{"event":"local_auto","mood":"blink"}
```

## SD asset pipeline

Source art folder:

```text
/Users/sem/Downloads/Hermes Familiar
```

Export SD pack:

```bash
python3 scripts/export_sd_pack.py \
  --src "/Users/sem/Downloads/Hermes Familiar" \
  --out sdcard/hermes-buddy \
  --preview assets/sd_preview
```

Copy `sdcard/hermes-buddy` to the root of the FAT32 SD card.

## Native extension status

USB serial is the always-on verified transport. Optional native Hermes API mode turns Page 2 into a real `/v1/runs` and `/api/jobs` control surface, including `/approval` decisions for runs started through the API bridge. CLI fallback remains available when the API server/key are not configured.

Current board bring-up status from the latest flash:

- Touch: verified OK.
- SD: verified OK.
- Audio/I2S chirps: verified firmware init OK.
- Wi-Fi: implemented; waiting for SD `config.json` credentials.
- Battery ADC: implemented and shown on Page 3.
- RTC/IMU: firmware probes vendor I2C pins GPIO11/GPIO10 and reports `off` on this unit/boot; code path remains present for boards where those parts respond.
