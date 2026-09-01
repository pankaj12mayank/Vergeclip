"""
src/tts_engine.py
-----------------
Text-to-Speech engine using edge-tts (free Microsoft cloud TTS).
Returns audio file + word-level timestamps for caption syncing.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import TEMP_DIR
from src.logger import get_logger

log = get_logger(__name__)

AVAILABLE_VOICES = {
    "en-US-GuyNeural": "English (US) - Male",
    "en-US-JennyNeural": "English (US) - Female",
    "en-US-AriaNeural": "English (US) - Female (News)",
    "en-GB-RyanNeural": "English (UK) - Male",
    "en-GB-SoniaNeural": "English (UK) - Female",
    "hi-IN-MadhurNeural": "Hindi - Male",
    "hi-IN-SwaraNeural": "Hindi - Female",
    "es-ES-AlvaroNeural": "Spanish - Male",
    "fr-FR-HenriNeural": "French - Male",
    "de-DE-ConradNeural": "German - Male",
    "pt-BR-AntonioNeural": "Portuguese (BR) - Male",
    "ja-JP-KeitaNeural": "Japanese - Male",
    "ko-KR-InJoonNeural": "Korean - Male",
    "zh-CN-YunxiNeural": "Chinese - Male",
    "ar-SA-HamedNeural": "Arabic - Male",
}


@dataclass
class WordTiming:
    text: str
    start: float  # seconds
    end: float    # seconds


@dataclass
class TTSResult:
    audio_path: Path
    duration_secs: float
    segments: list[dict] = field(default_factory=list)
    words: list[WordTiming] = field(default_factory=list)


def _split_into_words(text: str) -> list[str]:
    """Split text into individual words, preserving punctuation."""
    return text.split()


def _distribute_words_to_sentence(
    sentence_text: str,
    sentence_start: float,
    sentence_duration: float,
) -> list[WordTiming]:
    """Distribute word timings proportionally within a sentence by character length."""
    words = _split_into_words(sentence_text.strip())
    if not words:
        return []

    total_chars = sum(len(w) for w in words)
    if total_chars == 0:
        return []

    timings = []
    current_time = sentence_start

    for word in words:
        char_ratio = len(word) / total_chars
        word_dur = sentence_duration * char_ratio
        timings.append(WordTiming(
            text=word,
            start=round(current_time, 3),
            end=round(current_time + word_dur, 3),
        ))
        current_time += word_dur

    return timings


async def _generate_tts(
    text: str,
    voice: str = "en-US-GuyNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> tuple[bytes, list[dict]]:
    """Generate TTS audio and extract sentence boundaries."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)

    audio_chunks = []
    boundaries = []

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
        elif chunk["type"] == "SentenceBoundary":
            boundaries.append({
                "text": chunk["text"],
                "offset": chunk["offset"],      # 100-nanosecond units
                "duration": chunk["duration"],   # 100-nanosecond units
            })

    audio_data = b"".join(audio_chunks)

    return audio_data, boundaries


def _boundaries_to_segments(boundaries: list[dict]) -> list[dict]:
    """Convert sentence boundaries to segment dicts with word timings."""
    segments = []
    for b in boundaries:
        start = b["offset"] / 10_000_000  # convert to seconds
        duration = b["duration"] / 10_000_000
        words = _distribute_words_to_sentence(b["text"], start, duration)
        segments.append({
            "text": b["text"].strip(),
            "start": round(start, 3),
            "end": round(start + duration, 3),
            "words": [{"text": w.text, "start": w.start, "end": w.end} for w in words],
        })
    return segments


def _process_audio(audio_data: bytes, boundaries: list[dict], output_path: Path) -> TTSResult:
    """Process audio data + boundaries into a TTSResult (shared by sync/async)."""
    if not audio_data:
        raise RuntimeError("TTS produced no audio data")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(audio_data)

    segments = _boundaries_to_segments(boundaries)

    all_words = []
    for seg in segments:
        for w in seg["words"]:
            all_words.append(WordTiming(text=w["text"], start=w["start"], end=w["end"]))

    if boundaries:
        last = boundaries[-1]
        total_dur = (last["offset"] + last["duration"]) / 10_000_000
    else:
        total_dur = output_path.stat().st_size / 16000

    log.info("TTS: done — %.1fs audio, %d segments, %d words", total_dur, len(segments), len(all_words))

    return TTSResult(
        audio_path=output_path,
        duration_secs=round(total_dur, 3),
        segments=segments,
        words=all_words,
    )


def synthesize_speech(
    text: str,
    voice: str = "en-US-GuyNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    output_path: Optional[Path] = None,
) -> TTSResult:
    """Sync wrapper: convert text to speech, return audio file + word timestamps."""
    if not text or not text.strip():
        raise ValueError("Text is empty — nothing to synthesize.")
    if output_path is None:
        output_path = TEMP_DIR / "tts_output.mp3"

    log.info("TTS: synthesizing %d chars with voice=%s", len(text), voice)
    audio_data, boundaries = asyncio.run(_generate_tts(text, voice=voice, rate=rate, pitch=pitch))
    return _process_audio(audio_data, boundaries, output_path)


async def async_synthesize_speech(
    text: str,
    voice: str = "en-US-GuyNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    output_path: Optional[Path] = None,
) -> TTSResult:
    """Async version: safe to call from within a running event loop."""
    if not text or not text.strip():
        raise ValueError("Text is empty — nothing to synthesize.")
    if output_path is None:
        output_path = TEMP_DIR / "tts_output_async.mp3"

    log.info("TTS (async): synthesizing %d chars with voice=%s", len(text), voice)
    audio_data, boundaries = await _generate_tts(text, voice=voice, rate=rate, pitch=pitch)
    return _process_audio(audio_data, boundaries, output_path)
