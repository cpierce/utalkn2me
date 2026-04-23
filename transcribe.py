"""Transcription backends. Prefers faster-whisper (smaller, CPU-friendly);
falls back to the openai-whisper CLI if installed."""
from __future__ import annotations

import os
import re
import shutil
import subprocess


class TranscribeError(RuntimeError):
    pass


# Short filler phrases whisper loves to repeat at end-of-audio.
# Matched case-insensitively, with optional trailing punctuation.
_FILLER_PHRASES = [
    r"thank you",
    r"thanks",
    r"thank you so much",
    r"bye+(?:\s*bye)?",
    r"goodbye",
    r"alright(?:\s+then)?",
    r"all right(?:\s+then)?",
    r"ok(?:ay)?",
    r"so[,]?\s*i'?m going to go ahead and get started",
    r"i'?ll see you then",
    r"you're welcome",
    r"uh-huh",
    r"mm-hmm",
    r"yeah",
    r"yes sir",
]

_FILLER_RE = re.compile(
    r"(?:^|(?<=[.\s]))\s*(?:" + "|".join(_FILLER_PHRASES) + r")\b[.!?\s]*",
    re.IGNORECASE,
)


def clean_transcript(text: str) -> str:
    """Strip repeated filler/hallucination phrases, especially trailing runs.

    Whisper often emits the same short phrase 5–20× at end-of-audio (e.g.
    "Thank you. Thank you. Thank you…"). This walks from the end and removes
    duplicate/filler segments until it hits real content.
    """
    if not text:
        return text

    # 1. Collapse any run of identical consecutive sentences (anywhere in text)
    #    into a single instance: "A. A. A. B." → "A. B."
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    dedup: list[str] = []
    for s in sentences:
        if dedup and s.strip().lower() == dedup[-1].strip().lower():
            continue
        dedup.append(s)

    # 2. From the end, strip filler sentences (whole sentence is just filler).
    while dedup:
        last = dedup[-1].strip().rstrip(".!?,")
        if not last or _is_filler_only(last):
            dedup.pop()
        else:
            break

    cleaned = " ".join(dedup).strip()
    # 3. Also collapse any lingering immediate dupes of small phrases
    #    (handles "There you go. There you go." inside a sentence stream).
    cleaned = re.sub(
        r"\b(there you go|so i'?m going to go ahead and get started)[.!?,\s]+"
        r"(?:\1[.!?,\s]+){1,}", r"\1. ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _is_filler_only(sentence: str) -> bool:
    """True if the sentence (stripped of punctuation) is just filler phrases."""
    s = re.sub(r"[.!?,]", " ", sentence).strip().lower()
    if not s:
        return True
    # remove filler phrases one at a time; if nothing substantive is left, it's filler
    remaining = _FILLER_RE.sub(" ", s).strip()
    return len(remaining) <= 2  # allow stray "uh"


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


def transcribe(mp3_path: str, model: str = "tiny", prefer: str = "auto",
               clean: bool | None = None) -> tuple[str, str]:
    """Transcribe. Returns (text, source) where source is 'faster-whisper' or 'whisper'.

    If `clean` is True (or unset and $TRANSCRIPT_CLEAN is not "0"/"false"),
    strips whisper's trailing filler/hallucination repetitions.
    """
    if clean is None:
        clean = os.environ.get("TRANSCRIPT_CLEAN", "1").lower() not in ("0", "false", "no")

    order = {
        "auto": ["faster-whisper", "whisper"],
        "faster-whisper": ["faster-whisper"],
        "whisper": ["whisper"],
    }[prefer]
    errors = []
    for backend in order:
        try:
            if backend == "faster-whisper":
                text = _faster_whisper(mp3_path, model)
                return (clean_transcript(text) if clean else text), "faster-whisper"
            if backend == "whisper":
                text = _whisper_cli(mp3_path, model)
                return (clean_transcript(text) if clean else text), "whisper"
        except TranscribeError as e:
            errors.append(f"{backend}: {e}")
    raise TranscribeError(f"no backend available: {'; '.join(errors)}")
