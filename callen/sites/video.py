# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Video processing for managed sites.

Takes a video file (from an email attachment), converts it to a
lightweight web-friendly format (MP4/H.264 or WebM), and pushes
it to the site's GitHub repo.

GitHub has a 100MB file limit and repos should stay small, so we
aggressively compress: 720p max, CRF 28, fast preset, strip audio
option. A typical 30s phone video goes from ~50MB to ~2-5MB.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# GitHub's hard file size limit
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB (conservative; limit is 100MB)


def process_video(
    input_path: str,
    max_height: int = 720,
    crf: int = 28,
    strip_audio: bool = False,
    max_duration: int | None = 120,
) -> tuple[Path, dict]:
    """Transcode a video to lightweight H.264 MP4.

    Returns (output_path, info_dict). The output is a tempfile that the
    caller is responsible for cleaning up.
    """
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"video not found: {input_path}")

    # Probe the input
    info = _probe(src)

    # Build ffmpeg command
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    out.close()

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        # Video: H.264, scale down to max_height if larger, keep aspect ratio
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(crf),
        "-movflags", "+faststart",  # web streaming friendly
        "-pix_fmt", "yuv420p",      # maximum compatibility
    ]

    if max_duration:
        cmd.extend(["-t", str(max_duration)])

    if strip_audio:
        cmd.extend(["-an"])
    else:
        # Re-encode audio to AAC at low bitrate
        cmd.extend(["-c:a", "aac", "-b:a", "96k", "-ac", "2"])

    cmd.append(out.name)

    log.info("Transcoding %s -> %s (max_height=%d, crf=%d)",
             src.name, out.name, max_height, crf)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            log.error("ffmpeg failed: %s", result.stderr[:500])
            os.unlink(out.name)
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        os.unlink(out.name)
        raise RuntimeError("ffmpeg timed out (5 min limit)")

    out_size = os.path.getsize(out.name)
    out_info = _probe(Path(out.name))

    return Path(out.name), {
        "input": src.name,
        "input_size": info.get("size", 0),
        "input_duration": info.get("duration", 0),
        "output_size": out_size,
        "output_duration": out_info.get("duration", 0),
        "output_width": out_info.get("width", 0),
        "output_height": out_info.get("height", 0),
        "format": "mp4",
        "compression_ratio": round(info.get("size", 1) / max(out_size, 1), 1),
    }


def process_and_upload_video(
    video_path: str,
    site_subdomain: str,
    manager,
    dest_path: str | None = None,
    max_height: int = 720,
    crf: int = 28,
    strip_audio: bool = False,
    max_duration: int | None = 120,
    commit_message: str = "",
) -> dict:
    """Process a video and push it to a site's repo."""
    out_path, info = process_video(
        video_path,
        max_height=max_height,
        crf=crf,
        strip_audio=strip_audio,
        max_duration=max_duration,
    )

    try:
        out_size = os.path.getsize(out_path)
        if out_size > MAX_FILE_SIZE:
            raise RuntimeError(
                f"Output video is {out_size / 1024 / 1024:.1f}MB, "
                f"exceeds {MAX_FILE_SIZE / 1024 / 1024:.0f}MB limit for GitHub. "
                f"Try higher CRF, lower resolution, or shorter max_duration."
            )

        if not dest_path:
            stem = Path(video_path).stem.lower().replace(" ", "-")
            dest_path = f"videos/{stem}.mp4"

        repo_full = f"{manager.github_org}/{site_subdomain}"
        message = commit_message or f"Upload video {dest_path}"

        with open(out_path, "rb") as f:
            video_bytes = f.read()

        manager._upsert_file_binary(repo_full, dest_path, video_bytes, message)

        log.info("Uploaded %s to %s/%s (%d bytes, %.1fx compression)",
                 Path(video_path).name, site_subdomain, dest_path,
                 out_size, info["compression_ratio"])

        return {
            "site": site_subdomain,
            "file": dest_path,
            "status": "pushed",
            **info,
        }
    finally:
        os.unlink(out_path)


def _probe(path: Path) -> dict:
    """Get basic info about a video file via ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"size": path.stat().st_size}

        import json
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        info = {
            "size": path.stat().st_size,
            "duration": float(fmt.get("duration", 0)),
        }
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
                break
        return info
    except Exception:
        return {"size": path.stat().st_size}
