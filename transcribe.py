"""Transcription backends. Prefers faster-whisper (smaller, CPU-friendly);
falls back to the openai-whisper CLI if installed."""
from __future__ import annotations

import os
import shutil
import subprocess


class TranscribeError(RuntimeError):
    pass


def _whisper_cli(mp3_path: str, model: str = "tiny") -> str:
    """Use openai-whisper CLI. Slower; mainly for Mac/brew installs."""
    whisper = shutil.which("whisper")
    if not whisper:
        raise TranscribeError("whisper CLI not found")
    out_dir = os.path.dirname(mp3_path) or "."
    subprocess.run(
        [
            whisper, mp3_path,
            "--model", model,
            "--output_dir", out_dir,
            "--output_format", "txt",
            "--language", "en",
            "--verbose", "False",
        ],
        check=True,
        capture_output=True,
    )
    base = os.path.splitext(os.path.basename(mp3_path))[0]
    with open(os.path.join(out_dir, f"{base}.txt")) as f:
        return f.read().strip()


def _faster_whisper(mp3_path: str, model: str = "tiny") -> str:
    """Use faster-whisper (CTranslate2). Smaller model on disk, CPU-fast."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscribeError("faster-whisper not installed")
    fw_model = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = fw_model.transcribe(mp3_path, language="en", beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe(mp3_path: str, model: str = "tiny", prefer: str = "auto") -> tuple[str, str]:
    """Transcribe. Returns (text, source) where source is 'faster-whisper' or 'whisper'."""
    order = {
        "auto": ["faster-whisper", "whisper"],
        "faster-whisper": ["faster-whisper"],
        "whisper": ["whisper"],
    }[prefer]
    errors = []
    for backend in order:
        try:
            if backend == "faster-whisper":
                return _faster_whisper(mp3_path, model), "faster-whisper"
            if backend == "whisper":
                return _whisper_cli(mp3_path, model), "whisper"
        except TranscribeError as e:
            errors.append(f"{backend}: {e}")
    raise TranscribeError(f"no backend available: {'; '.join(errors)}")
