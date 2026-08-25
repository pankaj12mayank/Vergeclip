"""
app/clip_selector.py
--------------------
Phase 3 module: Enhanced conversational clip selector for short-form video.

Features
────────
1. Clean Sentence-Boundary Alignment:
   - Ensures candidate clips start only at legitimate, clean sentence openings.
   - Strictly rejects dangling prepositions, conjunctions, pronouns, and filler openings.
   - Requires natural terminal punctuation (. ! ?) at candidate clip ends.

2. Comprehensive Heuristic Scorer:
   - Standalone-context evaluation: rewards introduced subjects/entities, penalizes
     unresolved pronouns or context-dependent dialogue.
   - Rich hook detection: questions, bold claims, surprises, imperatives, stories, numbers.
   - Body flow & engagement: tension/contrast, educational value, emotional words, stats.
   - Completeness & pacing: words-per-second pacing, high transcription confidence.

3. Two-Stage Candidate Pipeline:
   - Stage 1: Window generation across the full transcript.
   - Stage 2: Strict validation pass rejecting low-quality / fragmented clips.
   - Stage 3: Scoring & Non-Maximum Suppression (NMS) deduplication.
   - Stage 4: Diversity-aware timeline distribution (spaced or bucketed selection).

4. Detailed Output Formats:
   - temp/candidates.json (machine-readable)
   - temp/candidates.txt (structured human-review sheet)

Public API
──────────
    run_selection(transcript_path, **overrides) -> list[ScoredClip]
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from src.config import (
    CANDIDATE_POOL_JSON_FILENAME,
    CANDIDATES_JSON_FILENAME,
    CANDIDATES_TXT_FILENAME,
    FFMPEG_BIN,
    INPUT_DIR,
    TEMP_DIR,
    TRANSCRIPT_JSON_FILENAME,
    get_clip_selection_config,
    get_scoring_weights,
)
from src.logger import get_logger

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TranscriptSegment:
    """One Whisper segment loaded from transcript.json."""
    id: int
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float


@dataclass
class CandidateClip:
    """A raw window of consecutive transcript segments."""
    segments: list[TranscriptSegment]

    @property
    def start(self) -> float:
        return self.segments[0].start

    @property
    def end(self) -> float:
        return self.segments[-1].end

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments)

    @property
    def avg_logprob(self) -> float:
        return sum(s.avg_logprob for s in self.segments) / len(self.segments)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def hook_sentence(self) -> str:
        """Return the first sentence or first 12 words of the clip."""
        text = self.text.strip()
        match = re.search(r"^.*?[.!?](?:\s+|$)", text)
        if match:
            return match.group(0).strip()
        words = text.split()[:12]
        return " ".join(words) + "..."


@dataclass
class ScoredClip:
    """A candidate clip with a heuristic score and human-readable reasons."""
    id: int
    start: float
    end: float
    duration: float
    text: str
    score: float
    reasons: list[str] = field(default_factory=list)
    hook: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(self.score, 1)
        d["start"] = round(self.start, 3)
        d["end"] = round(self.end, 3)
        d["duration"] = round(self.duration, 3)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Regex Patterns for Boundary, Context, Hook & Quality Detection
# ─────────────────────────────────────────────────────────────────────────────

# Sentence ends
_RE_SENTENCE_END = re.compile(r"[.!?]['\"]?\s*$")

# Dangling / Fragment Openings that must NOT start a standalone short
_RE_DANGLING_OPEN = re.compile(
    r"^\s*(from|which|that|because|and|but|so|then|or|to|also|as|for|with|at|into|about|"
    r"like|who|whom|whose|where|while|although|unless|since|than|until|after|before|"
    r"despite|in terms of|of course|in fact|such as|according to)\b",
    re.I,
)

# Filler words at opening
_RE_WEAK_FILLER_OPEN = re.compile(
    r"^\s*(yeah|yep|yes|nope|no|well|okay|ok|you know|um|uh|right|mhm|hmm|"
    r"i mean|sort of|kind of|basically|literally|actually|look|listen|now)\b",
    re.I,
)

# Unresolved Pronouns at opening without prior context
_RE_UNRESOLVED_PRONOUN_OPEN = re.compile(
    r"^\s*(he|she|they|it|this|that|these|those|them|him|her|his|their|its)\b",
    re.I,
)

# Strong Hook Openings
_RE_HOOK_QUESTION = re.compile(
    r"^\s*(why|how|what|who|when|where|is it|can you|did you|have you|could it|"
    r"would you|what if|what happens|why do|why does|how does|how do|is there|"
    r"should we|are we|will we)\b",
    re.I,
)

_RE_HOOK_BOLD_CLAIM = re.compile(
    r"^\s*(the truth is|the fact is|in reality|i believe|there is no|every single|"
    r"you cannot|we are living in|the real danger|in my opinion|history shows|"
    r"i think|most people think|everyone thinks|it turns out|the reality is|"
    r"there is only one|if you look at)\b",
    re.I,
)

_RE_HOOK_SURPRISE = re.compile(
    r"^\s*(nobody knows|nobody realizes|most people don't|the secret is|"
    r"the crazy thing is|what nobody tells you|what you don't realize|"
    r"the shocking thing is|what surprised me|what's wild is|the insane part is)\b",
    re.I,
)

_RE_HOOK_FORMULAIC = re.compile(
    r"^\s*(the biggest|the most important|the #1|the number one|the primary|"
    r"the reason|the problem|the key|the solution|the main reason|"
    r"the single most|one thing|the greatest|the worst|one of the biggest|"
    r"one of the most|here is why|here's why|the whole point)\b",
    re.I,
)

_RE_HOOK_IMPERATIVE = re.compile(
    r"^\s*(think about|imagine|consider|picture this|take a look at|remember when|"
    r"understand that|ask yourself|let me show you|look at|listen to)\b",
    re.I,
)

_RE_HOOK_STORY = re.compile(
    r"^\s*(what happened was|i remember when|back in \d{4}|in \d{4}|when i was|"
    r"a few years ago|the story is|there was a time|i learned this when|"
    r"when we started|when this happened)\b",
    re.I,
)

_RE_HOOK_NUMBER = re.compile(
    r"^\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten|hundred|"
    r"thousand|million|billion|trillion|over \d+|more than \d+|\d+ percent)\b",
    re.I,
)

# Body patterns
_RE_CONTRAST = re.compile(
    r"\b(but|however|instead|yet|although|nevertheless|whereas|on the other hand|"
    r"actually|in reality|the truth is|because)\b",
    re.I,
)

_RE_EDUCATIONAL = re.compile(
    r"\b(the reason|this means|what this means|here's why|here is why|the point is|"
    r"this is why|the key is|what you need to know|in other words|for example|"
    r"to put it simply|what that means is)\b",
    re.I,
)

_RE_EMOTIONAL = re.compile(
    r"\b(love|hate|fear|amazing|terrifying|beautiful|heartbreaking|angry|sad|"
    r"happy|excited|shocking|devastating|inspiring|powerful|dangerous|disaster|"
    r"genius|catastrophe|crisis)\b",
    re.I,
)

_RE_STORYTELLING = re.compile(
    r"\b(and then|so then|suddenly|at that point|that's when|that was|"
    r"i remember|i realized|i thought|i knew|we were|there was|one day|"
    r"years ago|back then)\b",
    re.I,
)

_RE_STATISTICS = re.compile(
    r"\b(\d[\d,]*\.?\d*\s*%?|\d+\s*(?:billion|million|thousand|hundred|trillion|dollars|percent))\b",
    re.I,
)

_RE_FILLER_WORDS = re.compile(
    r"\b(um|uh|like|you know|sort of|kind of|basically|literally|actually|right|okay|so|and|well)\b",
    re.I,
)

# Capitalized proper nouns / entities indicator
_RE_NAMED_ENTITY = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b")

# Pacing standards for spoken video clips
_WPS_MIN = 1.8   # words per second
_WPS_MAX = 3.6


# ─────────────────────────────────────────────────────────────────────────────
# Sentence Boundary Helpers & Clean Start Detection
# ─────────────────────────────────────────────────────────────────────────────

def _is_clean_sentence_start(seg: TranscriptSegment, prev_seg: Optional[TranscriptSegment]) -> tuple[bool, str]:
    """
    Check whether `seg` is a clean, natural sentence starting point.

    Returns:
        (is_valid, reason)
    """
    text = seg.text.strip()
    if not text:
        return False, "empty text"

    # Must start with an uppercase letter
    if not text[0].isupper():
        return False, "starts with lowercase letter"

    # Preceding segment should have ended with terminal punctuation or had a pause
    if prev_seg is not None:
        prev_text = prev_seg.text.strip()
        gap = seg.start - prev_seg.end
        has_punct = bool(_RE_SENTENCE_END.search(prev_text))
        if not has_punct and gap < 0.4:
            return False, "preceding segment has no terminal punctuation (mid-sentence continuation)"

    # Must not start with dangling prepositions or dependent conjunctions
    if _RE_DANGLING_OPEN.match(text):
        match = _RE_DANGLING_OPEN.match(text).group(0).strip()
        return False, f"starts with dependent preposition/conjunction ('{match}')"

    # Must not start with weak filler words
    if _RE_WEAK_FILLER_OPEN.match(text):
        match = _RE_WEAK_FILLER_OPEN.match(text).group(0).strip()
        return False, f"starts with filler word ('{match}')"

    # Must not start with an unresolved pronoun
    if _RE_UNRESOLVED_PRONOUN_OPEN.match(text):
        match = _RE_UNRESOLVED_PRONOUN_OPEN.match(text).group(0).strip()
        return False, f"starts with unresolved pronoun ('{match}')"

    return True, "clean sentence start"


def _is_sentence_boundary(text: str) -> bool:
    """Return True if text ends at a natural sentence boundary."""
    return bool(_RE_SENTENCE_END.search(text.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Candidate Validation Pass
# ─────────────────────────────────────────────────────────────────────────────

def validate_candidate(clip: CandidateClip, min_dur: float, max_dur: float, min_words: int = 22) -> tuple[bool, str]:
    """
    Validation filter that strictly checks if a candidate can qualify as a Short.
    Rejects obviously incomplete, fragmented, or bad clips.
    """
    # 1. Duration bounds
    if clip.duration < min_dur:
        return False, f"duration {clip.duration:.1f}s < min {min_dur}s"
    if clip.duration > max_dur:
        return False, f"duration {clip.duration:.1f}s > max {max_dur}s"

    # 2. Word count bounds
    if clip.word_count < min_words:
        return False, f"too few words ({clip.word_count} words)"
    if clip.word_count > 200:  # relaxed upper bound — longer clips are ok
        return False, f"too many words ({clip.word_count} words)"

    text = clip.text.strip()

    # 3. Capitalization & opening check
    if not text or not text[0].isupper():
        return False, "opening text is not capitalized"

    if _RE_DANGLING_OPEN.match(text):
        match = _RE_DANGLING_OPEN.match(text).group(0).strip()
        return False, f"dangling connector opening ('{match}')"

    if _RE_WEAK_FILLER_OPEN.match(text):
        match = _RE_WEAK_FILLER_OPEN.match(text).group(0).strip()
        return False, f"weak filler opening ('{match}')"

    if _RE_UNRESOLVED_PRONOUN_OPEN.match(text):
        match = _RE_UNRESOLVED_PRONOUN_OPEN.match(text).group(0).strip()
        return False, f"unresolved pronoun opening ('{match}')"

    # 4. Ending check: must finish with terminal punctuation
    if not _RE_SENTENCE_END.search(text):
        return False, "incomplete sentence ending (no terminal punctuation)"

    # 5. Audio transcription quality (skip for action rechunked content with 0.0 logprob)
    if clip.avg_logprob < -0.28:
        return False, f"low transcription confidence (avg_logprob {clip.avg_logprob:.3f})"

    return True, "valid"


# ─────────────────────────────────────────────────────────────────────────────
# Scoring Signals Engine
# ─────────────────────────────────────────────────────────────────────────────

def _evaluate_standalone_context(text: str, first_sentence: str) -> tuple[float, list[str]]:
    """
    Evaluate whether the clip introduces standalone context rather than
    relying on external conversation.
    """
    score = 0.0
    reasons = []

    # Check if a proper noun or concrete subject is introduced early
    named_entities = _RE_NAMED_ENTITY.findall(first_sentence)
    # Common introductory phrases that establish standalone context
    has_subject_intro = bool(named_entities) or bool(
        re.search(r"\b(the world|people|government|president|election|money|billionaire|country|america|china|russia)\b", first_sentence, re.I)
    )

    if has_subject_intro:
        score += 12.0
        reasons.append("standalone context: introduces clear subject/entity")

    # Check if the opening sentence is a standalone question or claim
    if _RE_HOOK_QUESTION.search(first_sentence) or _RE_HOOK_BOLD_CLAIM.search(first_sentence):
        score += 8.0
        reasons.append("standalone structure: self-contained premise")

    # Penalize if it contains dangling references to external topics
    if re.search(r"^\s*(also|and another thing|like i said|as we discussed|going back to)\b", text, re.I):
        score -= 15.0
        reasons.append("penalty: references prior conversation context")

    return score, reasons


def score_candidate(clip: CandidateClip, weights: Optional[dict] = None) -> tuple[float, list[str]]:
    """
    Evaluate candidate clip with comprehensive heuristic signals.
    Returns (score, reasons).
    """
    w = weights or get_scoring_weights()
    score = 0.0
    reasons: list[str] = []

    text = clip.text.strip()
    lower = text.lower()
    first_sentence = clip.hook_sentence
    first_sentence_lower = first_sentence.lower()

    # ── 1. Hook Signals (Opening sentence / first 8-12 words) ─────────────
    if _RE_HOOK_QUESTION.search(first_sentence_lower):
        score += w.get("hook_question", 22.0)
        reasons.append("strong hook: opens with compelling question")

    elif _RE_HOOK_BOLD_CLAIM.search(first_sentence_lower):
        score += w.get("hook_bold_claim", 18.0)
        reasons.append("strong hook: bold claim/statement opening")

    elif _RE_HOOK_SURPRISE.search(first_sentence_lower):
        score += w.get("hook_surprising", 18.0)
        reasons.append("strong hook: surprising/unusual claim opening")

    elif _RE_HOOK_FORMULAIC.search(first_sentence_lower):
        score += w.get("hook_formulaic_power", 16.0)
        reasons.append("strong hook: high-authority topic opener")

    elif _RE_HOOK_IMPERATIVE.search(first_sentence_lower):
        score += w.get("hook_imperative", 15.0)
        reasons.append("strong hook: direct imperative call-to-action")

    elif _RE_HOOK_STORY.search(first_sentence_lower):
        score += w.get("hook_story_intro", 14.0)
        reasons.append("strong hook: compelling story opening")

    elif _RE_HOOK_NUMBER.search(first_sentence_lower):
        score += w.get("hook_number_stat", 12.0)
        reasons.append("strong hook: concrete data/number opener")
    else:
        score += w.get("clean_sentence_start", 8.0)
        reasons.append("clean sentence opening")

    # ── 2. Body Engagement & Flow ──────────────────────────────────────────
    # Mid-clip question (curiosity tension)
    rest_text = text[len(first_sentence):] if len(text) > len(first_sentence) else ""
    if "?" in rest_text:
        score += w.get("body_question", 8.0)
        reasons.append("builds curiosity: question in body")

    if _RE_CONTRAST.search(lower):
        score += w.get("body_contrast", 8.0)
        reasons.append("tension/contrast: but/however/instead/because")

    stats = _RE_STATISTICS.findall(lower)
    if stats:
        score += w.get("body_statistics", 7.0)
        reasons.append(f"data-driven: {len(stats)} number/stat reference(s)")

    if _RE_EDUCATIONAL.search(lower):
        score += w.get("body_educational", 8.0)
        reasons.append("educational value: explains mechanism or concept")

    if _RE_EMOTIONAL.search(lower):
        score += w.get("body_emotional", 6.0)
        reasons.append("emotional resonance: high-intensity language")

    if _RE_STORYTELLING.search(lower):
        score += w.get("body_storytelling", 6.0)
        reasons.append("narrative flow: progressive storytelling markers")

    # ── 3. Standalone Context & Completeness ──────────────────────────────
    ctx_score, ctx_reasons = _evaluate_standalone_context(text, first_sentence)
    score += ctx_score
    reasons.extend(ctx_reasons)

    if _RE_SENTENCE_END.search(text):
        score += w.get("complete_thought", 12.0)
        reasons.append("complete thought: ends with clean sentence boundary")
    else:
        score += w.get("penalty_incomplete_end", -15.0)
        reasons.append("penalty: ends mid-sentence")

    # Transcription quality
    if clip.avg_logprob > -0.12:
        score += w.get("high_confidence", 5.0)
        reasons.append("high audio confidence")
    elif clip.avg_logprob < -0.22:
        score += w.get("penalty_low_confidence", -8.0)
        reasons.append("penalty: low transcription confidence")

    # Pacing
    wps = clip.word_count / clip.duration if clip.duration > 0 else 0
    if _WPS_MIN <= wps <= _WPS_MAX:
        score += w.get("good_pacing", 4.0)
        reasons.append("optimal speaking pace")

    # ── 4. Penalties ───────────────────────────────────────────────────────
    filler_count = len(_RE_FILLER_WORDS.findall(lower))
    filler_density = filler_count / max(clip.word_count, 1)
    if filler_density > 0.15:
        score += w.get("penalty_filler_dense", -10.0)
        reasons.append(f"penalty: high filler word ratio ({filler_density:.0%})")

    return score, reasons


# ─────────────────────────────────────────────────────────────────────────────
# Window Generation (Strict Sentence Starts & Boundary Adherence)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_candidate_windows(
    segments: list[TranscriptSegment],
    min_dur: float,
    max_dur: float,
    step: float,
) -> list[CandidateClip]:
    """
    Generate candidate clips that strictly start at clean sentence beginnings
    and terminate at natural sentence boundaries within [min_dur, max_dur].
    """
    if not segments:
        return []

    candidates: list[CandidateClip] = []
    n = len(segments)

    # Precompute valid start segment indices
    valid_start_indices: list[int] = []
    for i in range(n):
        prev_seg = segments[i - 1] if i > 0 else None
        is_clean, _ = _is_clean_sentence_start(segments[i], prev_seg)
        if is_clean:
            valid_start_indices.append(i)

    log.info("Identified %d clean sentence start anchors out of %d segments", len(valid_start_indices), n)

    for start_idx in valid_start_indices:
        anchor_start = segments[start_idx].start
        window: list[TranscriptSegment] = []
        best_boundary_idx = -1

        for j in range(start_idx, n):
            seg = segments[j]
            dur = seg.end - anchor_start

            # Exceeded maximum duration limit
            if dur > max_dur:
                break

            window.append(seg)

            if dur >= min_dur:
                # If this segment ends with sentence boundary, record it
                if _is_sentence_boundary(seg.text):
                    best_boundary_idx = j
                    # If we found a sentence boundary in the target duration window,
                    # we can emit a candidate window right here
                    clip_segments = segments[start_idx : j + 1]
                    candidates.append(CandidateClip(segments=clip_segments))

        # If no exact boundary was emitted but we found an in-range boundary
        if best_boundary_idx != -1 and not any(c.end == segments[best_boundary_idx].end for c in candidates if c.start == anchor_start):
            clip_segments = segments[start_idx : best_boundary_idx + 1]
            clip_dur = clip_segments[-1].end - anchor_start
            if min_dur <= clip_dur <= max_dur:
                candidates.append(CandidateClip(segments=clip_segments))

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Overlap Removal & Diversity Selection
# ─────────────────────────────────────────────────────────────────────────────

def _overlap_fraction(a: ScoredClip, b: ScoredClip) -> float:
    """Compute temporal overlap fraction relative to the shorter clip."""
    start = max(a.start, b.start)
    end = min(a.end, b.end)
    overlap = max(0.0, end - start)
    shorter = min(a.duration, b.duration)
    return overlap / shorter if shorter > 0 else 0.0


def _remove_overlaps(clips: list[ScoredClip], threshold: float) -> list[ScoredClip]:
    """Greedy Non-Maximum Suppression by score."""
    sorted_clips = sorted(clips, key=lambda c: c.score, reverse=True)
    kept: list[ScoredClip] = []

    for candidate in sorted_clips:
        if not any(_overlap_fraction(candidate, k) > threshold for k in kept):
            kept.append(candidate)

    return kept


def _select_diverse_candidates(
    clips: list[ScoredClip],
    top_n: int,
    min_score: float,
    min_separation: float,
    strategy: str = "spaced_top",
    total_duration: float = 0.0,
) -> list[ScoredClip]:
    """
    Select top candidates while ensuring healthy temporal distribution
    across the entire podcast timeline.
    """
    # Filter candidates meeting min_score threshold
    qualified = [c for c in clips if c.score >= min_score]
    if not qualified:
        log.warning("No clips met min_score=%.1f; falling back to unfiltered top candidates", min_score)
        qualified = clips

    # Sort descending by score
    sorted_candidates = sorted(qualified, key=lambda c: c.score, reverse=True)

    if strategy == "bucketed" and total_duration > 0:
        # Divide timeline into top_n equal time buckets
        bucket_size = total_duration / top_n
        selected: list[ScoredClip] = []
        used_ids = set()

        for b in range(top_n):
            b_start = b * bucket_size
            b_end = (b + 1) * bucket_size
            # Find highest scoring clip in this bucket
            in_bucket = [
                c for c in sorted_candidates
                if c.id not in used_ids and (b_start <= c.start < b_end or b_start <= c.end <= b_end)
            ]
            if in_bucket:
                best = in_bucket[0]
                selected.append(best)
                used_ids.add(best.id)

        # Fill remaining slots with highest scoring unused clips if needed
        for c in sorted_candidates:
            if len(selected) >= top_n:
                break
            if c.id not in used_ids and not any(abs(c.start - s.start) < (min_separation / 2) for s in selected):
                selected.append(c)
                used_ids.add(c.id)

        return sorted(selected, key=lambda c: c.score, reverse=True)

    # Strategy: "spaced_top"
    # Pass 1: Select top candidates enforcing strict min_separation
    selected: list[ScoredClip] = []
    for cand in sorted_candidates:
        if len(selected) >= top_n:
            break
        if not any(abs(cand.start - s.start) < min_separation for s in selected):
            selected.append(cand)

    # Pass 2: If we have room, relax separation to half min_separation
    if len(selected) < top_n:
        for cand in sorted_candidates:
            if len(selected) >= top_n:
                break
            if cand not in selected and not any(abs(cand.start - s.start) < (min_separation * 0.5) for s in selected):
                selected.append(cand)

    # Pass 3: Fill any remaining slots with highest available scoring clips
    if len(selected) < top_n:
        for cand in sorted_candidates:
            if len(selected) >= top_n:
                break
            if cand not in selected:
                selected.append(cand)

    return sorted(selected, key=lambda c: c.score, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Output Formatting
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ts(secs: float) -> str:
    """Format seconds into HH:MM:SS or MM:SS."""
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"00:{m:02d}:{s:02d}"


def _save_json(clips: list[ScoredClip], meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_video": meta.get("video_file", "unknown"),
        "transcript_model": meta.get("model", "unknown"),
        "total_candidates": len(clips),
        "candidates": [c.to_dict() for c in clips],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Candidates JSON -> %s (%d clips)", path, len(clips))


def _save_txt(clips: list[ScoredClip], meta: dict, path: Path) -> None:
    """
    Save candidates in human-friendly review format:
    #1
    Score: 91.0
    Start: 00:32:14
    End: 00:32:32
    Duration: 18.0 sec

    Hook:
    ...

    Transcript:
    ...

    Reasons:
    - reason 1
    - reason 2
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("Podcast Shorts Generator — Candidate Clips Review\n")
        fh.write(f"Source   : {meta.get('video_file', 'unknown')}\n")
        fh.write(f"Selected : {len(clips)} candidate clips\n")
        fh.write("=" * 70 + "\n\n")

        for clip in clips:
            fh.write(f"#{clip.id}\n")
            fh.write(f"Score: {clip.score:.1f}\n")
            fh.write(f"Start: {_fmt_ts(clip.start)}\n")
            fh.write(f"End: {_fmt_ts(clip.end)}\n")
            fh.write(f"Duration: {clip.duration:.1f} sec\n\n")
            fh.write("Hook:\n")
            fh.write(f"{clip.hook}\n\n")
            fh.write("Transcript:\n")
            fh.write(f"{clip.text}\n\n")
            fh.write("Reasons:\n")
            for r in clip.reasons:
                fh.write(f"- {r}\n")
            fh.write("\n" + "-" * 70 + "\n\n")

    log.info("Candidates TXT -> %s", path)


# ─────────────────────────────────────────────────────────────────────────────
# Transcript Loading & Pipeline Execution
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Transcript Loading & Audio-Energy Action Highlight Generator
# ─────────────────────────────────────────────────────────────────────────────

def _load_transcript(path: Path) -> tuple[list[TranscriptSegment], dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Transcript not found: {path}\n"
            "Run Phase 2 first:\n"
            "  python -m app.main transcribe"
        )
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    raw_segs = data.get("segments", [])

    segments = [
        TranscriptSegment(
            id=s["id"],
            start=float(s["start"]),
            end=float(s["end"]),
            text=s["text"],
            avg_logprob=float(s.get("avg_logprob", -0.15)),
            no_speech_prob=float(s.get("no_speech_prob", 0.0)),
        )
        for s in raw_segs
    ]
    meta = {k: v for k, v in data.items() if k != "segments"}
    return segments, meta


def _generate_energy_based_candidates(
    meta: dict,
    total_duration: float,
    min_dur: float = None,
    max_dur: float = None,
    top_n: int = None,
    min_separation: float = None,
    overlap_threshold: float = None,
    target_dur: float = 20.0,
) -> tuple[list[ScoredClip], list[ScoredClip]]:
    _cfg = get_clip_selection_config()
    if min_dur is None:
        min_dur = _cfg["clip_min_duration"]
    if max_dur is None:
        max_dur = _cfg["clip_max_duration"]
    if top_n is None:
        top_n = _cfg["clip_top_n"]
    if min_separation is None:
        min_separation = _cfg["clip_min_separation"]
    if overlap_threshold is None:
        overlap_threshold = _cfg["clip_overlap_threshold"]
    """
    Generate candidate clips from audio-energy peaks and scene dynamics for videos
    without spoken dialogue (fight scenes, music videos, action compilations, trailers).

    Returns:
        (deduped_pool, final_selected_clips)
    """
    # 1. Locate source video
    source_video_name = meta.get("video_file") or meta.get("source_video")
    source_video_path = None
    if source_video_name:
        p = INPUT_DIR / source_video_name
        if p.exists():
            source_video_path = p

    if not source_video_path:
        vids = sorted(INPUT_DIR.glob("*.mp4"))
        if vids:
            source_video_path = max(vids, key=lambda f: f.stat().st_mtime)

    if source_video_path and total_duration <= 0.0:
        try:
            import cv2
            cap = cv2.VideoCapture(str(source_video_path))
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            cap.release()
            if fps > 0 and frames > 0:
                total_duration = round(frames / fps, 3)
        except Exception:
            pass

    if total_duration <= 0.0:
        total_duration = 300.0  # Safe fallback 5 mins

    log.info("Generating Audio-Energy candidates for %.1fs video", total_duration)

    norm_rms = None
    if source_video_path and source_video_path.exists():
        raw_probe = TEMP_DIR / "_energy_probe.raw"
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-i", str(source_video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "8000",
            "-ac", "1",
            "-f", "s16le",
            str(raw_probe),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=False)
            if res.returncode == 0 and raw_probe.exists() and raw_probe.stat().st_size > 0:
                samples = np.fromfile(str(raw_probe), dtype=np.int16).astype(np.float32)
                sample_rate = 8000
                num_secs = int(len(samples) / sample_rate)
                if num_secs > 0:
                    s_trunc = samples[:num_secs * sample_rate].reshape(num_secs, sample_rate)
                    rms = np.sqrt(np.mean(s_trunc**2, axis=1) + 1e-6)
                    # 3-second moving average smoothing
                    if len(rms) >= 3:
                        kernel = np.ones(3) / 3.0
                        rms_smooth = np.convolve(rms, kernel, mode="same")
                    else:
                        rms_smooth = rms
                    max_rms = float(np.max(rms_smooth)) if len(rms_smooth) > 0 else 1.0
                    if max_rms <= 0:
                        max_rms = 1.0
                    norm_rms = (rms_smooth / max_rms) * 100.0
                raw_probe.unlink(missing_ok=True)
        except Exception as probe_err:
            log.warning("Audio energy probe failed: %s. Using uniform action grid.", probe_err)

    # 2. Generate raw candidate windows
    win_len = min(max_dur, max(min_dur, target_dur))
    step_sec = 5.0

    raw_clips: list[ScoredClip] = []
    t = 0.0
    idx = 1

    while t + win_len <= total_duration:
        c_start = round(t, 3)
        c_end = round(t + win_len, 3)

        if norm_rms is not None and len(norm_rms) > 0:
            s_sec = int(min(len(norm_rms) - 1, max(0, c_start)))
            e_sec = int(min(len(norm_rms), max(s_sec + 1, c_end)))
            win_rms = norm_rms[s_sec:e_sec]
            if len(win_rms) > 0:
                mean_e = float(np.mean(win_rms))
                peak_e = float(np.max(win_rms))
                std_e = float(np.std(win_rms))
                score = round(0.45 * mean_e + 0.40 * peak_e + 0.15 * std_e, 1)
            else:
                score = 50.0
                mean_e, peak_e = 50.0, 50.0

            reasons = [
                f"Peak Audio Energy: {peak_e:.1f}/100",
                f"Average Intensity: {mean_e:.1f}/100",
                "High-intensity Action Sequence / Sound Effect Climax",
            ]
        else:
            timeline_fraction = c_start / max(total_duration, 1.0)
            score = round(60.0 + 35.0 * math.sin(timeline_fraction * math.pi), 1)
            reasons = [
                "Cinematic Timeline Highlight",
                f"Video Section: {int(timeline_fraction * 100)}%",
            ]

        raw_clips.append(
            ScoredClip(
                id=idx,
                start=c_start,
                end=c_end,
                duration=round(c_end - c_start, 3),
                text=f"[Action Highlight #{idx}] High Energy Scene at {_fmt_ts(c_start)} ({win_len:.0f}s)",
                score=score,
                reasons=reasons,
                hook=f"Action Climax {_fmt_ts(c_start)} - {_fmt_ts(c_end)}",
            )
        )
        idx += 1
        t += step_sec

    # 3. Non-maximum suppression deduplication
    deduped_clips = _remove_overlaps(raw_clips, overlap_threshold)
    for r, clip in enumerate(deduped_clips, start=1):
        clip.id = r

    # 4. Diversity selection
    final_clips = _select_diverse_candidates(
        deduped_clips,
        top_n=top_n,
        min_score=0.0,
        min_separation=min_separation,
        strategy="spaced_top",
        total_duration=total_duration,
    )
    for r, clip in enumerate(final_clips, start=1):
        clip.id = r

    return deduped_clips, final_clips


def run_selection(
    transcript_path: Optional[Path] = None,
    *,
    min_dur: float = None,
    max_dur: float = None,
    step: float = None,
    top_n: int = None,
    min_score: float = None,
    min_separation: float = None,
    overlap_threshold: float = None,
    strategy: str = None,
    weights: Optional[dict] = None,
    json_out: Optional[Path] = None,
    txt_out: Optional[Path] = None,
) -> dict:
    """
    Run Phase 3 Clip Selection Pipeline.
    Supports conversational podcasts and non-dialogue / action / fight / music videos.
    """
    _cfg = get_clip_selection_config()
    if min_dur is None:
        min_dur = _cfg["clip_min_duration"]
    if max_dur is None:
        max_dur = _cfg["clip_max_duration"]
    if step is None:
        step = _cfg["clip_step_size"]
    if top_n is None:
        top_n = _cfg["clip_top_n"]
    if min_score is None:
        min_score = _cfg["clip_min_score"]
    if min_separation is None:
        min_separation = _cfg["clip_min_separation"]
    if overlap_threshold is None:
        overlap_threshold = _cfg["clip_overlap_threshold"]
    if strategy is None:
        strategy = _cfg["clip_distribution_strategy"]
    t_path = transcript_path or (TEMP_DIR / TRANSCRIPT_JSON_FILENAME)
    segments, meta = _load_transcript(t_path)
    total_duration = float(meta.get("duration_secs", segments[-1].end if segments else 0.0))

    log.info("Processing transcript with %d segments (total duration %.1fs)", len(segments), total_duration)

    # ── Non-Dialogue / Action Video Path ───────────────────────────────────────
    if len(segments) == 0:
        log.info("⚡ Zero speech segments detected — Activating Audio-Energy Scene Highlight Detection Engine...")
        print("\n  [Action Engine] Detecting high-energy battle/action climaxes via audio-energy analysis …")
        deduped_clips, final_clips = _generate_energy_based_candidates(
            meta=meta,
            total_duration=total_duration,
            min_dur=min_dur,
            max_dur=max_dur,
            top_n=top_n,
            min_separation=min_separation,
            overlap_threshold=overlap_threshold,
        )

        pool_out = TEMP_DIR / CANDIDATE_POOL_JSON_FILENAME
        _save_json(deduped_clips, meta, pool_out)
        log.info("Saved complete candidate pool -> %s (%d clips)", pool_out, len(deduped_clips))

        scores = [c.score for c in final_clips] if final_clips else [0.0]
        score_dist = {
            "min": min(scores),
            "max": max(scores),
            "mean": round(sum(scores) / len(scores), 1),
            "median": round(sorted(scores)[len(scores) // 2], 1),
        }

        timeline_dist = [
            {"id": c.id, "start": _fmt_ts(c.start), "end": _fmt_ts(c.end), "score": c.score, "hook": c.hook}
            for c in sorted(final_clips, key=lambda x: x.start)
        ]

        j_out = json_out or (TEMP_DIR / CANDIDATES_JSON_FILENAME)
        t_out = txt_out or (TEMP_DIR / CANDIDATES_TXT_FILENAME)
        _save_json(final_clips, meta, j_out)
        _save_txt(final_clips, meta, t_out)

        return {
            "raw_count": len(deduped_clips),
            "valid_count": len(deduped_clips),
            "rejected_count": 0,
            "pool_count": len(deduped_clips),
            "final_count": len(final_clips),
            "score_distribution": score_dist,
            "clips": final_clips,
            "timeline_distribution": timeline_dist,
            "rejection_summary": {},
        }

    # ── Conversational Dialogue Path ───────────────────────────────────────────
    avg_seg_dur = total_duration / max(len(segments), 1)
    is_action_content = avg_seg_dur < 12.0
    min_words_for_validation = 8 if is_action_content else 22
    if is_action_content:
        log.info("Short avg segment duration (%.1fs) detected — using relaxed word-count validation (min_words=%d)",
                 avg_seg_dur, min_words_for_validation)

    # ── Step 1: Window Generation ──────────────────────────────────────────
    print(f"\n  [1/4] Generating sentence-boundary windows ({min_dur:.0f}-{max_dur:.0f}s) …")
    raw_windows = _generate_candidate_windows(segments, min_dur, max_dur, step)
    raw_count = len(raw_windows)
    log.info("Generated %d raw candidate windows", raw_count)

    # ── Step 2: Validation Pass ────────────────────────────────────────────
    print(f"  [2/4] Validating {raw_count} raw candidates against standalone criteria …")
    valid_candidates: list[CandidateClip] = []
    rejection_reasons: dict[str, int] = {}

    for cand in raw_windows:
        is_valid, reason = validate_candidate(cand, min_dur, max_dur, min_words=min_words_for_validation)
        if is_valid:
            valid_candidates.append(cand)
        else:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

    valid_count = len(valid_candidates)
    rejected_count = raw_count - valid_count
    log.info("Validation: %d valid, %d rejected", valid_count, rejected_count)

    # Fallback to energy-based candidates if conversational validation produced 0 valid candidates
    if valid_count == 0:
        log.info("Conversational validation produced 0 candidates — falling back to Audio-Energy Action Selection.")
        deduped_clips, final_clips = _generate_energy_based_candidates(
            meta=meta,
            total_duration=total_duration,
            min_dur=min_dur,
            max_dur=max_dur,
            top_n=top_n,
            min_separation=min_separation,
            overlap_threshold=overlap_threshold,
        )
        pool_out = TEMP_DIR / CANDIDATE_POOL_JSON_FILENAME
        _save_json(deduped_clips, meta, pool_out)
        j_out = json_out or (TEMP_DIR / CANDIDATES_JSON_FILENAME)
        t_out = txt_out or (TEMP_DIR / CANDIDATES_TXT_FILENAME)
        _save_json(final_clips, meta, j_out)
        _save_txt(final_clips, meta, t_out)
        scores = [c.score for c in final_clips] if final_clips else [0.0]
        return {
            "raw_count": len(deduped_clips),
            "valid_count": len(deduped_clips),
            "rejected_count": 0,
            "pool_count": len(deduped_clips),
            "final_count": len(final_clips),
            "score_distribution": {"min": min(scores), "max": max(scores), "mean": round(sum(scores)/len(scores), 1), "median": 0.0},
            "clips": final_clips,
            "timeline_distribution": [],
            "rejection_summary": rejection_reasons,
        }

    # ── Step 3: Scoring & Deduplication ────────────────────────────────────
    print(f"  [3/4] Scoring and deduplicating {valid_count} valid candidates …")
    scored_clips: list[ScoredClip] = []
    for idx, cand in enumerate(valid_candidates):
        score, reasons = score_candidate(cand, weights)
        scored_clips.append(
            ScoredClip(
                id=idx,
                start=round(cand.start, 3),
                end=round(cand.end, 3),
                duration=round(cand.duration, 3),
                text=cand.text,
                score=round(score, 1),
                reasons=reasons,
                hook=cand.hook_sentence,
            )
        )

    # Overlap removal (NMS)
    deduped_clips = _remove_overlaps(scored_clips, overlap_threshold)
    log.info("Deduplication: reduced to %d candidates in full candidate pool", len(deduped_clips))

    # Renumber deduplicated pool candidates by heuristic rank
    for rank, clip in enumerate(deduped_clips, start=1):
        clip.id = rank

    # Save full deduplicated candidate pool to candidate_pool.json
    pool_out = TEMP_DIR / CANDIDATE_POOL_JSON_FILENAME
    _save_json(deduped_clips, meta, pool_out)
    log.info("Saved complete candidate pool -> %s (%d clips)", pool_out, len(deduped_clips))

    # ── Step 4: Diversity-Aware Selection ──────────────────────────────────
    print(f"  [4/4] Applying diversity timeline distribution (strategy='{strategy}', min_separation={min_separation:.0f}s) …")
    final_clips = _select_diverse_candidates(
        deduped_clips,
        top_n=top_n,
        min_score=min_score,
        min_separation=min_separation,
        strategy=strategy,
        total_duration=total_duration,
    )

    # Renumber IDs sequentially by rank (1 to N)
    for rank, clip in enumerate(final_clips, start=1):
        clip.id = rank

    # Compute score statistics
    scores = [c.score for c in final_clips] if final_clips else [0.0]
    score_dist = {
        "min": min(scores),
        "max": max(scores),
        "mean": round(sum(scores) / len(scores), 1),
        "median": round(sorted(scores)[len(scores) // 2], 1),
    }

    # Timeline distribution info
    timeline_dist = [
        {"id": c.id, "start": _fmt_ts(c.start), "end": _fmt_ts(c.end), "score": c.score, "hook": c.hook}
        for c in sorted(final_clips, key=lambda x: x.start)
    ]

    # Save outputs
    j_out = json_out or (TEMP_DIR / CANDIDATES_JSON_FILENAME)
    t_out = txt_out or (TEMP_DIR / CANDIDATES_TXT_FILENAME)
    _save_json(final_clips, meta, j_out)
    _save_txt(final_clips, meta, t_out)

    return {
        "raw_count": raw_count,
        "valid_count": valid_count,
        "rejected_count": rejected_count,
        "pool_count": len(deduped_clips),
        "final_count": len(final_clips),
        "score_distribution": score_dist,
        "clips": final_clips,
        "timeline_distribution": timeline_dist,
        "rejection_summary": rejection_reasons,
    }
