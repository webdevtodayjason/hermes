"""voice — give the familiar a real voice.

Pipeline: text → Hermes' own TTS (tools.tts_tool, provider/voice from the
user's ``tts:`` config — ElevenLabs here) → afconvert to 16 kHz mono s16le →
raw PCM file → served over a tiny LAN HTTP server → the device streams it
straight into its I2S speaker (no transcoding on-device; the I2S bus runs at
exactly this format).

USB serial stays the control channel; Wi-Fi only carries audio. Everything
degrades quietly: no TTS provider / no afconvert / port taken / device not on
Wi-Fi → banners and chirps still work, speech is skipped.
"""
from __future__ import annotations

import functools
import http.server
import json
import logging
import os
import socket
import subprocess
import threading
import time
import wave
from pathlib import Path

logger = logging.getLogger("familiar.voice")

_PORT = 8765
_MAX_FILES = 40
_serve_dir: Path | None = None
_server_ok = False


def audio_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    d = home / "familiar_audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def start_server(port: int = _PORT) -> bool:
    """Serve the audio dir on the LAN. Idempotent; False if the port is taken."""
    global _serve_dir, _server_ok
    if _server_ok:
        return True
    _serve_dir = audio_dir()
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(_serve_dir))
    handler.log_message = lambda *a, **k: None  # quiet
    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    except OSError as e:
        logger.warning("familiar audio server port %d unavailable (%s) — voice off", port, e)
        return False
    threading.Thread(target=srv.serve_forever, name="familiar-audio", daemon=True).start()
    _server_ok = True
    logger.info("familiar audio server on :%d serving %s", port, _serve_dir)
    return True


def _prune() -> None:
    files = sorted(audio_dir().glob("*.pcm"), key=lambda p: p.stat().st_mtime)
    for p in files[:-_MAX_FILES]:
        try:
            p.unlink()
        except OSError:
            pass


def _tts_render(text: str) -> Path | None:
    """Hermes' own TTS -> audio file (mp3/ogg/wav per provider config)."""
    try:
        from tools.tts_tool import text_to_speech_tool
    except ImportError:
        logger.warning("hermes tts_tool unavailable — voice off")
        return None
    out = audio_dir() / f"tts_{int(time.time() * 1000)}.mp3"
    try:
        res = json.loads(text_to_speech_tool(text=text, output_path=str(out)))
    except Exception:
        logger.exception("familiar tts render failed")
        return None
    path = res.get("file_path") if isinstance(res, dict) else None
    if not path or not Path(path).exists():
        logger.warning("familiar tts produced no file: %s", res)
        return None
    return Path(path)


def _to_pcm16k(src: Path) -> Path | None:
    """Any audio file -> raw s16le 16 kHz mono via afconvert (macOS builtin)."""
    wav = src.with_suffix(".16k.wav")
    try:
        subprocess.run(
            ["afconvert", str(src), "-d", "LEI16@16000", "-c", "1",
             "-f", "WAVE", str(wav)],
            check=True, capture_output=True, timeout=30)
        with wave.open(str(wav), "rb") as w:
            frames = w.readframes(w.getnframes())
        pcm = src.with_suffix(".pcm")
        pcm.write_bytes(frames)
        return pcm
    except FileNotFoundError:
        logger.warning("afconvert not found — voice off")
        return None
    except Exception:
        logger.exception("familiar pcm convert failed")
        return None
    finally:
        wav.unlink(missing_ok=True)
        src.unlink(missing_ok=True)


def say_url(text: str, port: int = _PORT) -> str | None:
    """Full pipeline: text -> LAN URL of a device-ready PCM clip, or None."""
    text = " ".join(str(text or "").split())
    if not text:
        return None
    if not start_server(port):
        return None
    ip = lan_ip()
    if not ip:
        return None
    audio = _tts_render(text[:400])
    if audio is None:
        return None
    pcm = _to_pcm16k(audio)
    if pcm is None:
        return None
    _prune()
    return f"http://{ip}:{port}/{pcm.name}"
