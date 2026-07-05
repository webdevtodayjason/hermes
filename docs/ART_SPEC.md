# Familiar Art Spec — landscape-native packs (Phase 8)

What an artist needs to draw the familiar a new face. No firmware knowledge
required: deliver PNGs to this spec and `scripts/export_sd_pack.py` compiles
them into an SD pack.

## Canvas

| Property | Value |
|---|---|
| Final screen | **320 × 240 landscape** |
| Source size | deliver at **2× (640×480)** or 3× (960×720), same aspect |
| Color | green-phosphor monochrome ideal; hard limit **16 distinct tones** per frame (4-bit indexed) — tiny soft gradients quantize badly, strong line art wins |
| Background | black / near-black |

## Zones (at 1× / 320×240)

```
┌────────────────────────────────┬───────┐  y=0
│ TAB BAR ZONE (320×18): firmware│       │  y=18
│ overdraws — decoration only    │       │
├────────────────────────────────┤ VITALS│
│                                │ ZONE  │
│   SUBJECT SAFE AREA            │ 80px  │
│   240 × 222                    │ wide: │
│   the face lives here          │ keep  │
│                                │ dark, │
│                                │ low-  │
│                                │ detail│
│                                │(text  │
│                                │ drawn │
│                                │ here) │
└────────────────────────────────┴───────┘  y=240
x=0                            x=240   x=320
```

- **Subject safe area (0,18)–(240,240):** all essential detail here.
- **Vitals zone (x ≥ 240):** firmware renders live green text on top. Ambient
  texture is welcome (cables, glow, shelf clutter) but keep it dark and free
  of high-contrast detail.
- **Tab bar zone (y < 18):** firmware overdraws it; anything here is texture.
- Avoid essential detail within 6px of any edge.

## Required frame set

One base frame per state + micro-animation frames. Small deltas beat big pose
changes: blink = eyelids only; wink = one eye + slight mouth; thinking =
eyes/glow pulse; sleep = breathing bob.

| State | Frames | Notes |
|---|---|---|
| `idle` | 1 base | the resting face — most-seen image, make it count |
| `blink` | 3 | closing, closed, base-return |
| `wink` | 2 | wink, un-wink (plays in ~700 ms) |
| `smile` | 2 | smile, neutral-return (~1.3 s) |
| `happy` | 3 | burst sequence (~1.5 s) |
| `thinking` | 4 | loop @ 220 ms/frame — motion should cycle cleanly |
| `waiting` | 2 | alert pulse loop @ 320 ms — reads as "needs you" |
| `sleep` | 3 | slow loop @ 700 ms — breathing |

**18 frames total.** Name them `idle_00.png`, `blink_00.png`… `sleep_02.png`.

## Optional sound set (any pack may include)

`boot / tap / ack / alert / done` — WAV, mono, 16 kHz, ≤2 s each.

## Delivery

One folder per pack:

```
my-pack/
  pack.json          {"name":"…","author":"…","license":"CC-BY-4.0"}
  idle_00.png … sleep_02.png
  sounds/ (optional)
```

Packs are MIT/CC-licensed for the Pack Exchange (buildafamiliar.com).
The exporter handles palette quantization, scaling, and the on-device format —
artists never touch a `.raw4`.

## Engineering note (not for artists)

Landscape packs need `export_sd_pack.py --landscape` (emit 320-wide raw4 +
pack v2 marker) and a firmware reader that keys frame width off the pack
marker instead of `HERMES_FRAME_W`. Tracked in Phase 8; portrait packs keep
working via the current crop path.
