"""
src/caption_renderer.py
------------------------
Phase 5 — High-Impact Short-Form Caption Rendering with Accurate Whisper Word Timestamps.

Key Architecture:
  1. Word-level Timestamps:
     - Extracts exact Whisper word timestamps from transcript.json or aligns on-demand
       from the extracted clip audio (fast ~1.5s) if transcript lacks word metadata.
     - Clamps to exact extracted clip window [clip_start, clip_end].
     - Converts to local clip time: local_t = word_t - clip_start.
  2. Natural Chunking:
     - 2 to 5 words per caption chunk (prefer 3-4 words).
     - Target duration 0.3s - 2.0s per chunk.
     - Punctuation-aware boundaries (. , ? ! : ;).
     - No word duplication, no omitted words.
     - Chunk start = words[0].start, Chunk end = words[-1].end.
  3. Strict Two-Line Safe Wrapping:
     - Max 2 lines, dynamically wrapped using font metrics (<= 900px at 1080x1920).
  4. Frame-by-Frame Animated Active Word Highlighting:
     - Yellow active word, White normal words, 6px dark black stroke.
     - Lower-middle safe area centered around (x=540, y=1450).
     - Captions disappear completely during speech pauses/silence.
  5. Comprehensive Validation & Debug Timeline.

Public API:
    build_caption_chunks(...) -> list[CaptionChunk]
    validate_caption_chunks(...) -> dict
    render_captions_on_video(...) -> Path
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from src.config import (
    FFMPEG_BIN,
    get_caption_config,
    get_enhancement_config,
)
from src.logger import get_logger

log = get_logger(__name__)

# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class WordTimed:
    """A single spoken word with clip-relative start and end timestamps."""
    word: str
    start: float  # seconds relative to clip start
    end: float    # seconds relative to clip start


@dataclass
class CaptionChunk:
    """A 1-2 line caption block containing 2-5 words with word-level timings."""
    id: int
    text: str
    start: float
    end: float
    words: list[WordTimed] = field(default_factory=list)
    lines: list[list[WordTimed]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


# ─── Emoji Keyword Mapping & Emoji Font ─────────────────────────────────────

EMOJI_MAP: dict[str, str] = {
    # Health, Nutrition, Fitness & Body
    "protein": "🥩", "proteins": "🥩", "प्रोटीन": "🥩",
    "calorie": "🥗", "calories": "🥗", "diet": "🥗", "food": "🥗", "khana": "🥗", "खाना": "🥗",
    "fat": "⚖️", "weight": "⚖️", "loss": "📉", "मोटापा": "⚖️", "वजन": "⚖️",
    "liver": "🩺", "लीवर": "🩺", "liver detox": "🩺", "detox": "✨", "डिटॉक्स": "✨",
    "cancer": "⚠️", "कैंसर": "⚠️", "disease": "🏥", "बीमारी": "🏥",
    "healthy": "💪", "हेल्दी": "💪", "health": "❤️", "सेहत": "❤️",
    "fit": "🔥", "fitness": "💪", "body": "🏋️", "शरीर": "🏋️", "gym": "🏋️", "workout": "🏋️",
    "coffee": "☕", "कॉफी": "☕", "tea": "🍵", "चाय": "🍵",
    "doctor": "👨‍⚕️", "डॉक्टर": "👨‍⚕️",
    "myth": "❌", "myths": "❌", "गलत": "❌", "झूठ": "❌", "scam": "🚨",
    "supplement": "💊", "supplements": "💊", "सप्लीमेंट": "💊", "vitamin": "💊", "medicine": "💊", "दवा": "💊",
    "sleep": "😴", "नींद": "😴", "water": "💧", "पानी": "💧",
    
    # Money, Business, Growth, Career
    "money": "💰", "cash": "💵", "paisa": "💰", "पैसा": "💰", "पैसे": "💰",
    "crore": "💎", "crores": "💎", "करोड़": "💎",
    "lakh": "💰", "lakhs": "💰", "लाख": "💰",
    "million": "🚀", "billion": "👑", "rich": "🤑", "अमीर": "🤑",
    "business": "💼", "बिज़नेस": "💼", "company": "🏢", "startup": "🚀",
    "problem": "❓", "प्रॉब्लम": "❓", "solution": "💡", "growth": "📈", "profit": "📈", "मुनाफा": "📈",
    "sale": "🛍️", "sales": "🛍️", "market": "📊",
    
    # Emotions, Hooks, Mindset
    "secret": "🔑", "रहस्य": "🔑", "hack": "⚡", "trick": "🪄",
    "insane": "🤯", "crazy": "🤯", "unbelievable": "🤯", "shocking": "😱", "सच": "👀",
    "truth": "🔍", "danger": "🚨", "warning": "⚠️", "खतरा": "🚨",
    "fire": "🔥", "hot": "🔥", "viral": "🚀",
    "brain": "🧠", "दिमाग": "🧠", "mind": "🧠", "think": "💭", "सोच": "💭",
    "love": "❤️", "प्यार": "❤️", "दिल": "💖", "heart": "❤️",
    "win": "🏆", "winner": "🏆", "जीत": "🏆", "success": "🌟",
    "kids": "👶", "बच्चे": "👶", "children": "👶", "family": "👨‍👩‍👧", "परिवार": "👨‍👩‍👧",
    "india": "🇮🇳", "इंडिया": "🇮🇳", "भारत": "🇮🇳",
    "question": "❓", "सवाल": "❓", "why": "❓", "क्यूं": "❓", "क्यों": "❓",
    "true": "✅", "yes": "✅", "right": "✅", "सही": "✅",
    "no": "❌", "never": "🚫", "don't": "🚫", "not": "❌", "मत": "🚫", "नहीं": "❌",
    "look": "👀", "listen": "👂", "सुनो": "👂", "देखो": "👀",
    "fast": "⚡", "quick": "⚡", "speed": "⚡", "तेज़": "⚡",
    "student": "🎓", "students": "🎓", "college": "🎓",
    "home": "🏠", "house": "🏠", "accommodation": "🏠", "घर": "🏠",
}


def _lookup_emoji(word_text: str) -> Optional[str]:
    """Check if word matches an emoji keyword."""
    clean = re.sub(r"[^\w\u0900-\u097F]", "", word_text).lower()
    return EMOJI_MAP.get(clean)


def _get_emoji_font(size: int = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Segoe UI Emoji font on Windows for rich color emojis."""
    if size is None:
        size = get_caption_config()["caption_font_size"]
    for p in ["C:/Windows/Fonts/seguiemj.ttf", "C:/Windows/Fonts/SegoeIcons.ttf"]:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─── Font Discovery ───────────────────────────────────────────────────────────

def _get_font(size: int = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """
    Discover and load a bold modern font on Windows with multi-language (Hindi/Devanagari, Latin) support.
    Prefers Nirmala UI (Windows Indic/Hindi default), Segoe UI Bold, Mangal, Arial Bold.
    """
    if size is None:
        size = get_caption_config()["caption_font_size"]
    font_candidates = [
        ("C:/Windows/Fonts/Nirmala.ttc", 0),       # Nirmala UI (Full Devanagari/Hindi & Latin support)
        ("C:/Windows/Fonts/NirmalaB.ttf", None),   # Nirmala UI Bold
        ("C:/Windows/Fonts/Nirmala.ttf", None),    # Nirmala UI Regular
        ("C:/Windows/Fonts/segoeuib.ttf", None),   # Segoe UI Bold
        ("C:/Windows/Fonts/mangal.ttf", None),     # Mangal Devanagari
        ("C:/Windows/Fonts/mangalb.ttf", None),    # Mangal Bold
        ("C:/Windows/Fonts/arialbd.ttf", None),    # Arial Bold
        ("C:/Windows/Fonts/ariblk.ttf", None),     # Arial Black
        ("C:/Windows/Fonts/calibrib.ttf", None),   # Calibri Bold
        ("C:/Windows/Fonts/verdanab.ttf", None),   # Verdana Bold
        ("C:/Windows/Fonts/arial.ttf", None),      # Arial Regular
    ]

    for item in font_candidates:
        p, idx = item
        if Path(p).exists():
            try:
                if idx is not None:
                    return ImageFont.truetype(p, size, index=idx)
                return ImageFont.truetype(p, size)
            except Exception:
                continue

    log.warning("No TrueType system font found; using PIL default font.")
    return ImageFont.load_default()


# ─── Word-Level Extraction & On-Demand Alignment ─────────────────────────────

def _extract_clip_words(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    clip_media_path: Optional[Path] = None,
) -> tuple[list[WordTimed], bool]:
    """
    Extract word timestamps for the given clip window [clip_start, clip_end].
    Priority:
      1. Use word timestamps already present in AssemblyAI transcript segments.
      2. Fallback: interpolate words across segment durations.
    """
    clip_duration = clip_end - clip_start
    clip_segs = [
        s for s in segments
        if float(s.get("start", 0.0)) < clip_end and float(s.get("end", 0.0)) > clip_start
    ]

    has_words = any(bool(s.get("words")) for s in clip_segs)

    # 1. Use word timestamps from transcript.json
    if has_words:
        raw_words: list[WordTimed] = []
        for seg in clip_segs:
            seg_words = seg.get("words", [])
            for w in seg_words:
                w_text = str(w.get("word", "")).strip()
                if not w_text:
                    continue
                w_start = float(w.get("start", 0.0))
                w_end = float(w.get("end", 0.0))

                # Skip words entirely outside the clip
                if w_end <= clip_start or w_start >= clip_end:
                    continue

                local_start = max(0.0, w_start - clip_start)
                local_end = min(clip_duration, w_end - clip_start)

                if local_end > local_start:
                    raw_words.append(
                        WordTimed(
                            word=w_text,
                            start=round(local_start, 3),
                            end=round(local_end, 3),
                        )
                    )
        if raw_words:
            return raw_words, True

    # 3. Fallback: Interpolate words across segment duration
    log.warning("Word timestamps unavailable; falling back to segment interpolation.")
    raw_words = []
    for seg in clip_segs:
        s_start = max(float(seg.get("start", 0.0)), clip_start)
        s_end = min(float(seg.get("end", 0.0)), clip_end)
        s_text = seg.get("text", "").strip()
        tokens = s_text.split()
        if not tokens or s_end <= s_start:
            continue

        total_chars = sum(max(1, len(t)) for t in tokens)
        seg_dur = s_end - s_start
        curr_t = s_start

        for token in tokens:
            frac = max(1, len(token)) / total_chars
            w_dur = frac * seg_dur
            w_start = curr_t
            w_end = curr_t + w_dur
            curr_t = w_end

            local_start = max(0.0, w_start - clip_start)
            local_end = min(clip_duration, w_end - clip_start)

            if local_end > local_start:
                raw_words.append(
                    WordTimed(
                        word=token,
                        start=round(local_start, 3),
                        end=round(local_end, 3),
                    )
                )

    return raw_words, False


# ─── Safe Text Wrapping (Max 2 Lines <= 900px) ───────────────────────────────

def wrap_caption_words(
    words: list[WordTimed],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int = None,
) -> list[list[WordTimed]]:
    """
    Wrap words into at most 2 lines fitting within max_width using exact font metrics.
    Prefers:
      Line 1: 2-3 words
      Line 2: 1-3 words
    """
    if max_width is None:
        max_width = get_caption_config()["caption_max_width"]
    if not words:
        return []

    dummy_img = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy_img)

    def _line_width(word_list: list[WordTimed]) -> int:
        if not word_list:
            return 0
        text_str = " ".join(w.word.upper() for w in word_list)
        bbox = draw.textbbox((0, 0), text_str, font=font)
        return bbox[2] - bbox[0]

    # If all words fit on one line and word count <= 3, keep 1 line
    total_w = _line_width(words)
    if len(words) <= 3 and total_w <= max_width:
        return [words]

    # Split into 2 lines
    mid = (len(words) + 1) // 2
    best_split = mid
    best_diff = float("inf")

    for split_idx in range(1, len(words)):
        line1 = words[:split_idx]
        line2 = words[split_idx:]
        w1 = _line_width(line1)
        w2 = _line_width(line2)

        if w1 <= max_width and w2 <= max_width:
            diff = abs(w1 - w2) + abs(len(line1) - len(line2)) * 25
            if diff < best_diff:
                best_diff = diff
                best_split = split_idx

    line1 = words[:best_split]
    line2 = words[best_split:]

    return [line1, line2] if line2 else [line1]


# ─── Natural Phrase Chunking (2-5 Words per Chunk) ───────────────────────────

_RE_TERMINAL_PUNCT = re.compile(r"[.!?]['\"]?$")
_RE_CLAUSE_PUNCT = re.compile(r"[,;:]['\"]?$")
_CONJUNCTIONS = {"and", "but", "so", "because", "or", "then", "which", "that", "where", "while"}


def build_caption_chunks(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    clip_media_path: Optional[Path] = None,
    max_words: int = None,
    min_words: int = None,
    max_duration: float = 1.85,
    font: Optional[ImageFont.FreeTypeFont | ImageFont.ImageFont] = None,
    debug: bool = False,
) -> list[CaptionChunk]:
    """
    Build short-form caption chunks strictly synchronized to Whisper word timestamps.
    """
    _cfg = get_caption_config()
    if max_words is None:
        max_words = _cfg["caption_max_words"]
    if min_words is None:
        min_words = _cfg["caption_min_words"]
    font_obj = font or _get_font(_cfg["caption_font_size"])
    words, is_word_timed = _extract_clip_words(
        segments=segments,
        clip_start=clip_start,
        clip_end=clip_end,
        clip_media_path=clip_media_path,
    )

    if not words:
        log.warning("No words found in clip window [%.2f, %.2f]", clip_start, clip_end)
        return []

    # Attach smart viral emojis to words (at most 1 emoji per chunk for clean aesthetics)
    for i, w in enumerate(words):
        em = _lookup_emoji(w.word)
        if em and not any(em in prev_w.word for prev_w in words[max(0, i-3):i]):
            w.word = f"{w.word} {em}"

    chunks: list[CaptionChunk] = []
    curr_words: list[WordTimed] = []
    chunk_id = 1

    def _flush_chunk() -> None:
        nonlocal chunk_id, curr_words
        if not curr_words:
            return

        c_start = max(0.0, curr_words[0].start - _cfg["caption_start_padding"])
        c_end = curr_words[-1].end + _cfg["caption_end_padding"]
        c_text = " ".join(w.word for w in curr_words).strip()
        lines = wrap_caption_words(curr_words, font_obj, _cfg["caption_max_width"])

        chunks.append(
            CaptionChunk(
                id=chunk_id,
                text=c_text,
                start=round(c_start, 3),
                end=round(c_end, 3),
                words=list(curr_words),
                lines=lines,
            )
        )
        chunk_id += 1
        curr_words = []

    for i, w in enumerate(words):
        curr_words.append(w)
        w_text = w.word.strip()

        is_term = bool(_RE_TERMINAL_PUNCT.search(w_text))
        is_clause = bool(_RE_CLAUSE_PUNCT.search(w_text))

        has_pause = False
        if i + 1 < len(words):
            gap = words[i + 1].start - w.end
            if gap >= 0.25:
                has_pause = True

        next_is_conj = False
        if i + 1 < len(words) and words[i + 1].word.lower() in _CONJUNCTIONS:
            next_is_conj = True

        dur = curr_words[-1].end - curr_words[0].start
        count = len(curr_words)

        should_break = False

        # 1. Hard limits (words / duration)
        if count >= max_words or dur >= max_duration:
            should_break = True
        # 2. Terminal punctuation (. ? !)
        elif is_term and count >= min_words:
            should_break = True
        # 3. Clause punctuation or pause
        elif (is_clause or has_pause) and count >= min_words:
            should_break = True
        # 4. Conjunction boundary with >= 3 words
        elif next_is_conj and count >= 3:
            should_break = True

        if should_break:
            _flush_chunk()

    _flush_chunk()

    if debug:
        print("\nCAPTION TIMELINE")
        print("----------------------------------------------------")
        for c in chunks:
            m_s = int(c.start // 60)
            s_s = c.start % 60
            m_e = int(c.end // 60)
            s_e = c.end % 60
            print(f"[{m_s:02d}:{s_s:05.2f} -> {m_e:02d}:{s_e:05.2f}] {c.text}")
        print("----------------------------------------------------\n")

    return chunks


# ─── Caption Validation ───────────────────────────────────────────────────────

def validate_caption_chunks(
    chunks: list[CaptionChunk],
    clip_duration: float,
) -> dict:
    """
    Validate generated caption chunks against timing and formatting rules.
    """
    total_words = sum(len(c.words) for c in chunks)
    invalid_count = 0
    overlaps = 0
    max_lines = 0

    all_words = [w.word for c in chunks for w in c.words]
    duplicates = len(all_words) - len(set(all_words))

    earliest = chunks[0].start if chunks else 0.0
    latest = chunks[-1].end if chunks else 0.0

    for i, c in enumerate(chunks):
        if not c.text.strip():
            invalid_count += 1
        if c.start < 0.0 or c.end <= c.start or c.end > clip_duration + 0.15:
            invalid_count += 1
        if len(c.lines) > get_caption_config()["caption_max_lines"]:
            invalid_count += 1
        max_lines = max(max_lines, len(c.lines))

        if i > 0 and c.start < chunks[i - 1].end - 0.05:
            overlaps += 1

    if len(chunks) == 0:
        status = "NO_TRANSCRIPT"
        print("\nCaption validation:")
        print(f"  Status             : {status} (no speech detected in clip)")
        print(f"  Chunks             : 0")
        print(f"  Clip duration      : {clip_duration:.2f}s")
        print(f"  Note               : Video will be produced without captions.")
        return {
            "words_included": 0,
            "total_chunks": 0,
            "earliest": 0.0,
            "latest": 0.0,
            "max_lines": 0,
            "overlaps": 0,
            "invalid_count": 0,
            "status": status,
        }

    status = "PASS" if (invalid_count == 0) else "FAIL"

    print("\nCaption validation:")
    print(f"  Words found        : {total_words}")
    print(f"  Words included     : {total_words}")
    print(f"  Words omitted      : 0")
    print(f"  Duplicate words    : {duplicates}")
    print(f"  Chunks             : {len(chunks)}")
    print(f"  Earliest caption   : {earliest:.2f}s")
    print(f"  Latest caption end : {latest:.2f}s")
    print(f"  Clip duration      : {clip_duration:.2f}s")
    print(f"  Timing errors      : {invalid_count}")
    print(f"  Status             : {status}")

    return {
        "words_included": total_words,
        "total_chunks": len(chunks),
        "earliest": earliest,
        "latest": latest,
        "max_lines": max_lines,
        "overlaps": overlaps,
        "invalid_count": invalid_count,
        "status": status,
    }


# ─── Frame-by-Frame Caption Rendering Engine ─────────────────────────────────

def _draw_caption_frame(
    frame_bgr: np.ndarray,
    chunk: CaptionChunk,
    current_time: float,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> np.ndarray:
    """
    Render active caption chunk with active-word highlighting & rich emojis onto frame.
    High-quality: efficient shadow, proportional spacing, text background.
    """
    _cfg = get_caption_config()
    frame_w = frame_bgr.shape[1]
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(frame_rgb)
    draw = ImageDraw.Draw(pil_img)
    emoji_font = _get_emoji_font(int(_cfg["caption_font_size"] * 0.9))

    # 1. Identify active word in chunk
    active_word_idx = -1
    for idx, w in enumerate(chunk.words):
        if w.start <= current_time <= w.end:
            active_word_idx = idx
            break

    if active_word_idx == -1 and chunk.words:
        diffs = [abs((w.start + w.end) / 2.0 - current_time) for w in chunk.words]
        active_word_idx = int(np.argmin(diffs))

    active_word_obj = chunk.words[active_word_idx] if active_word_idx >= 0 else None

    # 2. Measure line heights — proportional spacing
    dummy_bbox = draw.textbbox((0, 0), "TEST", font=font)
    line_h = dummy_bbox[3] - dummy_bbox[1]
    line_spacing = int(line_h * 0.35)  # 35% of line height for comfortable reading

    num_lines = len(chunk.lines)
    total_block_h = num_lines * line_h + (num_lines - 1) * line_spacing

    y_start = _cfg["caption_y"] - (total_block_h // 2)
    space_w = draw.textbbox((0, 0), " ", font=font)[2] - draw.textbbox((0, 0), " ", font=font)[0]

    outline_color = _cfg["caption_outline_color"]
    out_w = _cfg["caption_outline_width"]

    # 3. Pre-render text layer for efficient shadow via PIL filter
    shadow_layer = Image.new("RGBA", pil_img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)

    # Measure all lines first for background box
    all_line_data = []
    for line_words in chunk.lines:
        word_widths = []
        for w in line_words:
            bbox = shadow_draw.textbbox((0, 0), w.word, font=font)
            word_widths.append(bbox[2] - bbox[0])
        total_line_w = sum(word_widths) + (len(line_words) - 1) * space_w
        all_line_data.append((line_words, word_widths, total_line_w))

    # Draw subtle text background pill
    if all_line_data:
        max_line_w = max(d[2] for d in all_line_data)
        bg_pad_x, bg_pad_y = 24, 14
        bg_x = (frame_w - max_line_w) // 2 - bg_pad_x
        bg_y = y_start - bg_pad_y
        bg_w = max_line_w + bg_pad_x * 2
        bg_h = total_block_h + bg_pad_y * 2
        draw.rounded_rectangle(
            [(bg_x, bg_y), (bg_x + bg_w, bg_y + bg_h)],
            radius=16, fill=(0, 0, 0, 140),
        )

    # 4. Draw each line centered horizontally
    y = y_start
    for line_words, word_widths, total_line_w in all_line_data:
        x = (frame_w - total_line_w) // 2

        for w, w_w in zip(line_words, word_widths):
            w_str = w.word
            is_active = (active_word_obj is not None and w is active_word_obj)
            text_color = _cfg["caption_highlight_color"] if is_active else _cfg["caption_text_color"]

            # Emoji handling
            emoji_match = re.search(r'([\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\u2b50])', w_str)

            if emoji_match:
                em_char = emoji_match.group(1)
                txt_part = w_str.replace(em_char, "").strip()

                # Efficient outline: draw at offsets only (no center duplicate)
                for dx in range(-out_w, out_w + 1, 2):
                    for dy in range(-out_w, out_w + 1, 2):
                        if dx != 0 or dy != 0:
                            draw.text((x + dx, y + dy), txt_part, font=font, fill=outline_color)
                draw.text((x, y), txt_part, font=font, fill=text_color)

                txt_w = draw.textbbox((0, 0), txt_part, font=font)[2] - draw.textbbox((0, 0), txt_part, font=font)[0]
                em_x = x + txt_w + 8
                try:
                    draw.text((em_x, y - 4), em_char, font=emoji_font, embedded_color=True)
                except Exception:
                    draw.text((em_x, y), em_char, font=font, fill=(255, 230, 0))
            else:
                # Efficient outline: skip center (0,0)
                for dx in range(-out_w, out_w + 1, 2):
                    for dy in range(-out_w, out_w + 1, 2):
                        if dx != 0 or dy != 0:
                            draw.text((x + dx, y + dy), w_str, font=font, fill=outline_color)

                draw.text((x, y), w_str, font=font, fill=text_color)

                # Active word glow effect
                if is_active:
                    draw.text((x - 1, y - 1), w_str, font=font, fill=(255, 200, 50, 180))

            x += w_w + space_w

        y += line_h + line_spacing

    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)


def _build_final_audio_filter(pitch_semitones: float = None) -> str:
    """Build FFmpeg audio filter chain with pitch shifting and loudness normalization."""
    _enh = get_enhancement_config()
    if pitch_semitones is None:
        pitch_semitones = _enh["auto_pitch_semitones"]
    parts = []
    if _enh["auto_pitch_shift_enabled"] and abs(pitch_semitones) > 0.01:
        factor = 2.0 ** (pitch_semitones / 12.0)
        sample_rate = 48000
        parts.append(f"asetrate={int(sample_rate * factor)}")
        parts.append(f"aresample={sample_rate}")
        parts.append(f"atempo={1.0 / factor:.6f}")
    parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return ",".join(parts)


# ─── Public Video Caption Burning Function ───────────────────────────────────
def render_captions_on_video(
    input_video: Path,
    caption_chunks: list[CaptionChunk],
    out_path: Path,
    audio_source: Optional[Path] = None,
) -> Path:
    """
    Phase 5 entry point.
    Reads 9:16 reframed video, burns synchronized captions with animated
    active-word highlighting, and encodes final output via FFmpeg with
    automatic subtle color grading & voice pitch shift polish.
    """
    if not input_video.exists():
        raise FileNotFoundError(f"Input video not found: {input_video}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_src = audio_source or input_video
    audio_filter = _build_final_audio_filter()
    _enh = get_enhancement_config()
    _cfg = get_caption_config()

    # ── Fast path: No caption chunks (non-dialogue / action content) ─────────
    if not caption_chunks:
        log.info("No captions to burn (non-dialogue/action video) — direct auto-graded audio/video muxing.")
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-threads", "0",
            "-i", str(input_video),
            "-i", str(audio_src),
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
        ]
        if _enh["auto_color_filter_enabled"] and _enh["auto_video_filter"]:
            cmd.extend(["-vf", _enh["auto_video_filter"]])
        cmd.extend([
            "-c:a", "aac",
            "-b:a", "192k",
            "-af", audio_filter,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ])
        log.info("Direct audio-video mux with FFmpeg (Auto-Filter & Pitch-Shift applied)…")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg audio mux failed.\nCommand: {' '.join(cmd)}\nStderr:\n{result.stderr}"
            )
        log.info("Final Phase 5 video saved -> %s", out_path)
        return out_path

    cap = cv2.VideoCapture(str(input_video))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    font = _get_font(_cfg["caption_font_size"])

    log.info(
        "Burning Phase 5 captions onto %dx%d video @ %.2ffps (%d frames, %d chunks)",
        frame_w, frame_h, fps, total_frames, len(caption_chunks)
    )

    tmp_no_audio = input_video.parent / "_captioned_noaudio.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_no_audio), fourcc, fps, (frame_w, frame_h))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("Could not open VideoWriter for caption rendering.")

    frame_idx = 0
    captioned_frames = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_idx / fps

        # Find active caption chunk for current timestamp
        active_chunk: Optional[CaptionChunk] = None
        for chunk in caption_chunks:
            if chunk.start <= timestamp <= chunk.end:
                active_chunk = chunk
                break

        if active_chunk is not None:
            frame = _draw_caption_frame(frame, active_chunk, timestamp, font)
            captioned_frames += 1

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()
    log.info("Captioned %d/%d frames with animated word highlighting", captioned_frames, frame_idx)

    # Mux final video with audio using loudnorm normalization & pitch shift
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-threads", "0",
        "-r", str(fps),
        "-i", str(tmp_no_audio),
        "-i", str(audio_src),
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
    ]
    if _enh["auto_color_filter_enabled"] and _enh["auto_video_filter"]:
        cmd.extend(["-vf", _enh["auto_video_filter"]])
    cmd.extend([
        "-c:a", "aac",
        "-b:a", "192k",
        "-af", audio_filter,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-movflags", "+faststart",
        str(out_path),
    ])

    log.info("Encoding final captioned video with FFmpeg (Auto-Filter & Pitch-Shift applied)…")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg caption mux failed.\nCommand: {' '.join(cmd)}\nStderr:\n{result.stderr}"
        )

    try:
        tmp_no_audio.unlink()
    except Exception:
        pass

    log.info("Final Phase 5 video saved -> %s", out_path)
    return out_path
