"""
src/job_runner.py
-----------------
Runs pipeline for a Job (DB-backed) — called by job_queue worker.
Updates Job.progress_percent + status, saves GeneratedClip rows, uses per-user storage.
Reuses same phases as server.py _run_full_pipeline_task but per-job isolation.
"""

from __future__ import annotations

import gc
import json
import shutil
import time
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from src.config import INPUT_DIR, OUTPUT_DIR, TEMP_DIR
from src.logger import get_logger
from src.models import GeneratedClip, Job

log = get_logger("job_runner")


def _update_progress(job: Job, db: Session, percent: int, phase: Optional[str] = None):
    job.progress_percent = max(0, min(100, percent))
    if phase:
        # Store phase in error_message or just log; Job doesn't have current_phase col, use error_message prefix? Keep separate via progress
        pass
    db.commit()


def run_job_pipeline(job: Job, db: Session):
    """Run full pipeline for job — similar to server.py _run_full_pipeline_task but DB-aware."""
    job_id = job.id
    user_id = job.user_id
    url = (job.youtube_url or "").strip()
    filename = (job.filename or "").strip()
    # Determine num_shorts from clip settings? Use default "all"
    num_shorts: Optional[int] = None

    # Use job-specific temp dir to avoid collision when multiple workers (future)
    job_temp = TEMP_DIR / f"job_{job_id[:8]}"
    job_temp.mkdir(parents=True, exist_ok=True)

    # We will reuse global TEMP_DIR for transcript etc for simplicity, but job_id isolates logs
    # For true per-job isolation, we'd use job_temp as TEMP_DIR override via env, but keep global for now
    try:
        # Phase 1: Download
        _update_progress(job, db, 5)
        video_path: Optional[Path] = None
        if url:
            log.info("[Job %s] Download %s", job_id[:8], url[:60])
            from src.downloader import download_video

            video_path = download_video(url)
            _update_progress(job, db, 25)
        elif filename:
            safe = Path(filename).name
            video_path = INPUT_DIR / safe
            if not video_path.exists():
                raise FileNotFoundError(f"Input file not found: {safe}")
            _update_progress(job, db, 25)
        else:
            from app.transcriber import load_latest_video

            video_path = load_latest_video()
            _update_progress(job, db, 25)

        # Phase 2: Transcribe
        _update_progress(job, db, 30)
        from app.transcriber import transcribe_video
        from src.config import get_setting
        _tp = get_setting("transcription_provider", "groq")
        if _tp == "faster_whisper":
            _gm = get_setting("faster_whisper_model", "base")
        else:
            _gm = get_setting("groq_whisper_model", "whisper-large-v3")

        tr_result = transcribe_video(video_path=video_path, provider=_tp, model_name=_gm, language=None, keep_audio=False)
        _update_progress(job, db, 45)

        # Phase 3: Select
        _update_progress(job, db, 50)
        from app.clip_selector import run_selection

        # Use job_temp for transcript path? Global TEMP_DIR already has transcript.json from transcribe_video
        report = run_selection(
            transcript_path=TEMP_DIR / "transcript.json",
            min_dur=15.0,
            max_dur=30.0,
            top_n=100,
            min_score=20.0,
            min_separation=20.0,
        )
        _update_progress(job, db, 65)

        # Phase 3.5: Rank
        _update_progress(job, db, 70)
        candidates_json = TEMP_DIR / "candidates.json"
        try:
            from app.semantic_ranker import run_semantic_ranking

            rank_target = report["final_count"]
            rank_result = run_semantic_ranking(
                candidates_path=TEMP_DIR / "candidate_pool.json",
                transcript_path=TEMP_DIR / "transcript.json",
                top_n=rank_target,
                semantic_pool_size=max(rank_target, 50),
                min_score=20.0,
                min_separation=20.0,
            )
            candidates_json = Path(rank_result["json_path"])
        except Exception as e:
            log.warning("Rank fallback %s", e)
        _update_progress(job, db, 75)

        # Phase 4/5: Render
        with open(candidates_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            clips_list = data.get("candidates", data.get("final_selected", []))
        elif isinstance(data, list):
            clips_list = data
        else:
            clips_list = []
        clips_to_render = clips_list
        num_to_render = len(clips_to_render)
        if num_to_render == 0:
            raise RuntimeError("No clips to render")

        log.info("[Job %s] Rendering %s shorts", job_id[:8], num_to_render)
        from src.renderer import render_clip
        from src.storage import save_clip

        rendered = []
        for idx in range(1, num_to_render + 1):
            out_name = f"short_{job_id[:8]}_{idx:03d}.mp4"
            try:
                result = render_clip(
                    rank=idx,
                    output_filename=out_name,
                    video_path=video_path,
                    candidates_path=candidates_json,
                    transcript_path=TEMP_DIR / "transcript.json",
                )
                # Save to per-user storage + keep in output for backward compat
                src_path = OUTPUT_DIR / out_name
                if src_path.exists():
                    save_clip(user_id, job_id, out_name, src_path=src_path)
                    # Also record in DB
                    clip = GeneratedClip(
                        job_id=job_id,
                        user_id=user_id,
                        file_path=str(src_path),
                        duration_seconds=float(result.get("validation", {}).get("actual_duration", 0)),
                        hook_score=float(clips_to_render[idx - 1].get("score", 0)),
                    )
                    db.add(clip)
                    db.commit()
                rendered.append(out_name)
            except Exception as e:
                log.warning("Render %s failed %s", idx, e)
            finally:
                gc.collect()
            _update_progress(job, db, 75 + int((idx / num_to_render) * 24))

        _update_progress(job, db, 100)
        log.info("Job %s finished %s clips", job_id[:8], len(rendered))
        # Cleanup job temp
        try:
            shutil.rmtree(job_temp, ignore_errors=True)
        except Exception:
            pass

    except Exception as e:
        # Refund quota if failed early? Caller will handle
        log.error("Job %s pipeline error %s", job_id[:8], e)
        raise
