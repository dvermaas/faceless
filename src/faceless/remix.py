"""Orchestration for `faceless harvest` and `faceless remix`."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp.utils import DownloadError

from .download import GrabError, grab
from .library import Clip, Library, LibraryError, clips_from, segments_for
from .llm import warm
from .match import Match, choose
from .render import render
from .search import search
from .segments import DEFAULT_MIN_DURATION, Segment
from .ytdl import ClientOptions

HARVEST_PAUSE = 3.0


@dataclass(slots=True)
class HarvestResult:
    added: list[Clip] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "added_clips": len(self.added),
            "sources_added": len({clip.source_id for clip in self.added}),
            "skipped": self.skipped,
            "failed": [{"id": vid, "error": err} for vid, err in self.failed],
            "clips": [clip.to_dict() for clip in self.added],
        }


def _meta_for(video: Path) -> dict:
    meta_path = video.parent / f"{video.stem}.meta.json"
    if not meta_path.exists():
        raise LibraryError(f"no {meta_path.name} beside {video}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _subtitle_for(video: Path) -> Path | None:
    matches = sorted(video.parent.glob(f"{video.stem}.*.vtt"))
    return matches[0] if matches else None


def harvest(
    query: str,
    *,
    limit: int = 5,
    library_root: Path | str = "library",
    downloads: Path | str = "downloads",
    min_duration: float = DEFAULT_MIN_DURATION,
    model: str | None = None,
    client: ClientOptions | None = None,
    on_progress=None,
) -> HarvestResult:
    """Find Shorts, download them, and file every scene as a reusable clip."""
    library = Library(library_root)
    result = HarvestResult()
    warm(model)

    found = search(query, limit=limit, shorts=True, client=client)
    for position, item in enumerate(found):
        video_id = item["id"]
        if library.has_source(video_id):
            result.skipped.append(video_id)
            if on_progress:
                on_progress(f"skip {video_id} (already harvested)")
            continue
        try:
            if on_progress:
                on_progress(f"grab {video_id} - {(item['title'] or '')[:50]}")
            grabbed = grab(
                item["url"],
                out_dir=downloads,
                subs=True,
                scenes=True,
                client=client,
            )
            if not grabbed.video_path:
                raise LibraryError("no video file was saved")
            meta = _meta_for(grabbed.video_path)
            segments = segments_for(meta, _subtitle_for(grabbed.video_path), min_duration)
            made = clips_from(grabbed.video_path, meta, segments, library, model=model)
            result.added.extend(made)
            library.save()
            if on_progress:
                on_progress(f"  +{len(made)} clips")
        except (GrabError, LibraryError, DownloadError, OSError) as exc:
            # A throttled or unavailable video is one lost source, not a lost
            # harvest - YouTube 403s partway through a batch are routine.
            result.failed.append((video_id, str(exc)[:200]))
            if on_progress:
                on_progress(f"  failed: {str(exc)[:120]}")
        # YouTube throttles rapid sequential downloads; pace between videos.
        if position + 1 < len(found):
            time.sleep(HARVEST_PAUSE)

    library.save()
    return result


@dataclass(slots=True)
class RemixResult:
    target_id: str
    title: str
    segments: list[Segment]
    matches: list[Match]
    output: Path | None = None

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "segment_count": len(self.segments),
            "matched": sum(1 for match in self.matches if match.clip),
            "output": str(self.output) if self.output else None,
            "matches": [match.to_dict() for match in self.matches],
        }


def remix(
    url: str,
    *,
    library_root: Path | str = "library",
    downloads: Path | str = "downloads",
    out_dir: Path | str = "remixes",
    min_duration: float = DEFAULT_MIN_DURATION,
    model: str | None = None,
    dry_run: bool = False,
    client: ClientOptions | None = None,
    on_progress=None,
) -> RemixResult:
    """Rebuild a Short: its audio, someone else's pictures."""
    library = Library(library_root)
    if not library.clips:
        raise LibraryError(
            f"the library at {library.root} is empty - run `faceless harvest` first"
        )
    warm(model)

    if on_progress:
        on_progress("fetching target")
    grabbed = grab(url, out_dir=downloads, subs=True, scenes=True, client=client)
    if not grabbed.video_path:
        raise LibraryError("no video file was saved for the target")

    meta = _meta_for(grabbed.video_path)
    segments = segments_for(meta, _subtitle_for(grabbed.video_path), min_duration)
    if not segments:
        raise LibraryError("target has no scenes to rebuild")

    if on_progress:
        on_progress(f"matching {len(segments)} segments against {len(library.clips)} clips")
    # Never recut the video we are rebuilding.
    matches = choose(
        segments,
        library.clips,
        exclude_sources={meta.get("id") or ""},
        model=model,
    )

    result = RemixResult(
        target_id=meta.get("id") or "",
        title=meta.get("title") or "",
        segments=segments,
        matches=matches,
    )
    if dry_run:
        return result

    output = Path(out_dir) / f"{result.target_id}.remix.mp4"
    if on_progress:
        on_progress("rendering")
    result.output = render(
        matches, grabbed.video_path, output, library_root=Path(library_root)
    )
    return result
