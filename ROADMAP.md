# Hermes Buddy Roadmap

A physical Hermes companion for the Waveshare ESP32-S3-Touch-LCD-2.8: dark-terminal aesthetic, animated portrait, touch interaction, SD-card assets, speaker feedback, and Hermes event integration.

## Current working baseline

- Board: Waveshare ESP32-S3-Touch-LCD-2.8, ST7789 240x320 IPS.
- Firmware stack: PlatformIO + Arduino + LovyanGFX + ArduinoJson + NimBLE.
- Display calibration locked:
  - ST7789 LCD inversion: ON.
  - Frame buffer path: `pushImage()` requires `gfx->setSwapBytes(true)` while pushing RGB565 frame rows.
  - Normal RGB565 palette renders correctly after calibration.
- Power latch fixed:
  - `GPIO6` = power key input.
  - `GPIO7` = battery power hold; must be driven HIGH early in boot.
- Current data bridge:
  - USB serial newline-delimited JSON.
  - Python bridge reads Hermes SQLite state read-only and sends compact status snapshots.
- UI direction:
  - Hermes portrait/art area should remain mostly free of live overlay text.
  - Live readable status belongs in a reserved bottom terminal band.
  - Baked-in/decorative terminal text inside art is okay as texture, but live text must not stack on it.

## Product vision

Hermes Buddy is a small desk companion: a green-phosphor terminal avatar for Hermes that feels alive. It should show Hermes state, react to touch, display alerts, and eventually allow lightweight approvals/actions from the device.

Tone:

- dark terminal
- CRT/phosphor glow
- minimal but expressive animation
- physical companion, not a generic dashboard
- useful enough to sit on a desk all day

## Phase 1 — SD-card asset system

Goal: stop compiling every visual into firmware. The SD card becomes the theme/animation store.

### SD layout

```text
/hermes-buddy/
  config.json
  palettes/
    green-crt.json
    amber-crt.json
  frames/
    sleep/
      000.raw4
      001.raw4
      002.raw4
    idle/
      000.raw4
      blink_000.raw4
      blink_001.raw4
      smile_000.raw4
    thinking/
      000.raw4
      001.raw4
      002.raw4
      003.raw4
    waiting/
      000.raw4
      pulse_000.raw4
      pulse_001.raw4
    happy/
      000.raw4
      001.raw4
    alert/
      000.raw4
  sounds/
    boot.wav
    tap.wav
    giggle.wav
    ack.wav
    alert.wav
  logs/
    events.log
```

### Asset format v1

Start with the same format already proven in firmware:

- 240x320 pixels.
- 4-bit indexed palette.
- Two pixels per byte.
- 38,400 bytes per full-screen frame.
- RGB565 palette loaded separately.

This is efficient enough for many animation frames and simple enough to stream line-by-line.

### Firmware behavior

- Try SD first.
- If SD card missing or asset missing, fall back to compiled-in frames.
- Read `config.json` for brightness, palette, animation timings, and enabled features.
- Log SD errors over serial.

## Phase 2 — Animation engine

Goal: Hermes looks alive using short frame bursts and procedural terminal effects.

### Animation model

Each state has:

```json
{
  "state": "idle",
  "default_loop": ["000.raw4"],
  "micro_animations": [
    {"name": "blink", "frames": ["blink_000.raw4", "blink_001.raw4", "000.raw4"], "interval_sec": [5, 12]},
    {"name": "smile", "frames": ["smile_000.raw4", "000.raw4"], "trigger": "touch"}
  ]
}
```

### Animation patterns

- **Sleep**
  - 2-4 frames: breathing / head bob.
  - drifting `Z z z` procedural overlay.
  - very dim glow.

- **Idle**
  - 1 base frame.
  - blink: 2-3 frames.
  - glance/head tilt: 2-4 frames.
  - occasional smile/wink.

- **Thinking**
  - 2-4 frame loop.
  - faster scanline.
  - animated prompt: `> thinking...`, `> routing...`, `> tool active...`.

- **Waiting for user**
  - amber or pulsing green outline.
  - subtle alert pulse.
  - bottom band shows available action.

- **Happy / acknowledged**
  - smile or wink burst.
  - optional giggle sound.

- **Error / attention**
  - red/amber accent only; keep portrait mostly green.

### Procedural effects

Do these in firmware so art does not need hundreds of frames:

- scanline drift
- subtle flicker
- cursor blink
- bottom-band terminal type-on
- random phosphor noise
- touch ripple
- small horizontal glitch on state changes

## Phase 3 — Touch interaction

Goal: touch makes Hermes feel responsive and eventually useful.

### v1 interactions

- tap portrait: poke / wake / smile
- tap while idle: short smile/wink/giggle
- tap while waiting: approve once, if configured safe
- long press portrait: sleep / dim
- tap bottom band: cycle status pages
- hold bottom band: open quick menu

### Touch zones

```text
0..251 px vertical: portrait zone
252..319 px vertical: terminal/status zone
```

### Status pages

- Session status
- Active jobs/tools
- Alerts/permissions
- Device status: battery, Wi-Fi, SD, BLE, uptime
- Settings: brightness, volume, palette

## Phase 4 — Audio/speaker personality

Goal: small sounds, not full voice first.

Sounds from SD:

- boot chime
- tap/click
- giggle
- acknowledged/approved
- alert
- disconnect/reconnect

Later:

- stream TTS snippets from desktop
- mouth/VU animation while speaking
- optional local WAV playback for canned responses

## Phase 5 — Better Hermes bridge protocol

Current bridge sends snapshots. Better bridge sends events.

### Event examples

```json
{"type":"state","mood":"thinking","running":1,"waiting":0}
{"type":"tool_started","name":"terminal"}
{"type":"tool_finished","name":"terminal","ok":true}
{"type":"attention","level":"warning","text":"Hermes needs approval"}
{"type":"permission","id":"abc123","text":"Run command?","choices":["yes","no"]}
{"type":"tts_started"}
{"type":"tts_finished"}
```

### Transport roadmap

1. USB serial, current dev path.
2. Wi-Fi WebSocket from local desktop bridge.
3. BLE NUS as fallback/mobile-friendly transport.

## Phase 6 — Device actions

Potential device-side controls:

- yes/no for approvals
- start predefined job
- pause/resume job
- dismiss alert
- show last Hermes message
- send poke/check-in
- brightness/volume control

Safety rule: start with low-risk actions only. Dangerous approvals should require explicit confirmation/hold gesture.

## Graphics requirements for Sem

Best source assets to provide:

### Canvas

- Native target: 240x320 portrait.
- Best source size: 480x640 or 720x960 portrait, same 3:4 aspect ratio.
- Leave the bottom 68 px of the final 240x320 screen for the live terminal band. In source art terms, avoid putting important facial detail in the bottom ~20%.
- Avoid live-readable text in the top-left unless it is meant to be decorative texture.

### Style

- Green-on-black source is ideal.
- Keep Hermes mostly monochrome green/CRT.
- Strong line art and clear silhouette work best.
- High contrast is good; tiny soft gradients may quantize poorly.
- Transparent/black background preferred.

### Frame sets

For each animation, provide 2-4 consecutive images. Yes: `bam bam bam` loops are exactly the right model.

Recommended first set:

```text
sleep_00.png
sleep_01.png
sleep_02.png

idle_00.png
blink_00.png
blink_01.png
wink_00.png
smile_00.png

thinking_00.png
thinking_01.png
thinking_02.png
thinking_03.png

happy_00.png
happy_01.png

waiting_00.png
waiting_01.png
```

### Animation guidance

Small changes are better than large ones:

- blink: eyelid only
- wink: one eye + slight smile
- smile: mouth/cheek change
- head turn: subtle 2-4 px shift or slight alternate pose
- thinking: eyes/glow/headphones pulse
- sleep: breathing/head bob, Zs can also be firmware overlay

### What to avoid

- lots of tiny text baked into the same region as the live bottom terminal band
- full-color skin tones if the final should be terminal green
- huge pose changes unless intended as one-shot transitions
- essential detail at the very edges of the frame

## Community/polish goals

- Make asset packs easy to swap.
- Publish clear `assets/README.md` and converter script.
- Include default Hermes CRT theme.
- Include calibration mode for other screens/revisions.
- Include a demo bridge mode so people can run it without Hermes installed.
- Eventually package as an open-source project with firmware, desktop bridge, SD sample pack, and build instructions.

## Next implementation task

Implement Phase 1:

1. Add SD card initialization.
2. Define `/hermes-buddy/config.json`.
3. Add `.raw4` frame loading line-by-line from SD.
4. Add fallback to current compiled frames.
5. Add `scripts/export_sd_pack.py` to convert source PNGs into SD card assets.
6. Test with one sleep/idle animation loop.
