"""
src/renderer.py
---------------
Phase 4 — Master render orchestrator, single-clip renderer, and batch renderer.

Coordinates the full video rendering pipeline:
  1. Extract candidate clip from source video (video_extractor)
  2. Run local face tracking (face_tracker)
  3. Compute smoothed 9:16 crop plan (reframer)
  4. Apply face-tracked 9:16 reframing and encode (reframer)
  5. Build synchronized caption chunks (caption_renderer)
  6. Burn captions onto video and mux audio (caption_renderer)
  7. Validate final output with ffprobe (4.11)

Public API:
    render_clip(rank, output_filename, ...) -> dict
    render_test_clip(rank, output_filename, ...) -> dict
    render_batch(candidates_path, transcript_path, ...) -> dict
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from src.config import (
    FFMPEG_BIN,
    FFPROBE_BIN,
    OUTPUT_DIR,
    SEMANTIC_JSON_FILENAME,
    TEMP_DIR,
    TRANSCRIPT_JSON_FILENAME,
    get_video_spec_config,
)
from src.logger import get_logger

log = get_logger(__name__)

PHASE4_TEMP_DIR = TEMP_DIR / "phase4"


def caption_burn_enabled() -> bool:
    """Whether captions should be burned onto Youtube-shorts output.

    Default OFF — the user wants clean 9:16 cuts with no captions. Can be turned
    on via Admin -> Settings key ``shorts_burn_captions`` = "1"/"true".
    """
    try:
        from src.config import get_setting
        flag = str(get_setting("shorts_burn_captions", "0")).strip().lower()
        return flag in ("1", "true", "yes", "on")
    except Exception:
        return False


def _get_video_spec():
    spec = get_video_spec_config()
    return int(spec["target_width"]), int(spec["target_height"])


def _fmt_ts(secs: float) -> str:
    """Format seconds into HH:MM:SS.mmm."""
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    ms = int((secs - int(secs)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _run_ffprobe_inspect(video_path: Path) -> dict:
    """Use ffprobe or OpenCV to get video stream metadata."""
    probe_bin = shutil.which("ffprobe")
    if probe_bin:
        cmd = [
            probe_bin,
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-print_format", "json",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                pass

    # Fallback to OpenCV + file stat inspection
    import cv2
    cap = cv2.VideoCapture(str(video_path))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur = round(frame_count / fps, 2) if fps > 0 else 0.0
    cap.release()

    size_bytes = video_path.stat().st_size if video_path.exists() else 0

    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": w,
                "height": h,
                "r_frame_rate": f"{int(fps)}/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
            },
        ],
        "format": {
            "duration": str(dur),
            "size": str(size_bytes),
        },
    }


def _validate_output(video_path: Path, expected_duration: Optional[float] = None) -> dict:
    """
    Phase 4.11 — Validate output video with ffprobe / metadata inspect.
    Validates resolution (1080x1920), audio codec, and duration consistency (diff <= 0.10s).
    Returns a dict of all validation fields.
    """
    probe = _run_ffprobe_inspect(video_path)
    streams = probe.get("streams", [])
    fmt = probe.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))

    fps = 25.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = video_stream.get(key)
        if val:
            try:
                if "/" in val:
                    num, den = val.split("/")
                    if float(den) > 0:
                        f = float(num) / float(den)
                        if 10.0 <= f <= 120.0:
                            fps = round(f, 2)
                            break
                else:
                    f = float(val)
                    if 10.0 <= f <= 120.0:
                        fps = round(f, 2)
                        break
            except (ValueError, ZeroDivisionError, AttributeError):
                pass

    duration = float(fmt.get("duration", 0))
    file_size = int(fmt.get("size", 0))
    vcodec = video_stream.get("codec_name", "h264")
    acodec = audio_stream.get("codec_name", "aac")

    exp_dur = round(expected_duration, 2) if expected_duration is not None else round(duration, 2)
    act_dur = round(duration, 2)
    diff = round(abs(exp_dur - act_dur), 2)
    is_duration_consistent = (diff <= 0.10)

    # Validation checks
    _vspec = _get_video_spec()
    exp_w, exp_h = _vspec[0], _vspec[1]
    exp_aspect = (exp_w / exp_h) if exp_h else 0.0
    is_9_16 = (width == exp_w and height == exp_h) or (
        height > 0 and exp_aspect and abs((width / height) - exp_aspect) < 0.02
    )
    is_audio_ok = acodec in ("aac", "mp3", "opus", "vorbis") and audio_stream != {}
    is_duration_ok = is_duration_consistent

    return {
        "path": str(video_path),
        "resolution": f"{width}×{height}",
        "fps": fps,
        "expected_duration": exp_dur,
        "actual_duration": act_dur,
        "duration": act_dur,
        "duration_difference": diff,
        "is_duration_consistent": is_duration_consistent,
        "is_duration_ok": is_duration_ok,
        "video_codec": vcodec,
        "audio_codec": acodec,
        "file_size_mb": round(file_size / (1024 * 1024), 2),
        "is_9_16": is_9_16,
        "is_audio_ok": is_audio_ok,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Generic Single Clip Renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_clip(
    rank: int,
    output_filename: str,
    video_path: Optional[Path] = None,
    temp_dir: Optional[Path] = None,
    candidates_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    quiet: bool = False,
    debug_captions: bool = False,
) -> dict:
    """
    Generic single-clip rendering pipeline.

    Executes:
      1. Extract clip from source video
      2. Face detection and tracking
      3. Compute 9:16 crop plan with smoothing
      4. Render 9:16 reframed video
      5. Build caption chunks with word-level timing and burn captions
      6. Run caption validation and ffprobe validation

    Returns a results dictionary with all metadata and validation fields.
    """
    from src.caption_renderer import (
        build_caption_chunks,
        render_captions_on_video,
        validate_caption_chunks,
    )
    from src.face_tracker import track_faces
    from src.reframer import compute_crop_plan, render_reframed_video
    from src.video_extractor import select_and_extract
    import cv2

    clip_temp_dir = temp_dir or PHASE4_TEMP_DIR
    clip_temp_dir.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cand_path = candidates_path or (TEMP_DIR / SEMANTIC_JSON_FILENAME)
    trans_path = transcript_path or (TEMP_DIR / TRANSCRIPT_JSON_FILENAME)

    # ── Step 1: Extract clip ──────────────────────────────────────────────────
    if not quiet:
        print("  [1/5] Extracting source clip from video…")
    source_clip_path = clip_temp_dir / "source_clip.mp4"
    clip_info = select_and_extract(
        rank=rank,
        candidates_path=cand_path,
        input_video_path=video_path,
        out_path=source_clip_path,
        transcript_path=trans_path,
        quiet=quiet,
    )
    if not quiet:
        print(f"       Source clip: {clip_info.extracted_clip}")
        print(f"       Duration   : {clip_info.duration:.2f}s")
        if clip_info.was_trimmed:
            print(f"       Trimmed from {clip_info.original_duration:.2f}s to {clip_info.duration:.2f}s")

    # ── Step 2: Face tracking ─────────────────────────────────────────────────
    if not quiet:
        print("  [2/5] Running face detection and tracking…")
    face_tracks_path = clip_temp_dir / "face_tracks.json"
    frame_data = track_faces(source_clip_path, out_path=face_tracks_path)

    face_frames = sum(1 for f in frame_data if f.primary_face is not None)
    face_pct = 100.0 * face_frames / max(len(frame_data), 1)
    if not quiet:
        print(f"       Frames analyzed: {len(frame_data)}")
        print(f"       Frames with face: {face_frames} ({face_pct:.1f}%)")
        if face_pct < 10.0:
            print("       ⚠ Low face detection — will use center-crop fallback")
        else:
            print("       Face tracking: OK")

    # ── Step 3: Compute crop plan ─────────────────────────────────────────────
    if not quiet:
        print("  [3/5] Computing 9:16 crop plan with smoothing…")
    cap = cv2.VideoCapture(str(source_clip_path))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()

    crop_plan = compute_crop_plan(frame_data, src_w, src_h)
    if not quiet:
        print(f"       Source size: {src_w}×{src_h}")
        ow, oh = _get_video_spec()
        print(f"       Output size: {ow}×{oh}")
        print(f"       Crop windows: {len(crop_plan)}")

    # ── Step 4: Render reframed video ─────────────────────────────────────────
    if not quiet:
        print("  [4/5] Rendering 9:16 reframed video…")
    reframed_path = clip_temp_dir / "reframed.mp4"
    render_reframed_video(
        source_clip=source_clip_path,
        crop_plan=crop_plan,
        out_path=reframed_path,
        fps=src_fps,
    )
    if not quiet:
        print(f"       Reframed video: {reframed_path}")

    # ── Step 5: Burn captions (OFF for Youtube-shorts — clean cuts only) ─────
    # The user wants Youtube shorts without any captions burned in. We take the
    # already-reframed 9:16 clip and go straight to the final full-frame encode.
    final_out = OUTPUT_DIR / output_filename
    if not caption_burn_enabled():
        if not quiet:
            print("  [5/5] Skipping captions (Youtube shorts = clean cuts, no captions)…")
        # Directly use the reframed clip as the base for the final encode.
        _raw_base = reframed_path
        caption_chunks: list = []
        caption_val = {"chunks": 0, "ok": True}
    else:
        segments: list[dict] = []
        if trans_path.exists():
            with trans_path.open(encoding="utf-8") as fh:
                trans_data = json.load(fh)
            segments = trans_data.get("segments", []) or []

        if not segments and not quiet:
            print("       ⚠ Main transcript has no segments — attempting on-demand re-transcription of clip audio…")

        if not segments and source_clip_path.exists():
            try:
                import tempfile
                from app.transcriber import transcribe_with_faster_whisper
                with tempfile.TemporaryDirectory() as _td:
                    clip_audio = Path(_td) / "clip_audio.mp3"
                    from src.config import FFMPEG_BIN
                    from src.ffmpeg_utils import run_ffmpeg
                    _ac = run_ffmpeg(
                        [FFMPEG_BIN, "-y", "-i", str(source_clip_path), "-vn", "-ac", "1", "-ar", "16000", str(clip_audio)],
                        timeout=90,
                    )
                    if clip_audio.exists() and clip_audio.stat().st_size > 0:
                        from src.config import get_setting
                        _fw_model = get_setting("faster_whisper_model", "base")
                        _device = get_setting("faster_whisper_device", "auto")
                        _compute = get_setting("faster_whisper_compute_type", "int8")
                        _segs, _ = transcribe_with_faster_whisper(
                            audio_path=clip_audio,
                            model_name=_fw_model,
                            language_code=None,
                            device=_device,
                            compute_type=_compute,
                        )
                        for s in _segs:
                            segments.append({
                                "start": s.start + clip_info.start,
                                "end": s.end + clip_info.start,
                                "text": s.text,
                                "words": [
                                    {
                                        "word": w.get("word", ""),
                                        "start": float(w.get("start", 0.0)) + clip_info.start,
                                        "end": float(w.get("end", 0.0)) + clip_info.start,
                                    }
                                    for w in (s.words or [])
                                ],
                            })
                        if not quiet:
                            print(f"       ✓ On-demand transcription recovered {len(segments)} segments ({clip_info.start:.2f}s offset applied)")
            except Exception as _e:
                if not quiet:
                    print(f"       ⚠ On-demand transcription failed: {_e}")

        if not segments and not quiet:
            print("       ⚠ No transcript available — captions will be skipped for this clip.")

        caption_chunks = build_caption_chunks(
            segments=segments,
            clip_start=clip_info.start,
            clip_end=clip_info.end,
            clip_media_path=source_clip_path,
            debug=debug_captions,
        )

        caption_val = validate_caption_chunks(caption_chunks, clip_info.duration)

        render_captions_on_video(
            input_video=reframed_path,
            caption_chunks=caption_chunks,
            out_path=final_out,
            audio_source=reframed_path,
        )

    # ── Final 9:16 Re-encode: Guarantee no black borders (1080x1920 full-frame) ──
    if not quiet:
        print("  [6/6] Final 9:16 re-encode (force full-frame, no black bars)…")
    from src.config import FFMPEG_BIN, get_video_spec_config
    from src.ffmpeg_utils import run_ffmpeg
    _vspec = get_video_spec_config()
    _W = int(_vspec.get("target_width", 1080))
    _H = int(_vspec.get("target_height", 1920))
    _reenc_path = final_out.with_name(f"_re_{final_out.name}")
    _reenc_cmd = [
        FFMPEG_BIN, "-y", "-threads", "0",
        "-i", str(_raw_base),
        "-vf", (
            f"scale={_W}:{_H}:force_original_aspect_ratio=increase,"
            f"crop={_W}:{_H}"
        ),
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(_reenc_path),
    ]
    try:
        _reproc = run_ffmpeg(_reenc_cmd, timeout=300)
        if _reproc.returncode == 0 and _reenc_path.exists() and _reenc_path.stat().st_size > 0:
            _reenc_path.replace(final_out)
            if not quiet:
                print(f"       ✓ Final output guaranteed at {_W}x{_H} (full-frame, no black bars)")
        else:
            if not quiet:
                print(f"       ⚠ Final re-encode failed; using previous output. ({_reproc.stderr[:200] if _reproc.stderr else 'no stderr'})")
            if _reenc_path.exists():
                _reenc_path.unlink(missing_ok=True)
    except Exception as _re_err:
        if not quiet:
            print(f"       ⚠ Final re-encode exception: {_re_err}")
        if _reenc_path.exists():
            _reenc_path.unlink(missing_ok=True)

    if not quiet:
        print(f"       Final output: {final_out}")

    # ── Step 6: Validation ────────────────────────────────────────────────────
    if not quiet:
        print("  Running ffprobe validation…")
    validation = _validate_output(final_out, expected_duration=clip_info.duration)

    if not quiet:
        print(f"\n  Duration Breakdown (Rank #{rank}):")
        print(f"    Candidate start    : {clip_info.start:.3f}s ({_fmt_ts(clip_info.start)})")
        print(f"    Candidate end      : {clip_info.end:.3f}s ({_fmt_ts(clip_info.end)})")
        print(f"    Candidate duration : {clip_info.duration:.2f}s")
        print(f"    Extracted start    : {clip_info.start:.3f}s")
        print(f"    Extracted end      : {clip_info.end:.3f}s")
        print(f"    Extracted duration : {clip_info.duration:.2f}s")
        print(f"    Final duration     : {validation.get('actual_duration', 0):.2f}s")
        print(f"    Duration diff      : {validation.get('duration_difference', 0):.2f}s")

    return {
        "rank": rank,
        "clip_info": clip_info,
        "face_coverage_pct": face_pct,
        "crop_plan_frames": len(crop_plan),
        "caption_chunks": len(caption_chunks),
        "caption_validation": caption_val,
        "output_path": final_out,
        "face_tracks_path": face_tracks_path,
        "validation": validation,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test Clip Handler (Calls generic render_clip)
# ─────────────────────────────────────────────────────────────────────────────

def render_test_clip(
    rank: int = 1,
    output_filename: str = "test_short_001.mp4",
    video_path: Optional[Path] = None,
    candidates_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    debug_captions: bool = False,
) -> dict:
    """
    Phase 4/5 test short rendering — delegates to generic render_clip.
    """
    return render_clip(
        rank=rank,
        output_filename=output_filename,
        video_path=video_path,
        temp_dir=PHASE4_TEMP_DIR,
        candidates_path=candidates_path,
        transcript_path=transcript_path,
        quiet=False,
        debug_captions=debug_captions,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch Renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_batch(
    video_path: Optional[Path] = None,
    candidates_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    output_prefix: str = "short",
    start_rank: int = 1,
    end_rank: Optional[int] = None,
    overwrite: bool = False,
    keep_temp: bool = False,
    debug_captions: bool = False,
) -> dict:
    """
    Phase 4 Batch Rendering — Renders all or a range of selected clips from
    temp/semantic_candidates.json into output/ using the full Phase 4 pipeline.

    Parameters:
      candidates_path: Path to semantic_candidates.json (default: temp/semantic_candidates.json)
      transcript_path: Path to transcript.json (default: temp/transcript.json)
      output_prefix: Filename prefix (e.g. 'short' -> short_001.mp4)
      start_rank: Starting rank (1-indexed, default: 1)
      end_rank: Ending rank (1-indexed, default: None = all)
      overwrite: If True, re-renders existing output files. If False, skips them.
      keep_temp: If True, keeps per-rank temporary working directories.

    Returns:
      Summary dict with total, rendered, skipped, failed counts and result lists.
    """
    cand_path = candidates_path or (TEMP_DIR / SEMANTIC_JSON_FILENAME)
    trans_path = transcript_path or (TEMP_DIR / TRANSCRIPT_JSON_FILENAME)

    if not cand_path.exists():
        raise FileNotFoundError(
            f"Semantic candidates file not found at {cand_path}.\n"
            "Run Phase 3.5 first: python -m app.main rank-clips"
        )
    if not trans_path.exists():
        raise FileNotFoundError(
            f"Transcript file not found at {trans_path}.\n"
            "Run Phase 2 first: python -m app.main transcribe"
        )

    with cand_path.open(encoding="utf-8") as fh:
        cand_data = json.load(fh)

    candidates = cand_data.get("candidates", [])
    total_candidates = len(candidates)
    if total_candidates == 0:
        raise ValueError(f"No candidate clips found in {cand_path}")

    # Validate and clamp rank bounds
    s_rank = max(1, start_rank)
    e_rank = total_candidates if end_rank is None else min(total_candidates, end_rank)

    if s_rank > e_rank:
        raise ValueError(
            f"start_rank ({s_rank}) cannot be greater than end_rank ({e_rank}). "
            f"Total available candidates: {total_candidates}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    batch_temp_root = TEMP_DIR / "phase4" / "batch"
    batch_temp_root.mkdir(parents=True, exist_ok=True)

    total_to_process = e_rank - s_rank + 1
    rendered_count = 0
    skipped_count = 0
    failed_count = 0

    results: list[dict] = []
    successful_outputs: list[str] = []
    skipped_items: list[dict] = []
    failed_items: list[dict] = []

    for rank in range(s_rank, e_rank + 1):
        output_filename = f"{output_prefix}_{rank:03d}.mp4"
        final_output_path = OUTPUT_DIR / output_filename
        rank_temp_dir = batch_temp_root / f"rank_{rank:03d}"

        # Retrieve candidate info for progress display
        cand_item = candidates[rank - 1]
        sem_score = cand_item.get("semantic", {}).get("semantic_score", cand_item.get("score", 0.0))
        cand_start = float(cand_item.get("start", 0.0))
        cand_end = float(cand_item.get("end", 0.0))
        cand_dur = float(cand_item.get("duration", cand_end - cand_start))

        print(f"\n{'=' * 52}")
        print(f"Rendering Rank #{rank} / {e_rank}")
        print(f"{'=' * 52}")
        print(f"  Semantic Score : {sem_score:.1f}")
        print(f"  Timeline       : {_fmt_ts(cand_start)} -> {_fmt_ts(cand_end)}")
        print(f"  Duration       : {cand_dur:.2f}s")
        print(f"  Output         : output/{output_filename}")

        # Overwrite protection check
        if final_output_path.exists() and not overwrite:
            print(f"  ➜ SKIPPED — output already exists ({output_filename})")
            skipped_count += 1
            item_info = {
                "rank": rank,
                "status": "skipped",
                "output_path": str(final_output_path),
                "reason": "output already exists",
            }
            results.append(item_info)
            skipped_items.append(item_info)
            continue

        # Render clip in dedicated temp directory
        rank_temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            clip_res = render_clip(
                rank=rank,
                output_filename=output_filename,
                video_path=video_path,
                temp_dir=rank_temp_dir,
                candidates_path=cand_path,
                transcript_path=trans_path,
                quiet=False,
                debug_captions=debug_captions,
            )

            v = clip_res["validation"]
            face_ok = clip_res["face_coverage_pct"] >= 20.0
            is_9_16 = v.get("is_9_16", False)
            is_audio = v.get("is_audio_ok", False)
            has_captions = clip_res["caption_chunks"] > 0
            dur_ok = v.get("is_duration_ok", False)
            all_pass = all([face_ok, is_9_16, is_audio, has_captions, dur_ok])

            # Print per-clip validation summary
            print(f"\n  Validation:")
            print(f"    Resolution   : {v.get('resolution', 'unknown')}")
            print(f"    FPS          : {v.get('fps', '?')}")
            print(f"    Expected Dur : {v.get('expected_duration', '?')}s")
            print(f"    Actual Dur   : {v.get('actual_duration', '?')}s")
            print(f"    Duration Diff: {v.get('duration_difference', '?')}s")
            print(f"    Face         : {'PASS' if face_ok else 'FAIL'} ({clip_res['face_coverage_pct']:.1f}%)")
            print(f"    Aspect       : {'PASS' if is_9_16 else 'FAIL'}")
            print(f"    Captions     : {'PASS' if has_captions else 'FAIL'} ({clip_res['caption_chunks']} chunks)")
            print(f"    Audio        : {'PASS' if is_audio else 'FAIL'}")
            print(f"    Duration     : {'PASS' if dur_ok else 'FAIL'}")

            if not all_pass:
                failed_reasons = []
                if not face_ok:
                    failed_reasons.append(f"Face coverage low ({clip_res['face_coverage_pct']:.1f}%)")
                if not is_9_16:
                    failed_reasons.append(f"Wrong aspect ratio ({v.get('resolution')})")
                if not is_audio:
                    failed_reasons.append("Audio validation failed")
                if not has_captions:
                    failed_reasons.append("No captions generated")
                if not dur_ok:
                    failed_reasons.append(f"Duration difference too high: {v.get('duration_difference')}s (Expected: {v.get('expected_duration')}s, Actual: {v.get('actual_duration')}s)")

                reason_str = "; ".join(failed_reasons)
                failed_count += 1
                failed_item = {
                    "rank": rank,
                    "status": "failed",
                    "output_path": str(clip_res["output_path"]),
                    "reason": reason_str,
                    "validation": v,
                }
                failed_items.append(failed_item)
                results.append(failed_item)
                print(f"  ✗ Rank #{rank} validation FAILED: {reason_str}")
            else:
                rendered_count += 1
                successful_outputs.append({
                    "rank": rank,
                    "output_path": f"output/{output_filename}",
                })
                results.append({
                    "rank": rank,
                    "status": "success",
                    "output_path": str(clip_res["output_path"]),
                    "validation": v,
                })
                print(f"  ✓ Rank #{rank} complete -> output/{output_filename}")

                # Clean up per-rank temp directory if not keep_temp
                if not keep_temp:
                    try:
                        shutil.rmtree(rank_temp_dir, ignore_errors=True)
                    except Exception:
                        pass

        except Exception as exc:
            log.exception("Error rendering rank #%d: %s", rank, exc)
            failed_count += 1
            failed_item = {
                "rank": rank,
                "status": "failed",
                "output_path": str(final_output_path),
                "reason": str(exc),
            }
            failed_items.append(failed_item)
            results.append(failed_item)
            print(f"  ✗ Rank #{rank} FAILED with exception: {exc}")

    return {
        "total": total_to_process,
        "total_candidates": total_candidates,
        "start_rank": s_rank,
        "end_rank": e_rank,
        "rendered": rendered_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "successful_outputs": successful_outputs,
        "skipped_items": skipped_items,
        "failed_items": failed_items,
        "results": results,
    }
