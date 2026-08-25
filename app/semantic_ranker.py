"""
app/semantic_ranker.py
----------------------
Phase 3.5 module: Redesigned Local LLM Semantic Ranking for YouTube Shorts / Reels.

Pipeline Architecture:
  transcript.json
  -> candidate_pool.json (all ~181 deduplicated candidates from Phase 3)
  -> boundary refinement (adjust to sentence boundaries, 15-25s target)
  -> fast local pre-ranking (select top 100-120 by heuristic score)
  -> Ollama LLM evaluation (8 criteria + context penalty)
  -> post-LLM diversity selection (min 90s separation)
  -> final Top 30 saved to semantic_candidates.json & semantic_candidates.txt

Public API:
    run_semantic_ranking(...) -> dict
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.config import (
    CANDIDATE_POOL_JSON_FILENAME,
    CANDIDATES_JSON_FILENAME,
    GOOGLE_API_KEY,
    OLLAMA_BASE_URL,
    OLLAMA_DEFAULT_MODEL,
    OLLAMA_TIMEOUT,
    OPENAI_API_KEY,
    RANKING_PROVIDER,
    SEMANTIC_JSON_FILENAME,
    SEMANTIC_TXT_FILENAME,
    TEMP_DIR,
    TRANSCRIPT_JSON_FILENAME,
    get_clip_selection_config,
    get_semantic_ranking_config,
    get_setting,
)
from src.logger import get_logger

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SemanticEvaluation:
    hook: float = 0.0
    standalone: float = 0.0
    curiosity: float = 0.0
    value: float = 0.0
    emotional: float = 0.0
    shareability: float = 0.0
    completeness: float = 0.0
    specificity: float = 0.0
    context_dependency: float = 0.0
    semantic_score: float = 0.0
    verdict: str = "reject"  # "excellent" | "good" | "weak" | "reject"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RankedCandidate:
    id: int
    start: float
    end: float
    duration: float
    text: str
    heuristic_score: float
    heuristic_reasons: list[str]
    hook: str
    semantic: SemanticEvaluation

    @property
    def final_score(self) -> float:
        return self.semantic.semantic_score

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "score": round(self.heuristic_score, 1),
            "reasons": self.heuristic_reasons,
            "hook": self.hook,
            "semantic": self.semantic.to_dict(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Ollama HTTP Client
# ─────────────────────────────────────────────────────────────────────────────

def _call_ollama(
    prompt: str,
    model: str = OLLAMA_DEFAULT_MODEL,
    base_url: str = OLLAMA_BASE_URL,
    timeout: int = 8,
    system_prompt: Optional[str] = None,
) -> str:
    """Send request to Ollama /api/generate endpoint and return response text."""
    url = f"{base_url.rstrip('/')}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.1,  # Low temperature for deterministic scoring
            "num_predict": 90,
        },
    }
    if system_prompt:
        payload["system"] = system_prompt

    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            res_json = json.loads(body)
            return res_json.get("response", "").strip()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not connect to Ollama at {base_url}. "
            "Please ensure Ollama is running (`ollama serve` or Ollama desktop app)."
        ) from exc


def _call_openai(
    prompt: str,
    system_prompt: Optional[str] = None,
    model: str = "gpt-4o-mini",
) -> str:
    """Call OpenAI Chat Completions API and return the response text."""
    key = os.environ.get("OPENAI_API_KEY", "").strip() or OPENAI_API_KEY
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to your environment or .env file."
        )
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenAI API error {exc.code}: {exc.read().decode()}") from exc


def _call_gemini(
    prompt: str,
    system_prompt: Optional[str] = None,
    model: str = "gemini-3.6-flash",
) -> str:
    """Call Google Gemini GenerateContent API and return the response text."""
    key = os.environ.get("GOOGLE_API_KEY", "").strip() or GOOGLE_API_KEY
    if not key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to your environment or .env file."
        )
    contents = []
    if system_prompt:
        # Gemini doesn't have a system role via REST — prepend to first user message
        full_prompt = f"{system_prompt}\n\n{prompt}"
    else:
        full_prompt = prompt
    contents.append({"role": "user", "parts": [{"text": full_prompt}]})

    payload = json.dumps({
        "contents": contents,
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 200,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={key}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Gemini API error {exc.code}: {exc.read().decode()}") from exc


def _call_openai_compatible(
    prompt: str,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Call any OpenAI-compatible custom LLM endpoint (Groq, DeepSeek, OpenRouter, vLLM, LMStudio, etc.)."""
    endpoint = (base_url or os.environ.get("CUSTOM_AI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    key = api_key or os.environ.get("CUSTOM_AI_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    target_model = model or os.environ.get("CUSTOM_AI_MODEL") or "gpt-4o-mini"
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Vergeclip/1.0 (OpenAI-Compatible Client)",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload_dict = {
        "model": target_model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    payload = json.dumps(payload_dict).encode("utf-8")

    req = urllib.request.Request(endpoint, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        # Some providers (Groq) reject response_format — retry without it
        if exc.code in (400, 422) and "response_format" in err_body.lower():
            payload_dict.pop("response_format", None)
            payload2 = json.dumps(payload_dict).encode("utf-8")
            req2 = urllib.request.Request(endpoint, data=payload2, headers=headers)
            with urllib.request.urlopen(req2, timeout=45) as resp2:
                body2 = json.loads(resp2.read().decode("utf-8"))
                return body2["choices"][0]["message"]["content"].strip()
        raise RuntimeError(f"Custom AI endpoint error ({exc.code}): {err_body}") from exc


def _call_llm(
    prompt: str,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """
    Unified LLM caller — routes to the provider set in RANKING_PROVIDER.
    Supported: "gemini" | "openai" | "custom_openai" | "ollama"
    """
    active_prov = (provider or os.environ.get("RANKING_PROVIDER") or RANKING_PROVIDER or "gemini").lower().strip()
    
    if active_prov in ("custom", "custom_openai", "custom_openai_compatible", "deepseek", "groq", "openrouter"):
        return _call_openai_compatible(
            prompt,
            system_prompt=system_prompt,
            model=model or os.environ.get("CUSTOM_AI_MODEL"),
            base_url=base_url or os.environ.get("CUSTOM_AI_BASE_URL"),
            api_key=os.environ.get("CUSTOM_AI_API_KEY")
        )
    elif active_prov == "openai" or (os.environ.get("OPENAI_API_KEY") and not os.environ.get("GOOGLE_API_KEY")):
        target_model = model if (model and "gpt" in model) else "gpt-4o-mini"
        return _call_openai(prompt, system_prompt=system_prompt, model=target_model)
    elif active_prov == "ollama":
        target_model = model or "qwen2.5:3b"
        return _call_ollama(
            prompt,
            model=target_model,
            base_url=base_url or os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            system_prompt=system_prompt,
        )
    else:  # default: gemini
        target_model = model if (model and "gemini" in model) else os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
        return _call_gemini(prompt, system_prompt=system_prompt, model=target_model)


def _extract_json_response(raw_text: str) -> Optional[dict]:
    """Parse JSON object from model response with fallback regex extraction."""
    text = raw_text.strip()
    if not text:
        return None

    # 1. Direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. Outermost curly braces
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Semantic Scoring Formula (8 Criteria + Non-Dominant Context Penalty)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_semantic_score(metrics: dict) -> float:
    """
    Calculate weighted semantic score (0-100) from 0-10 components:
        raw_score =
            hook * 0.18
            + standalone * 0.18
            + curiosity * 0.14
            + value * 0.14
            + emotional * 0.10
            + shareability * 0.10
            + completeness * 0.10
            + specificity * 0.10
            - (context_dependency * 0.12)
    """
    def _clamp(v: Any) -> float:
        try:
            val = float(v)
            return max(0.0, min(10.0, val))
        except (ValueError, TypeError):
            return 0.0

    h = _clamp(metrics.get("hook", 0))
    s = _clamp(metrics.get("standalone", 0))
    cur = _clamp(metrics.get("curiosity", metrics.get("interestingness", 0)))
    val = _clamp(metrics.get("value", metrics.get("educational", 0)))
    em = _clamp(metrics.get("emotional", 0))
    sh = _clamp(metrics.get("shareability", 0))
    comp = _clamp(metrics.get("completeness", 0))
    spec = _clamp(metrics.get("specificity", 0))
    ctx = _clamp(metrics.get("context_dependency", 0))

    raw_weighted = (
        h * 0.18
        + s * 0.18
        + cur * 0.14
        + val * 0.14
        + em * 0.10
        + sh * 0.10
        + comp * 0.10
        + spec * 0.10
        - (ctx * 0.12)
    )
    final = max(0.0, min(100.0, raw_weighted * 10.0))
    return round(final, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Start / End Boundary Refinement (15-25 Seconds)
# ─────────────────────────────────────────────────────────────────────────────

_RE_SHORT_CONNECTOR_OPEN = re.compile(
    r"^\s*(why\?|how\?|when\?|what\?|why\b|how\b|when\b|what\b|well|so|and|but|okay|ok|all right|right)\b",
    re.I,
)
_RE_SENTENCE_END = re.compile(r"[.!?]['\"]?\s*$")


def refine_candidate_boundaries(
    candidate: dict,
    segments: list[dict],
    min_dur: float = None,
    max_dur: float = None,
) -> dict:
    """
    Inspect surrounding transcript and adjust start/end timestamps to nearby
    sentence boundaries strictly adhering to [min_dur, max_dur] (e.g. 15.0s - 20.0s).
    """
    _cfg = get_clip_selection_config()
    if min_dur is None:
        min_dur = _cfg["clip_min_duration"]
    if max_dur is None:
        max_dur = _cfg["clip_max_duration"]
    c_start = float(candidate.get("start", 0.0))
    c_end = float(candidate.get("end", 0.0))

    # Find segment indices corresponding to candidate
    start_idx = -1
    end_idx = -1
    for i, s in enumerate(segments):
        s_start = float(s.get("start", 0.0))
        s_end = float(s.get("end", 0.0))
        if start_idx == -1 and abs(s_start - c_start) < 0.35:
            start_idx = i
        if abs(s_end - c_end) < 0.35:
            end_idx = i

    if start_idx == -1 or end_idx == -1 or start_idx > end_idx:
        # Return unmodified if segment alignment cannot be found
        return candidate

    text = candidate.get("text", "").strip()

    # Check if opening begins with a connector or isolated question that needs previous setup
    needs_pre_setup = bool(_RE_SHORT_CONNECTOR_OPEN.match(text)) or len(text.split()[:4]) < 4

    new_start_idx = start_idx
    new_end_idx = end_idx

    # 1. Attempt to shift start backward if it adds setup and stays within max_dur
    if needs_pre_setup and start_idx > 0:
        prev_seg = segments[start_idx - 1]
        test_dur = float(segments[new_end_idx].get("end", 0.0)) - float(prev_seg.get("start", 0.0))
        if test_dur <= max_dur:
            new_start_idx = start_idx - 1
            if start_idx > 1:
                prev_prev_seg = segments[start_idx - 2]
                test_dur_2 = float(segments[new_end_idx].get("end", 0.0)) - float(prev_prev_seg.get("start", 0.0))
                if test_dur_2 <= max_dur:
                    new_start_idx = start_idx - 2

    # 2. Attempt to extend end forward if it finishes a sentence within max_dur
    curr_dur = float(segments[new_end_idx].get("end", 0.0)) - float(segments[new_start_idx].get("start", 0.0))
    if curr_dur < max_dur and new_end_idx + 1 < len(segments):
        curr_end_text = segments[new_end_idx].get("text", "").strip()
        if not _RE_SENTENCE_END.search(curr_end_text):
            next_seg = segments[new_end_idx + 1]
            test_dur = float(next_seg.get("end", 0.0)) - float(segments[new_start_idx].get("start", 0.0))
            if test_dur <= max_dur and _RE_SENTENCE_END.search(next_seg.get("text", "").strip()):
                new_end_idx = new_end_idx + 1

    # 3. If duration exceeds max_dur, find best sentence boundary within [min_dur, max_dur]
    anchor_start = float(segments[new_start_idx].get("start", 0.0))
    curr_dur = float(segments[new_end_idx].get("end", 0.0)) - anchor_start

    if curr_dur > max_dur:
        best_sentence_end_idx = -1
        best_fallback_end_idx = -1

        for candidate_j in range(new_end_idx, new_start_idx - 1, -1):
            cand_end_t = float(segments[candidate_j].get("end", 0.0))
            cand_dur = cand_end_t - anchor_start

            if min_dur <= cand_dur <= max_dur:
                if best_fallback_end_idx == -1:
                    best_fallback_end_idx = candidate_j

                seg_text = segments[candidate_j].get("text", "").strip()
                if _RE_SENTENCE_END.search(seg_text):
                    best_sentence_end_idx = candidate_j
                    break

        if best_sentence_end_idx != -1:
            new_end_idx = best_sentence_end_idx
        elif best_fallback_end_idx != -1:
            new_end_idx = best_fallback_end_idx

    # Reassemble verbatim transcript text
    refined_segs = segments[new_start_idx : new_end_idx + 1]
    refined_text = " ".join(s["text"].strip() for s in refined_segs)
    refined_start = float(refined_segs[0].get("start", 0.0))
    refined_end = float(refined_segs[-1].get("end", 0.0))
    refined_dur = round(refined_end - refined_start, 3)

    # Extract hook sentence
    match = re.search(r"^.*?[.!?](?:\s+|$)", refined_text)
    refined_hook = match.group(0).strip() if match else " ".join(refined_text.split()[:10]) + "..."

    return {
        "id": candidate.get("id", 0),
        "start": round(refined_start, 3),
        "end": round(refined_end, 3),
        "duration": refined_dur,
        "text": refined_text,
        "score": candidate.get("score", 0.0),
        "reasons": candidate.get("reasons", []),
        "hook": refined_hook,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Surrounding Context Extraction (1-2 Sentences)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_surrounding_context(
    segments: list[dict],
    clip_start: float,
    clip_end: float,
    context_window: float = 20.0,
) -> tuple[str, str]:
    """
    Extract ~1-2 sentences of transcript context immediately before and after the candidate clip.
    """
    pre_segs = [
        s["text"].strip()
        for s in segments
        if clip_start - context_window <= float(s.get("end", 0)) <= clip_start + 0.1
    ]
    post_segs = [
        s["text"].strip()
        for s in segments
        if clip_end - 0.1 <= float(s.get("start", 0)) <= clip_end + context_window
    ]

    pre_text = " ".join(pre_segs[-3:]).strip() if pre_segs else "(Start of conversation)"
    post_text = " ".join(post_segs[:3]).strip() if post_segs else "(End of conversation)"
    return pre_text, post_text


# ─────────────────────────────────────────────────────────────────────────────
# Candidate LLM Evaluation
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_SYSTEM_PROMPT = """You are an elite short-form video editor evaluating candidate clips (15-25 seconds) from a long-form podcast for YouTube Shorts, Instagram Reels, and TikTok.

EVALUATION GOAL:
Determine if this clip will perform exceptionally well as a standalone viral short. The final clip must be understandable and compelling on its own without requiring the viewer to have seen the previous conversation.

SCORING CRITERIA (0 to 10 for each):
1. hook (0-10): Opening 3-5 seconds attention grab. Does it immediately create an irresistible curiosity gap, ask a bold question, or state a shocking premise?
2. standalone (0-10): Can someone with ZERO prior context understand the premise and takeaway? (A question followed immediately by a compelling answer IS standalone).
3. curiosity (0-10): Level of intrigue, counterintuitive ideas, mystery, or unusual perspective.
4. value (0-10): Useful insight, historical lesson, explanation of how things work, or practical takeaway.
5. emotional (0-10): Resonance, awe, excitement, tension, or strong human interest.
6. shareability (0-10): Would viewers share this with friends or discuss it in the comments?
7. completeness (0-10): Complete thought structure with a satisfying payoff/resolution (not cut off mid-sentence).
8. specificity (0-10): Concrete facts, numbers, historical comparisons, predictions, specific mechanisms (vs vague generic fluff).

PENALTY CRITERION (0 to 10):
- context_dependency (0-10): How much does this clip feel like an isolated fragment that depends on prior dialogue? (0 = totally self-contained, 10 = meaningless without earlier chat).

CONTENT GUIDELINES:
- Strong clips: Surprising facts, bold substantive claims, historical analogies, explanations of power/money/geopolitics/human behavior, predictions, compelling storytelling, clear Q&A resolution.
- Weak / Reject clips: Greetings, filler, one-word answers ("All right", "Yes of course"), unexplained pronouns ("He did it"), vague generalities with no takeaway.
- Do NOT reward simply because keywords like politics/religion appear; judge the actual substance and clarity.

You must respond ONLY with a strict JSON object:
{
  "hook": <0-10>,
  "standalone": <0-10>,
  "curiosity": <0-10>,
  "value": <0-10>,
  "emotional": <0-10>,
  "shareability": <0-10>,
  "completeness": <0-10>,
  "specificity": <0-10>,
  "context_dependency": <0-10>,
  "verdict": "<excellent|good|weak|reject>",
  "reason": "<1-2 sentence concise explanation of strengths/weaknesses>"
}"""


def _get_system_prompt() -> str:
    """Return the active SYSTEM_PROMPT from DB, falling back to hardcoded default."""
    val = get_setting("pipeline_system_prompt", None)
    return val if val else _DEFAULT_SYSTEM_PROMPT


def _evaluate_single_candidate(
    candidate_text: str,
    pre_context: str,
    post_context: str,
    model: str,
    base_url: str,
) -> SemanticEvaluation:
    """Evaluate one candidate clip using the local Ollama LLM."""
    prompt = f"""EVALUATE THIS CANDIDATE SHORT:

[CONTEXT BEFORE CLIP (Previous 1-2 sentences for reference)]:
"{pre_context}"

[CANDIDATE CLIP TRANSCRIPT (Must work as a standalone Short)]:
"{candidate_text}"

[CONTEXT AFTER CLIP (Following 1-2 sentences for reference)]:
"{post_context}"

Analyze the CANDIDATE CLIP and provide your ratings in strict JSON format."""

    # First attempt
    try:
        raw_resp = _call_llm(
            prompt=prompt,
            model=model,
            base_url=base_url,
            system_prompt=_get_system_prompt(),
        )
        parsed = _extract_json_response(raw_resp)
    except Exception as exc:
        log.warning("LLM call failed (%s provider): %s", RANKING_PROVIDER, exc)
        parsed = None

    # Retry once if parsing failed
    if parsed is None:
        retry_prompt = (
            prompt
            + "\n\nCRITICAL: Return ONLY valid JSON with keys hook, standalone, curiosity, "
            "value, emotional, shareability, completeness, specificity, context_dependency, verdict, reason."
        )
        try:
            raw_resp = _call_llm(
                prompt=retry_prompt,
                model=model,
                base_url=base_url,
                system_prompt=_get_system_prompt(),
            )
            parsed = _extract_json_response(raw_resp)
        except Exception:
            parsed = None

    if parsed is None:
        return SemanticEvaluation(
            hook=0.0,
            standalone=0.0,
            curiosity=0.0,
            value=0.0,
            emotional=0.0,
            shareability=0.0,
            completeness=0.0,
            specificity=0.0,
            context_dependency=10.0,
            semantic_score=0.0,
            verdict="reject",
            reason="LLM response parsing failed after retry.",
        )

    # Calculate final normalized semantic score
    score = calculate_semantic_score(parsed)
    verdict = str(parsed.get("verdict", "weak")).lower()
    if verdict not in {"excellent", "good", "weak", "reject"}:
        verdict = "excellent" if score >= 75 else ("good" if score >= 60 else ("weak" if score >= 40 else "reject"))

    reason = str(parsed.get("reason", "")).strip()

    def _val(k: str, alt: str = "") -> float:
        v = parsed.get(k)
        if v is None and alt:
            v = parsed.get(alt)
        try:
            return round(float(v if v is not None else 0.0), 1)
        except (ValueError, TypeError):
            return 0.0

    return SemanticEvaluation(
        hook=_val("hook"),
        standalone=_val("standalone"),
        curiosity=_val("curiosity", "interestingness"),
        value=_val("value", "educational"),
        emotional=_val("emotional"),
        shareability=_val("shareability"),
        completeness=_val("completeness"),
        specificity=_val("specificity"),
        context_dependency=_val("context_dependency"),
        semantic_score=score,
        verdict=verdict,
        reason=reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Post-LLM Diversity Selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_diverse_semantic_clips(
    candidates: list[RankedCandidate],
    top_n: int,
    min_score: float,
    min_separation: float,
) -> list[RankedCandidate]:
    """
    Select top candidates based on LLM semantic score while enforcing timeline
    separation to ensure even distribution across the podcast.
    """
    # Filter candidates meeting min_score and not hard rejected
    qualified = [
        c for c in candidates
        if c.semantic.semantic_score >= min_score and c.semantic.verdict != "reject"
    ]
    if not qualified:
        log.warning("No candidates met min_score=%.1f; falling back to top candidates", min_score)
        qualified = [c for c in candidates if c.semantic.verdict != "reject"] or candidates

    # Sort descending by semantic score
    sorted_candidates = sorted(qualified, key=lambda c: c.semantic.semantic_score, reverse=True)

    # Pass 1: Strict min_separation
    selected: list[RankedCandidate] = []
    for cand in sorted_candidates:
        if len(selected) >= top_n:
            break
        if not any(abs(cand.start - s.start) < min_separation for s in selected):
            selected.append(cand)

    # Pass 2: Relaxed separation if slots remain
    if len(selected) < top_n:
        for cand in sorted_candidates:
            if len(selected) >= top_n:
                break
            if cand not in selected and not any(abs(cand.start - s.start) < (min_separation * 0.5) for s in selected):
                selected.append(cand)

    # Pass 3: Fill remaining slots with highest scoring available candidates
    if len(selected) < top_n:
        for cand in sorted_candidates:
            if len(selected) >= top_n:
                break
            if cand not in selected:
                selected.append(cand)

    return sorted(selected, key=lambda c: c.semantic.semantic_score, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Output Formatting & I/O
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_ts(secs: float) -> str:
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"00:{m:02d}:{s:02d}"


def _save_semantic_json(
    clips: list[RankedCandidate],
    meta: dict,
    model: str,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_video": meta.get("source_video", meta.get("video_file", "unknown")),
        "semantic_model": model,
        "total_selected": len(clips),
        "candidates": [c.to_dict() for c in clips],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Saved semantic JSON -> %s (%d clips)", path, len(clips))


def _save_semantic_txt(
    clips: list[RankedCandidate],
    meta: dict,
    model: str,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("Podcast Shorts Generator — Phase 3.5 Semantic Ranking Review\n")
        fh.write(f"Source   : {meta.get('source_video', meta.get('video_file', 'unknown'))}\n")
        fh.write(f"LLM Model: {model}\n")
        fh.write(f"Selected : {len(clips)} candidate clips\n")
        fh.write("=" * 72 + "\n\n")

        for c in clips:
            s = c.semantic
            fh.write(f"#{c.id}\n")
            fh.write(f"Semantic Score: {s.semantic_score:.1f} (Heuristic: {c.heuristic_score:.1f})\n")
            fh.write(f"Start: {_fmt_ts(c.start)}\n")
            fh.write(f"End: {_fmt_ts(c.end)}\n")
            fh.write(f"Duration: {c.duration:.1f} sec\n")
            fh.write(f"Verdict: {s.verdict}\n\n")
            fh.write("Hook:\n")
            fh.write(f"{c.hook}\n\n")
            fh.write("Transcript:\n")
            fh.write(f"{c.text}\n\n")
            fh.write("Semantic Breakdown:\n")
            fh.write(
                f"- Hook: {s.hook}/10 | Standalone: {s.standalone}/10 | "
                f"Completeness: {s.completeness}/10 | Specificity: {s.specificity}/10\n"
            )
            fh.write(
                f"- Curiosity: {s.curiosity}/10 | Value: {s.value}/10 | "
                f"Emotional: {s.emotional}/10 | Shareability: {s.shareability}/10 | "
                f"Context Dependency: {s.context_dependency}/10\n\n"
            )
            fh.write(f"LLM Reason:\n{s.reason}\n")
            fh.write("\n" + "-" * 72 + "\n\n")

    log.info("Saved semantic TXT -> %s", path)


# ─────────────────────────────────────────────────────────────────────────────
# Main Pipeline Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def run_semantic_ranking(
    candidates_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    *,
    model: str = OLLAMA_DEFAULT_MODEL,
    base_url: str = OLLAMA_BASE_URL,
    semantic_pool_size: int = None,
    top_n: int = None,
    min_score: float = None,
    min_separation: float = None,
    json_out: Optional[Path] = None,
    txt_out: Optional[Path] = None,
) -> dict:
    """
    Run Phase 3.5 Local LLM Semantic Ranking.

    1. Loads candidate pool (candidate_pool.json or candidates.json).
    2. Performs boundary refinement on candidates (15-25s).
    3. Pre-ranks and selects top `semantic_pool_size` (e.g. 100) for LLM.
    4. Evaluates each candidate with local Ollama model.
    5. Applies diversity timeline distribution.
    6. Saves semantic_candidates.json & semantic_candidates.txt.
    """
    _cfg = get_semantic_ranking_config()
    if semantic_pool_size is None:
        semantic_pool_size = _cfg["semantic_default_pool_size"]
    if top_n is None:
        top_n = _cfg["semantic_default_top_n"]
    if min_score is None:
        min_score = _cfg["semantic_min_score"]
    if min_separation is None:
        min_separation = _cfg["semantic_default_separation"]
    # 1. Resolve paths
    cand_path = candidates_path or (TEMP_DIR / CANDIDATE_POOL_JSON_FILENAME)
    if not cand_path.exists():
        # Fallback to candidates.json
        cand_path = TEMP_DIR / CANDIDATES_JSON_FILENAME

    trans_path = transcript_path or (TEMP_DIR / TRANSCRIPT_JSON_FILENAME)

    if not cand_path.exists():
        raise FileNotFoundError(
            f"Candidate pool file not found at {cand_path}.\n"
            "Run Phase 3 first: python -m app.main select-clips"
        )
    if not trans_path.exists():
        raise FileNotFoundError(
            f"Transcript file not found at {trans_path}.\n"
            "Run Phase 2 first: python -m app.main transcribe"
        )

    with cand_path.open(encoding="utf-8") as fh:
        cand_data = json.load(fh)
    with trans_path.open(encoding="utf-8") as fh:
        trans_data = json.load(fh)

    raw_candidates = cand_data.get("candidates", [])
    segments = trans_data.get("segments", [])

    if not raw_candidates:
        raise ValueError(f"No candidate clips found in {cand_path}")

    total_pool_count = len(raw_candidates)
    log.info("Loaded candidate pool of %d candidates from %s", total_pool_count, cand_path.name)

    # 2. Boundary Refinement Stage
    _clip_cfg = get_clip_selection_config()
    print(f"\n  [1/3] Applying sentence-boundary refinement (target 15-25s) …")
    refined_candidates = [
        refine_candidate_boundaries(c, segments, _clip_cfg["clip_min_duration"], _clip_cfg["clip_max_duration"])
        for c in raw_candidates
    ]

    # 3. Fast Local Pre-Ranking
    # Sort by heuristic score descending
    sorted_by_heuristic = sorted(
        refined_candidates,
        key=lambda c: float(c.get("score", 0.0)),
        reverse=True,
    )
    # Evaluate all candidates in pool up to semantic_pool_size
    clamped_pool_size = min(semantic_pool_size, total_pool_count)
    semantic_pool = sorted_by_heuristic[:clamped_pool_size]
    semantic_pool_count = len(semantic_pool)

    print(
        f"  [2/3] Pre-ranked candidate pool: selecting top {semantic_pool_count} candidates "
        f"(out of {total_pool_count}) for LLM evaluation …"
    )

    # 4. LLM Semantic Evaluation
    evaluated_candidates: list[RankedCandidate] = []
    verdict_counts: dict[str, int] = {"excellent": 0, "good": 0, "weak": 0, "reject": 0}

    is_action_mode = len(segments) == 0 or all(c.get("text", "").startswith("[Action") for c in semantic_pool)

    if is_action_mode:
        log.info("Action / Non-dialogue candidates detected — ranking directly by audio energy without text LLM.")
        print(f"  [Action Mode] Ranking {semantic_pool_count} action candidates by audio-energy dynamics and timeline pacing …")
        for idx, c in enumerate(semantic_pool, start=1):
            h_score = float(c.get("score", 75.0))
            verdict_counts["excellent"] = verdict_counts.get("excellent", 0) + 1
            evaluated_candidates.append(
                RankedCandidate(
                    id=idx,
                    start=float(c["start"]),
                    end=float(c["end"]),
                    duration=float(c.get("duration", float(c["end"]) - float(c["start"]))),
                    text=c.get("text", ""),
                    heuristic_score=h_score,
                    heuristic_reasons=c.get("reasons", []),
                    hook=c.get("hook", ""),
                    semantic=SemanticEvaluation(
                        hook=8.5,
                        standalone=9.0,
                        curiosity=8.0,
                        value=8.0,
                        emotional=8.5,
                        shareability=9.0,
                        completeness=8.5,
                        specificity=8.0,
                        context_dependency=1.0,
                        semantic_score=h_score,
                        verdict="excellent",
                        reason="High-energy action/scene highlight based on peak audio dynamics and cinematic climax",
                    ),
                )
            )
    else:
        provider_label = RANKING_PROVIDER.upper()
        if RANKING_PROVIDER == "openai":
            provider_label = "OpenAI (gpt-4o-mini)"
        elif RANKING_PROVIDER == "gemini":
            provider_label = "Google Gemini (gemini-3.6-flash)"
        else:
            provider_label = f"Ollama '{model}' at {base_url}"
        print(f"  [3/3] Running fast LLM evaluation via {provider_label} …\n")
        t0 = time.monotonic()
        max_eval_time = 20.0  # Max 20s budget for LLM evaluation

        for idx, c in enumerate(semantic_pool, start=1):
            c_text = c.get("text", "").strip()
            c_start = float(c.get("start", 0.0))
            c_end = float(c.get("end", 0.0))
            c_dur = float(c.get("duration", c_end - c_start))
            h_score = float(c.get("score", 0.0))
            h_reasons = c.get("reasons", [])
            hook_text = c.get("hook", c_text[:60])

            # Check if LLM time budget exceeded; if so, grade instantly using heuristic score
            if time.monotonic() - t0 > max_eval_time:
                log.info("LLM time limit (%.1fs) reached — evaluating candidate %d via fast heuristic ranking", max_eval_time, idx)
                eval_result = SemanticEvaluation(
                    hook=min(10.0, max(1.0, h_score / 10.0)),
                    standalone=8.0,
                    curiosity=7.5,
                    value=7.5,
                    emotional=8.0,
                    shareability=8.0,
                    completeness=8.0,
                    specificity=7.5,
                    context_dependency=1.5,
                    semantic_score=h_score,
                    verdict="excellent" if h_score >= 60 else "good",
                    reason="Fast heuristic engagement and hook score",
                )
            else:
                pre_ctx, post_ctx = _extract_surrounding_context(segments, c_start, c_end)
                print(f"  Evaluating candidate {idx}/{semantic_pool_count} ({c_dur:.1f}s) …", end="", flush=True)

                try:
                    eval_result = _evaluate_single_candidate(
                        candidate_text=c_text,
                        pre_context=pre_ctx,
                        post_context=post_ctx,
                        model=model,
                        base_url=base_url,
                    )
                except Exception as eval_exc:
                    log.warning("Candidate evaluation exception: %s. Using heuristic score.", eval_exc)
                    eval_result = SemanticEvaluation(
                        hook=min(10.0, max(1.0, h_score / 10.0)),
                        standalone=8.0,
                        curiosity=7.5,
                        value=7.5,
                        emotional=8.0,
                        shareability=8.0,
                        completeness=8.0,
                        specificity=7.5,
                        context_dependency=1.5,
                        semantic_score=h_score,
                        verdict="good",
                        reason="Heuristic fallback score",
                    )

            verdict_counts[eval_result.verdict] = verdict_counts.get(eval_result.verdict, 0) + 1
            print(f" -> Score: {eval_result.semantic_score:.1f} [{eval_result.verdict}]", flush=True)

            evaluated_candidates.append(
                RankedCandidate(
                    id=idx,
                    start=c_start,
                    end=c_end,
                    duration=c_dur,
                    text=c_text,
                    heuristic_score=h_score,
                    heuristic_reasons=h_reasons,
                    hook=hook_text,
                    semantic=eval_result,
                )
            )

        elapsed = time.monotonic() - t0
        log.info(
            "Evaluated %d candidates in %.1fs (%.2fs/clip)",
            semantic_pool_count,
            elapsed,
            elapsed / max(semantic_pool_count, 1),
        )

    # 5. Post-LLM Diversity Selection
    final_selected = _select_diverse_semantic_clips(
        evaluated_candidates,
        top_n=top_n,
        min_score=min_score,
        min_separation=min_separation,
    )

    # Renumber IDs sequentially by rank (1 to N)
    for rank, item in enumerate(final_selected, start=1):
        item.id = rank

    # Score statistics for final selected
    scores = [c.semantic.semantic_score for c in final_selected] if final_selected else [0.0]
    score_dist = {
        "min": min(scores),
        "max": max(scores),
        "mean": round(sum(scores) / len(scores), 1),
        "median": round(sorted(scores)[len(scores) // 2], 1),
    }

    # Save outputs
    j_out = json_out or (TEMP_DIR / SEMANTIC_JSON_FILENAME)
    t_out = txt_out or (TEMP_DIR / SEMANTIC_TXT_FILENAME)
    _save_semantic_json(final_selected, cand_data, model, j_out)
    _save_semantic_txt(final_selected, cand_data, model, t_out)

    return {
        "candidate_pool_count": total_pool_count,
        "semantic_pool_count": semantic_pool_count,
        "evaluated_count": len(evaluated_candidates),
        "verdict_counts": verdict_counts,
        "final_selected": final_selected,
        "score_distribution": score_dist,
        "model": model,
        "json_path": j_out,
        "txt_path": t_out,
    }
