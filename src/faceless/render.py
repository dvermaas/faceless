"""Assemble matched clips into a finished video, over the original audio."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .library import TARGET_FPS, TARGET_HEIGHT, TARGET_WIDTH
from .match import Match


class RenderError(RuntimeError):
    """Raised when ffmpeg cannot produce the output."""


def _run(command: list[str], what: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(f"ffmpeg failed {what}: {result.stderr[-400:]}")


def _fit(clip: Path, duration: float, target: Path) -> None:
    """Render exactly `duration` seconds of `clip`, looping if it is too short."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
        capture_output=True,
        text=True,
    )
    try:
        available = float(probe.stdout.strip())
    except ValueError as exc:
        raise RenderError(f"could not read duration of {clip.name}") from exc

    # -stream_loop repeats the input; 0 means play it once.
    loops = 0 if available >= duration else int(duration // max(available, 0.04)) + 1
    _run(
        [
            "ffmpeg", "-v", "error", "-nostdin",
            "-stream_loop", str(loops), "-i", str(clip),
            "-t", f"{duration:.3f}",
            "-vf", f"fps={TARGET_FPS},scale={TARGET_WIDTH}:{TARGET_HEIGHT},setsar=1",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-y", str(target),
        ],  # fmt: skip
        f"fitting {clip.name} to {duration:.2f}s",
    )


def render(
    matches: list[Match],
    audio_source: Path,
    output: Path,
    *,
    library_root: Path,
) -> Path:
    """Concatenate the matched clips and lay the original audio over them.

    Segments tile the source video exactly, so the concatenated picture is the
    same length as the audio - no padding or stretching is needed to keep them
    together.
    """
    usable = [match for match in matches if match.clip]
    if not usable:
        raise RenderError("no segment matched a clip, so there is nothing to render")

    output.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="faceless-render-"))
    try:
        pieces: list[Path] = []
        for position, match in enumerate(usable):
            source = library_root / match.clip.path
            if not source.exists():
                raise RenderError(f"library clip missing from disk: {source}")
            piece = workdir / f"{position:03d}.mp4"
            _fit(source, match.segment.duration, piece)
            pieces.append(piece)

        listing = workdir / "concat.txt"
        listing.write_text(
            "".join(f"file '{piece.as_posix()}'\n" for piece in pieces), encoding="utf-8"
        )
        silent = workdir / "silent.mp4"
        # Every piece was written with identical parameters, so the concat
        # demuxer can copy rather than re-encode.
        _run(
            [
                "ffmpeg", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0",
                "-i", str(listing), "-c", "copy", "-y", str(silent),
            ],  # fmt: skip
            "concatenating segments",
        )

        _run(
            [
                "ffmpeg", "-v", "error", "-nostdin",
                "-i", str(silent), "-i", str(audio_source),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                "-movflags", "+faststart", "-y", str(output),
            ],  # fmt: skip
            "muxing the original audio",
        )
        return output
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
