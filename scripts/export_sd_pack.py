#!/usr/bin/env python3
"""Export Hermes Familiar source PNGs into an SD-card asset pack.

Layout choice C: preserve the square portrait as a 240x240-ish character panel,
place it in the top portrait area, and reserve the bottom 68px for live firmware
terminal text. Output frames are 240x320 raw4: 4-bit indexed, two pixels/byte.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter

W, H = 240, 320
BOTTOM_BAND = 68
PORTRAIT_H = H - BOTTOM_BAND
# RGB preview palette. Firmware uses equivalent RGB565 palette.
PAL = [
    (0, 0, 0),       # 0 off/black
    (0, 24, 0),      # 1 dim phosphor
    (0, 160, 0),     # 2 active phosphor
    (88, 255, 83),   # 3 hot phosphor
]

STATE_MAP = {
    "idle": ["idle_00.png"],
    "blink": ["blink_00.png", "blink_01.png", "idle_00.png"],
    "wink": ["wink_00.png", "idle_00.png"],
    "smile": ["smile_00.png", "idle_00.png"],
    "happy": ["happy_00.png", "happy_01.png", "idle_00.png"],
    "sleep": ["sleep_00.png", "sleep_01.png", "sleep_02.png"],
    "thinking": ["thinking_00.png", "thinking_01.png", "thinking_02.png", "thinking_03.png"],
    "waiting": ["waiting_00.png", "waiting_01.png"],
}


def fit_square_portrait(src: Image.Image) -> Image.Image:
    """Fit square source into top portrait area without covering bottom band."""
    src = src.convert("RGB")
    # Use most of the portrait zone, preserving full headphones/hair.
    target = 238
    src.thumbnail((target, target), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    x = (W - src.width) // 2
    # Slightly high so chin/shoulders do not enter bottom live console.
    y = 6
    canvas.paste(src, (x, y))
    return canvas


def terminal_quantize(im: Image.Image, sleep_dim: bool = False) -> Image.Image:
    im = ImageEnhance.Contrast(im).enhance(1.25)
    im = ImageEnhance.Sharpness(im).enhance(1.25)
    im = im.filter(ImageFilter.UnsharpMask(radius=0.7, percent=115, threshold=2))
    px = im.load()
    out = Image.new("P", (W, H))
    flat = []
    for c in PAL:
        flat.extend(c)
    flat += [0, 0, 0] * (256 - len(PAL))
    out.putpalette(flat)
    opx = out.load()
    for y in range(H):
        # Bottom band is live firmware territory; keep asset black there.
        if y >= H - BOTTOM_BAND:
            for x in range(W):
                opx[x, y] = 0
            continue
        scan = 0.75 if (y % 3 == 2) else (0.90 if (y % 3 == 1) else 1.0)
        for x in range(W):
            r, g, b = px[x, y]
            # Source art is already green, but some antialiasing may include RGB.
            # Favor green while still preserving bright linework.
            lum = max(g, int((r + g + b) / 3)) * scan
            # Stable ordered dither; avoids muddy gradients on the low-bit LCD.
            jitter = ((x * 13 + y * 7) & 15) - 7
            v = lum + jitter
            if v < 22:
                idx = 0
            elif v < 72:
                idx = 1
            elif v < 172:
                idx = 2
            else:
                idx = 3
            if sleep_dim and idx > 1:
                idx -= 1
            opx[x, y] = idx
    return out


def pack4(im: Image.Image) -> bytes:
    pix = im.load()
    data = bytearray()
    for y in range(H):
        for x in range(0, W, 2):
            data.append(((pix[x, y] & 0x0F) << 4) | (pix[x + 1, y] & 0x0F))
    return bytes(data)


def export_state(src_dir: Path, out_root: Path, preview_root: Path, state: str, files: list[str]) -> list[str]:
    out_dir = out_root / "frames" / state
    prev_dir = preview_root / state
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for i, name in enumerate(files):
        src_path = src_dir / name
        if not src_path.exists():
            raise FileNotFoundError(src_path)
        im = fit_square_portrait(Image.open(src_path))
        q = terminal_quantize(im, sleep_dim=(state == "sleep"))
        raw_name = f"{i:03d}.raw4"
        (out_dir / raw_name).write_bytes(pack4(q))
        q.convert("RGB").save(prev_dir / f"{i:03d}.png")
        exported.append(raw_name)
    return exported


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path("/Users/sem/Downloads/Hermes Familiar"))
    ap.add_argument("--out", type=Path, default=Path("sdcard/hermes-buddy"))
    ap.add_argument("--preview", type=Path, default=Path("assets/sd_preview"))
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    args.preview.mkdir(parents=True, exist_ok=True)
    (args.out / "frames").mkdir(exist_ok=True)
    (args.out / "sounds").mkdir(exist_ok=True)
    (args.out / "logs").mkdir(exist_ok=True)

    animations = {}
    for state, files in STATE_MAP.items():
        frames = export_state(args.src, args.out, args.preview, state, files)
        animations[state] = {"frames": frames, "frame_ms": 180 if state not in {"sleep", "thinking"} else (700 if state == "sleep" else 220)}

    config = {
        "name": "Hermes Familiar",
        "version": 1,
        "format": "raw4-indexed-240x320",
        "layout": "option-c-square-portrait-bottom-console",
        "width": W,
        "height": H,
        "bottom_band_px": BOTTOM_BAND,
        "palette_rgb565": ["0x0000", "0x00C0", "0x03E0", "0x57EA"],
        "animations": animations,
        "notes": "Generated from square source frames; bottom console is intentionally black for live firmware text."
    }
    (args.out / "config.json").write_text(json.dumps(config, indent=2))
    (args.out / "README.txt").write_text(
        "Hermes Familiar SD asset pack\n"
        "Copy this hermes-buddy folder to the root of the FAT32 SD card.\n"
        "Frames are 240x320 raw4, two pixels per byte.\n"
    )
    print(f"wrote SD pack: {args.out}")
    print(f"wrote previews: {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
