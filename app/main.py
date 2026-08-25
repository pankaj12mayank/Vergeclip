"""
app/main.py
-----------
Unified CLI entry point for the Podcast Shorts Generator.

Usage
─────
  python -m app.main <command> [options]

Commands
────────
  download      Phase 1 — Download a YouTube video (wraps src/downloader.py)
  inspect       Phase 1 — Inspect a video file with FFprobe
  transcribe    Phase 2 — Transcribe a video with faster-whisper
  select-clips  Phase 3 — Score and select the best candidate clips
  rank-clips    Phase 3.5 — LLM Semantic Ranking using local Ollama
  render-test   Phase 4 — Render one test short
  render-batch  Phase 4 — Render all selected shorts

Examples
────────
  python -m app.main render-test
  python -m app.main render-batch
  python -m app.main render-batch --start-rank 1 --end-rank 10
  python -m app.main render-batch --overwrite

Run `python -m app.main <command> --help` for per-command options.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    class _ColorFallback:
        def __getattr__(self, name):
            return ""
    Fore = _ColorFallback()
    Style = _ColorFallback()

# Force UTF-8 output on Windows so the banner/logs render correctly
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Python version guard ───────────────────────────────────────────────────────
if sys.version_info < (3, 11):
    sys.exit(
        f"Python 3.11+ required (found {sys.version}). Please upgrade."
    )

from src.logger import get_logger

log = get_logger("app.main")

# ── Banner ─────────────────────────────────────────────────────────────────────
BANNER = f"""\
{Fore.CYAN}+--------------------------------------------------+
|      Podcast Shorts Generator                    |
|      Phase 1: Download & Inspect                 |
|      Phase 2: Transcription                      |
|      Phase 3: Clip Selection                     |
|      Phase 3.5: LLM Semantic Ranking             |
|      Phase 4: Render Test                        |
|      Phase 4: Batch Rendering <- new             |
+--------------------------------------------------+{Style.RESET_ALL}"""


# ── Sub-command handlers ───────────────────────────────────────────────────────

def _cmd_download(args: argparse.Namespace) -> int:
    """Phase 1 — Download a YouTube video."""
    from src.downloader import download_video
    from src.inspector import inspect_video, print_video_info

    url = args.url
    if not url:
        print(f"{Fore.YELLOW}Paste the YouTube URL below.{Style.RESET_ALL}")
        try:
            url = input("  YouTube URL: ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{Fore.RED}Aborted.{Style.RESET_ALL}")
            return 1

    if not url:
        log.error("No URL provided.")
        return 1

    print(f"\n{Fore.CYAN}[ Downloading … ]{Style.RESET_ALL}")
    try:
        video_path = download_video(url)
    except (ValueError, RuntimeError) as exc:
        print(f"\n{Fore.RED}✗ {exc}{Style.RESET_ALL}")
        return 1

    print(f"\n{Fore.GREEN}✓ Saved:{Style.RESET_ALL} {video_path}")

    if not args.no_inspect:
        print(f"\n{Fore.CYAN}[ Inspecting … ]{Style.RESET_ALL}\n")
        try:
            info = inspect_video(video_path)
            print_video_info(info)
        except RuntimeError as exc:
            print(f"\n{Fore.YELLOW}⚠ Inspection skipped:{Style.RESET_ALL} {exc}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Phase 1 — Inspect an existing video with FFprobe."""
    from src.inspector import inspect_video, print_video_info

    if args.file:
        path = Path(args.file)
    else:
        from app.transcriber import load_latest_video
        try:
            path = load_latest_video()
        except FileNotFoundError as exc:
            print(f"\n{Fore.RED}✗ {exc}{Style.RESET_ALL}")
            return 1

    try:
        info = inspect_video(path)
        print_video_info(info)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"\n{Fore.RED}✗ {exc}{Style.RESET_ALL}")
        return 1
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    """Phase 2 — Transcribe with AssemblyAI Cloud API."""
    from app.transcriber import load_latest_video, transcribe_video
    from src.config import TEMP_DIR

    # Resolve video path
    video_path: Path | None = None
    if args.file:
        video_path = Path(args.file)
        if not video_path.exists():
            print(f"\n{Fore.RED}✗ File not found: {video_path}{Style.RESET_ALL}")
            return 1
    else:
        try:
            video_path = load_latest_video()
        except FileNotFoundError as exc:
            print(f"\n{Fore.RED}✗ {exc}{Style.RESET_ALL}")
            return 1

    language = getattr(args, "language", None) or None
    from src.config import get_setting
    _tp = get_setting("transcription_provider", "assemblyai")
    _gm = get_setting("groq_whisper_model", "whisper-large-v3-turbo")
    _tp_label = "Groq Whisper (FREE)" if _tp == "groq" else "AssemblyAI Cloud"

    print(f"\n{Fore.CYAN}[ Phase 2 — {_tp_label} Transcription ]{Style.RESET_ALL}")
    print(f"  Video  : {video_path.name}")
    print(f"  API    : {_tp_label}")
    print(f"  Lang   : {language or 'auto-detect'}\n")

    try:
        result = transcribe_video(
            video_path=video_path,
            provider=_tp,
            model_name=_gm,
            language=language,
            keep_audio=getattr(args, "keep_audio", False),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"\n{Fore.RED}✗ Transcription failed:{Style.RESET_ALL}\n  {exc}")
        log.exception("Transcription error")
        return 1

    # Print summary
    json_out = TEMP_DIR / "transcript.json"
    txt_out  = TEMP_DIR / "transcript.txt"

    print(f"\n{Fore.GREEN}✓ Transcription complete!{Style.RESET_ALL}")
    print(f"  Segments  : {result.num_segments}")
    print(f"  Language  : {result.language}")
    print(f"  Device    : {result.device} ({result.compute_type})")
    print(f"  JSON out  : {json_out}")
    print(f"  TXT  out  : {txt_out}")
    print()
    print("Next step → Run: python -m app.main select-clips")
    return 0


def _cmd_select_clips(args: argparse.Namespace) -> int:
    """Phase 3 — Score and select best clip candidates from the transcript."""
    from app.clip_selector import run_selection, _fmt_ts
    from src.config import (
        CANDIDATES_JSON_FILENAME,
        CANDIDATES_TXT_FILENAME,
        CLIP_DISTRIBUTION_STRATEGY,
        CLIP_MAX_DURATION,
        CLIP_MIN_DURATION,
        CLIP_MIN_SCORE,
        CLIP_MIN_SEPARATION,
        CLIP_STEP_SIZE,
        CLIP_TOP_N,
        TEMP_DIR,
        TRANSCRIPT_JSON_FILENAME,
    )
    from pathlib import Path

    transcript_path = Path(args.transcript) if args.transcript else None
    top_n = args.top_n or CLIP_TOP_N
    min_dur = args.min_dur or CLIP_MIN_DURATION
    max_dur = args.max_dur or CLIP_MAX_DURATION
    min_score = args.min_score if args.min_score is not None else CLIP_MIN_SCORE
    min_separation = args.min_separation if args.min_separation is not None else CLIP_MIN_SEPARATION
    strategy = args.strategy or CLIP_DISTRIBUTION_STRATEGY

    print(f"\n{Fore.CYAN}[ Phase 3 — Clip Selection & Diversity Ranking ]{Style.RESET_ALL}")
    print(f"  Transcript : {transcript_path or TEMP_DIR / TRANSCRIPT_JSON_FILENAME}")
    print(f"  Duration   : {min_dur:.0f}-{max_dur:.0f}s")
    print(f"  Top N      : {top_n}")
    print(f"  Min Score  : {min_score:.1f}")
    print(f"  Separation : {min_separation:.0f}s (strategy='{strategy}')")

    try:
        report = run_selection(
            transcript_path=transcript_path,
            min_dur=min_dur,
            max_dur=max_dur,
            step=args.step or CLIP_STEP_SIZE,
            top_n=top_n,
            min_score=min_score,
            min_separation=min_separation,
            strategy=strategy,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\n{Fore.RED}✗ Clip selection failed:{Style.RESET_ALL}\n  {exc}")
        log.exception("Clip selection error")
        return 1

    json_out = TEMP_DIR / CANDIDATES_JSON_FILENAME
    txt_out  = TEMP_DIR / CANDIDATES_TXT_FILENAME

    print(f"\n{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.GREEN}✓ Phase 3 Selection Complete!{Style.RESET_ALL}")
    print(f"{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"  • Raw Candidate Windows  : {report['raw_count']}")
    print(f"  • Valid Candidates       : {report['valid_count']}")
    print(f"  • Rejected Candidates    : {report['rejected_count']}")
    print(f"  • Final Selected Clips   : {report['final_count']}")
    print()
    print(f"{Fore.CYAN}Score Distribution (Final Selected Clips):{Style.RESET_ALL}")
    dist = report['score_distribution']
    print(f"  • Min Score : {dist['min']:.1f}")
    print(f"  • Max Score : {dist['max']:.1f}")
    print(f"  • Mean      : {dist['mean']:.1f}")
    print(f"  • Median    : {dist['median']:.1f}")
    print()
    print(f"{Fore.CYAN}Selected Clips Distributed Across Podcast Timeline:{Style.RESET_ALL}")
    print(f"  {'#':<4} {'Score':<8} {'Timeline Window':<22} {'Duration':<10} {'Hook'}")
    print(f"  {'-'*75}")
    for clip in sorted(report['clips'], key=lambda c: c.start):
        ts = f"{_fmt_ts(clip.start)} -> {_fmt_ts(clip.end)}"
        hook_preview = clip.hook[:42] + ("..." if len(clip.hook) > 42 else "")
        print(f"  #{clip.id:<3} {clip.score:<8.1f} {ts:<22} {clip.duration:<10.1f}s {hook_preview}")

    print()
    print(f"  JSON output : {json_out}")
    print(f"  TXT review  : {txt_out}")
    print()
    print("Next step → Phase 4 will reframe and render clips with face tracking.")
    return 0


def _cmd_rank_clips(args: argparse.Namespace) -> int:
    """Phase 3.5 — Local LLM Semantic Ranking using Ollama."""
    from app.semantic_ranker import run_semantic_ranking, _fmt_ts
    from src.config import (
        OLLAMA_DEFAULT_MODEL,
        SEMANTIC_DEFAULT_POOL_SIZE,
        SEMANTIC_DEFAULT_SEPARATION,
        SEMANTIC_DEFAULT_TOP_N,
        SEMANTIC_MIN_SCORE,
        TEMP_DIR,
    )
    from pathlib import Path

    model = args.model or OLLAMA_DEFAULT_MODEL
    top_n = args.top_n or SEMANTIC_DEFAULT_TOP_N
    min_score = args.min_score if args.min_score is not None else SEMANTIC_MIN_SCORE
    separation = args.separation if args.separation is not None else SEMANTIC_DEFAULT_SEPARATION
    pool_size = args.semantic_pool_size or SEMANTIC_DEFAULT_POOL_SIZE
    cand_path = Path(args.candidates) if args.candidates else None
    trans_path = Path(args.transcript) if args.transcript else None

    print(f"\n{Fore.CYAN}[ Phase 3.5 — Local LLM Semantic Ranking ]{Style.RESET_ALL}")
    print(f"  Model         : {model}")
    print(f"  Semantic Pool : {pool_size}")
    print(f"  Final Top N   : {top_n}")
    print(f"  Min Score     : {min_score:.1f}")
    print(f"  Separation    : {separation:.0f}s\n")

    try:
        result = run_semantic_ranking(
            candidates_path=cand_path,
            transcript_path=trans_path,
            model=model,
            semantic_pool_size=pool_size,
            top_n=top_n,
            min_score=min_score,
            min_separation=separation,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\n{Fore.RED}✗ Semantic ranking failed:{Style.RESET_ALL}\n  {exc}")
        log.exception("Semantic ranking error")
        return 1

    final_clips = result["final_selected"]
    vcounts = result["verdict_counts"]
    dist = result["score_distribution"]

    print(f"\n{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.GREEN}✓ Phase 3.5 LLM Semantic Ranking Complete!{Style.RESET_ALL}")
    print(f"{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"  • Candidate Pool       : {result['candidate_pool_count']}")
    print(f"  • Semantic Pool        : {result['semantic_pool_count']}")
    print(f"  • LLM Evaluated        : {result['evaluated_count']}")
    print(f"  • Excellent            : {vcounts.get('excellent', 0)}")
    print(f"  • Good                 : {vcounts.get('good', 0)}")
    print(f"  • Weak                 : {vcounts.get('weak', 0)}")
    print(f"  • Rejected             : {vcounts.get('reject', 0)}")
    print(f"  • Final Selected Clips : {len(final_clips)}")
    print()
    print(f"{Fore.CYAN}Score Distribution (Final Selected Clips):{Style.RESET_ALL}")
    print(f"  • Min Score : {dist['min']:.1f}")
    print(f"  • Max Score : {dist['max']:.1f}")
    print(f"  • Mean      : {dist['mean']:.1f}")
    print(f"  • Median    : {dist['median']:.1f}")
    print()

    # Print Top 10 Candidates with full detail
    print(f"{Fore.CYAN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.CYAN}Top 10 Ranked Shorts Candidates:{Style.RESET_ALL}")
    print(f"{Fore.CYAN}===================================================={Style.RESET_ALL}")

    for idx, c in enumerate(final_clips[:10], start=1):
        s = c.semantic
        ts = f"{_fmt_ts(c.start)} -> {_fmt_ts(c.end)}"
        v_color = Fore.GREEN if s.verdict == "excellent" else (Fore.YELLOW if s.verdict == "good" else Fore.WHITE)
        print(f"\n{Fore.CYAN}Rank #{idx} | Semantic Score: {s.semantic_score:.1f}{Style.RESET_ALL} (Heuristic: {c.heuristic_score:.1f})")
        print(f"  Timeline : {ts} ({c.duration:.1f}s) | Verdict: {v_color}{s.verdict.upper()}{Style.RESET_ALL}")
        print(f"  Transcript:\n    \"{c.text}\"")
        print(f"  Reason:\n    {s.reason}")

    print(f"\n{Fore.GREEN}Outputs Saved:{Style.RESET_ALL}")
    print(f"  JSON output : {result['json_path']}")
    print(f"  TXT review  : {result['txt_path']}")
    print()

    # Duration Validation Report
    from src.config import CLIP_MIN_DURATION, CLIP_MAX_DURATION
    durations = [c.duration for c in final_clips] if final_clips else [0.0]
    min_c_dur = min(durations)
    max_c_dur = max(durations)
    valid_count = sum(1 for d in durations if CLIP_MIN_DURATION <= d <= CLIP_MAX_DURATION)
    invalid_count = len(final_clips) - valid_count
    val_status = "PASS" if invalid_count == 0 else "FAIL"

    print(f"{Fore.CYAN}Duration validation:{Style.RESET_ALL}")
    print(f"  Min candidate duration : {min_c_dur:.2f}s")
    print(f"  Max candidate duration : {max_c_dur:.2f}s")
    print(f"  Configured minimum     : {CLIP_MIN_DURATION:.2f}s")
    print(f"  Configured maximum     : {CLIP_MAX_DURATION:.2f}s")
    print(f"  Valid candidates       : {valid_count}")
    print(f"  Invalid candidates     : {invalid_count}")
    print(f"  Status                 : {Fore.GREEN if val_status == 'PASS' else Fore.RED}{val_status}{Style.RESET_ALL}\n")

    print("Next step → Phase 4 will reframe and render clips with face tracking.")
    return 0


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="Podcast Shorts Generator — automated pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m app.main download --url https://youtu.be/dQw4w9WgXcQ\n"
            "  python -m app.main inspect\n"
            "  python -m app.main transcribe\n"
            "  python -m app.main select-clips\n"
            "  python -m app.main rank-clips\n"
            "  python -m app.main rank-clips --model qwen2.5:3b --top-n 30\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # ── download ───────────────────────────────────────────────────────────────
    dl = subparsers.add_parser(
        "download",
        help="Download a YouTube video to input/",
        description="Phase 1 — Download a YouTube (or yt-dlp-supported) video.",
    )
    dl.add_argument("--url", type=str, default=None, help="Video URL.")
    dl.add_argument(
        "--no-inspect",
        action="store_true",
        help="Skip FFprobe inspection after download.",
    )

    # ── inspect ────────────────────────────────────────────────────────────────
    ins = subparsers.add_parser(
        "inspect",
        help="Show FFprobe metadata for a video",
        description="Phase 1 — Inspect a video file with FFprobe.",
    )
    ins.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to video file. Defaults to latest file in input/.",
    )

    # ── transcribe ─────────────────────────────────────────────────────────────
    tr = subparsers.add_parser(
        "transcribe",
        help="Transcribe a video via AssemblyAI Cloud API",
        description=(
            "Phase 2 — Extract audio and transcribe with AssemblyAI Cloud API.\n"
            "Outputs are saved to temp/transcript.json and temp/transcript.txt."
        ),
    )
    tr.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to video file. Defaults to latest file in input/.",
    )
    tr.add_argument(
        "--language",
        type=str,
        default=None,
        metavar="LANG",
        help="Language code, e.g. 'en', 'es', 'fr'. Default: auto-detect.",
    )
    tr.add_argument(
        "--keep-audio",
        action="store_true",
        dest="keep_audio",
        help="Keep the extracted WAV file in temp/ after transcription.",
    )

    # ── select-clips ───────────────────────────────────────────────────────────
    sc = subparsers.add_parser(
        "select-clips",
        help="Score and select clip candidates from the transcript",
        description=(
            "Phase 3 — Load temp/transcript.json, generate candidate clips,\n"
            "score them with heuristics, remove duplicates, and save\n"
            "temp/candidates.json + temp/candidates.txt."
        ),
    )
    sc.add_argument(
        "--transcript",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to transcript.json. Defaults to temp/transcript.json.",
    )
    sc.add_argument(
        "--top-n",
        type=int,
        default=None,
        dest="top_n",
        metavar="N",
        help="Number of top candidates to keep (default: 30).",
    )
    sc.add_argument(
        "--min-dur",
        type=float,
        default=None,
        dest="min_dur",
        metavar="SECS",
        help="Minimum clip duration in seconds (default: 15.0).",
    )
    sc.add_argument(
        "--max-dur",
        type=float,
        default=None,
        dest="max_dur",
        metavar="SECS",
        help="Maximum clip duration in seconds (default: 20.0).",
    )
    sc.add_argument(
        "--step",
        type=float,
        default=None,
        metavar="SECS",
        help="Window advance step size in seconds (default: 1.5).",
    )
    sc.add_argument(
        "--min-score",
        type=float,
        default=None,
        dest="min_score",
        metavar="SCORE",
        help="Minimum score threshold for qualification (default: 30.0).",
    )
    sc.add_argument(
        "--min-separation",
        type=float,
        default=None,
        dest="min_separation",
        metavar="SECS",
        help="Minimum separation between selected clips on timeline (default: 90.0s).",
    )
    sc.add_argument(
        "--strategy",
        type=str,
        default=None,
        choices=["spaced_top", "bucketed"],
        help="Timeline distribution strategy ('spaced_top' or 'bucketed').",
    )

    # ── rank-clips ─────────────────────────────────────────────────────────────
    rc = subparsers.add_parser(
        "rank-clips",
        help="Evaluate and rank candidates using a local Ollama LLM",
        description=(
            "Phase 3.5 — Evaluate candidates from temp/candidate_pool.json using\n"
            "a local Ollama LLM (e.g. qwen2.5:3b), score them on 8 dimensions,\n"
            "and save temp/semantic_candidates.json + temp/semantic_candidates.txt."
        ),
    )
    rc.add_argument(
        "--model",
        type=str,
        default=None,
        help="Ollama model to use (default: qwen2.5:3b).",
    )
    rc.add_argument(
        "--candidates",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to candidate pool JSON (default: temp/candidate_pool.json).",
    )
    rc.add_argument(
        "--transcript",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to transcript.json (default: temp/transcript.json).",
    )
    rc.add_argument(
        "--semantic-pool-size",
        type=int,
        default=None,
        dest="semantic_pool_size",
        metavar="N",
        help="Number of pre-ranked candidates to send to the LLM (default: 100).",
    )
    rc.add_argument(
        "--top-n",
        type=int,
        default=None,
        dest="top_n",
        metavar="N",
        help="Final number of clips to select (default: 30).",
    )
    rc.add_argument(
        "--min-score",
        type=float,
        default=None,
        dest="min_score",
        metavar="SCORE",
        help="Minimum semantic score threshold (default: 40.0).",
    )
    rc.add_argument(
        "--separation",
        type=float,
        default=None,
        dest="separation",
        metavar="SECS",
        help="Minimum separation in seconds between selected clips (default: 90.0s).",
    )

    # ── render-test ────────────────────────────────────────────────────────────
    rt = subparsers.add_parser(
        "render-test",
        help="Render the Rank #1 test short (Phase 4)",
        description=(
            "Phase 4 — Extract Rank #1 from temp/semantic_candidates.json,\n"
            "apply face-tracked 9:16 reframing, burn Whisper captions,\n"
            "and save output/test_short_001.mp4."
        ),
    )
    rt.add_argument(
        "--rank",
        type=int,
        default=1,
        metavar="N",
        help="Rank of candidate to render (default: 1).",
    )
    rt.add_argument(
        "--output",
        type=str,
        default="test_short_001.mp4",
        metavar="FILENAME",
        help="Output filename inside output/ (default: test_short_001.mp4).",
    )
    rt.add_argument(
        "--candidates",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to semantic_candidates.json (default: temp/semantic_candidates.json).",
    )
    rt.add_argument(
        "--transcript",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to transcript.json (default: temp/transcript.json).",
    )
    rt.add_argument(
        "--debug-captions",
        action="store_true",
        dest="debug_captions",
        help="Print detailed word-level caption timing debug information.",
    )

    # ── render-batch ───────────────────────────────────────────────────────────
    rb = subparsers.add_parser(
        "render-batch",
        help="Batch render all selected shorts (Phase 4)",
        description=(
            "Phase 4 — Batch render selected semantic clips with face tracking,\n"
            "9:16 reframing, captions, audio, and validation into output/ directory."
        ),
    )
    rb.add_argument(
        "--candidates",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to semantic_candidates.json (default: temp/semantic_candidates.json).",
    )
    rb.add_argument(
        "--transcript",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to transcript.json (default: temp/transcript.json).",
    )
    rb.add_argument(
        "--output-prefix",
        type=str,
        default="short",
        dest="output_prefix",
        metavar="PREFIX",
        help="Output filename prefix (default: short -> output/short_001.mp4).",
    )
    rb.add_argument(
        "--start-rank",
        type=int,
        default=1,
        dest="start_rank",
        metavar="N",
        help="Starting candidate rank to render (1-indexed, default: 1).",
    )
    rb.add_argument(
        "--end-rank",
        type=int,
        default=None,
        dest="end_rank",
        metavar="N",
        help="Ending candidate rank to render (default: None = all candidates).",
    )
    rb.add_argument(
        "--overwrite",
        action="store_true",
        dest="overwrite",
        help="Overwrite existing output video files.",
    )
    rb.add_argument(
        "--keep-temp",
        action="store_true",
        dest="keep_temp",
        help="Keep per-clip temporary processing directories after rendering.",
    )
    rb.add_argument(
        "--debug-captions",
        action="store_true",
        dest="debug_captions",
        help="Print detailed word-level caption timing debug information.",
    )

    return parser


# ── Entry point ────────────────────────────────────────────────────────────────

def _cmd_render_test(args: argparse.Namespace) -> int:
    """Phase 4 — Render Rank #1 test Short with face tracking, reframing, and captions."""
    from src.renderer import render_test_clip
    from pathlib import Path

    rank = args.rank if hasattr(args, "rank") else 1
    output_fn = args.output if hasattr(args, "output") else "test_short_001.mp4"
    cand_path = Path(args.candidates) if args.candidates else None
    trans_path = Path(args.transcript) if args.transcript else None

    print(f"\n{Fore.CYAN}[ Phase 4 — Video Rendering Test ]{Style.RESET_ALL}")
    print(f"  Rendering Rank #{rank} candidate")
    print(f"  Output: output/{output_fn}")
    print()

    try:
        result = render_test_clip(
            rank=rank,
            output_filename=output_fn,
            candidates_path=cand_path,
            transcript_path=trans_path,
            debug_captions=bool(args.debug_captions),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\n{Fore.RED}\u2717 Phase 4 render failed:{Style.RESET_ALL}\n  {exc}")
        log.exception("Render error")
        return 1

    v = result["validation"]
    face_ok = result["face_coverage_pct"] >= 20.0  # At least 20% face coverage
    is_9_16 = v.get("is_9_16", False)
    is_audio = v.get("is_audio_ok", False)
    has_captions = result["caption_chunks"] > 0
    dur_ok = v.get("is_duration_consistent", v.get("is_duration_ok", False))

    def _status(ok: bool) -> str:
        return f"{Fore.GREEN}PASS{Style.RESET_ALL}" if ok else f"{Fore.RED}FAIL{Style.RESET_ALL}"

    print(f"\n{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.GREEN}PHASE 4 TEST COMPLETE{Style.RESET_ALL}")
    print(f"{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print()
    print(f"Output:")
    print(f"  output/{output_fn}")
    print()
    print(f"Resolution     : {v.get('resolution', 'unknown')}")
    print(f"FPS            : {v.get('fps', '?')}")
    print(f"Expected dur   : {v.get('expected_duration', '?')}s")
    print(f"Actual dur     : {v.get('actual_duration', '?')}s")
    print(f"Duration diff  : {v.get('duration_difference', '?')}s")
    print(f"Video codec    : {v.get('video_codec', 'unknown')}")
    print(f"Audio codec    : {v.get('audio_codec', 'unknown')}")
    print(f"File size      : {v.get('file_size_mb', '?')} MB")
    print()
    print(f"Face tracking  : {_status(face_ok)} ({result['face_coverage_pct']:.1f}% coverage)")
    print(f"9:16           : {_status(is_9_16)}")
    print(f"Captions       : {_status(has_captions)} ({result['caption_chunks']} chunks)")
    print(f"Audio          : {_status(is_audio)}")
    print(f"Duration       : {_status(dur_ok)}")
    print()
    all_pass = all([face_ok, is_9_16, is_audio, has_captions, dur_ok])
    if all_pass:
        print(f"{Fore.GREEN}All validations PASSED. \u2713{Style.RESET_ALL}")
        print("Next step \u2192 Approved! Ready for batch rendering (Phase 4 batch).")
    else:
        print(f"{Fore.YELLOW}Some validations failed. Review the output file before proceeding.{Style.RESET_ALL}")
    return 0


def _cmd_render_batch(args: argparse.Namespace) -> int:
    """Phase 4 — Batch render selected semantic clips."""
    from src.renderer import render_batch
    from src.config import SEMANTIC_JSON_FILENAME, TEMP_DIR, TRANSCRIPT_JSON_FILENAME, OUTPUT_DIR
    from pathlib import Path

    cand_path = Path(args.candidates) if args.candidates else (TEMP_DIR / SEMANTIC_JSON_FILENAME)
    trans_path = Path(args.transcript) if args.transcript else (TEMP_DIR / TRANSCRIPT_JSON_FILENAME)
    prefix = args.output_prefix or "short"
    start_rank = args.start_rank or 1
    end_rank = args.end_rank
    overwrite = bool(args.overwrite)
    keep_temp = bool(args.keep_temp)
    debug_captions = bool(args.debug_captions)

    print(f"\n{Fore.CYAN}[ Phase 4 — Batch Video Rendering ]{Style.RESET_ALL}")
    print(f"  Candidates : {cand_path}")
    print(f"  Transcript : {trans_path}")
    print(f"  Output     : {OUTPUT_DIR}")
    print(f"  Prefix     : {prefix}")
    print(f"  Rank range : {start_rank} -> {end_rank or 'END'}")
    print(f"  Overwrite  : {overwrite}")
    print(f"  Keep temp  : {keep_temp}")
    print(f"  Debug caps : {debug_captions}")

    try:
        summary = render_batch(
            candidates_path=cand_path,
            transcript_path=trans_path,
            output_prefix=prefix,
            start_rank=start_rank,
            end_rank=end_rank,
            overwrite=overwrite,
            keep_temp=keep_temp,
            debug_captions=debug_captions,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"\n{Fore.RED}✗ Batch rendering failed to start:{Style.RESET_ALL}\n  {exc}")
        log.exception("Batch render error")
        return 1

    # Print comprehensive Final Batch Report
    print(f"\n{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.GREEN}PHASE 4 BATCH COMPLETE{Style.RESET_ALL}")
    print(f"{Fore.GREEN}===================================================={Style.RESET_ALL}")
    print(f"  Total selected : {summary['total']}")
    print(f"  Rendered       : {summary['rendered']}")
    print(f"  Skipped        : {summary['skipped']}")
    print(f"  Failed         : {summary['failed']}")
    print()

    if summary["successful_outputs"]:
        print(f"{Fore.CYAN}Successful outputs ({len(summary['successful_outputs'])}):{Style.RESET_ALL}")
        for item in summary["successful_outputs"]:
            if isinstance(item, dict):
                print(f"  ✓ Rank #{item['rank']} -> {item['output_path']}")
            else:
                print(f"  ✓ {item}")
        print()

    if summary["skipped_items"]:
        print(f"{Fore.YELLOW}Skipped ({len(summary['skipped_items'])}):{Style.RESET_ALL}")
        for item in summary["skipped_items"]:
            print(f"  ➜ Rank #{item['rank']} -> {item['reason']}")
        print()

    if summary["failed_items"]:
        print(f"{Fore.RED}Failed ({len(summary['failed_items'])}):{Style.RESET_ALL}")
        for item in summary["failed_items"]:
            print(f"  ✗ Rank #{item['rank']} -> {item['reason']}")
        print()

    print(f"{Fore.CYAN}===================================================={Style.RESET_ALL}")
    print(f"{Fore.CYAN}FINAL VALIDATION SUMMARY{Style.RESET_ALL}")
    print(f"{Fore.CYAN}===================================================={Style.RESET_ALL}")
    print(f"  Passed : {summary['rendered']}")
    print(f"  Failed : {summary['failed']}")
    print(f"  Skipped: {summary['skipped']}")
    print()

    if summary["failed"] == 0:
        print(f"{Fore.GREEN}All batch renders completed successfully. ✓{Style.RESET_ALL}")
        return 0
    else:
        print(f"{Fore.YELLOW}Batch completed with {summary['failed']} error(s). Review the failed clips above.{Style.RESET_ALL}")
        return 1


def main() -> int:
    print(BANNER)
    parser = _build_parser()
    args = parser.parse_args()

    dispatch = {
        "download":     _cmd_download,
        "inspect":      _cmd_inspect,
        "transcribe":   _cmd_transcribe,
        "select-clips": _cmd_select_clips,
        "rank-clips":   _cmd_rank_clips,
        "render-test":  _cmd_render_test,
        "render-batch": _cmd_render_batch,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except KeyboardInterrupt:
        print(f"\n{Fore.RED}Interrupted by user.{Style.RESET_ALL}")
        return 130


if __name__ == "__main__":
    sys.exit(main())

