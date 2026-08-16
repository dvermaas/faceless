"""Orchestration for `faceless harvest` and `faceless remix`."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp.utils import DownloadError

from . import pexels
from .download import GrabError, grab
from .library import Clip, Library, LibraryError, clips_from, import_clip, segments_for
from .llm import warm
from .match import Match, choose
from .pexels import PexelsError
from .render import render
from .search import search
from .segments import DEFAULT_MIN_DURATION, Segment
from .ytdl import ClientOptions

HARVEST_PAUSE = 3.0
SOURCES = ("youtube", "pexels")
# Segments run 2-4s and the renderer uses only the head of a clip, so keeping
# more than this of a 30s stock video is storage for nothing.
PEXELS_MAX_CLIP_SECONDS = 10.0


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


@dataclass(slots=True)
class ResetTarget:
    name: str
    path: Path
    files: int
    bytes: int

    @property
    def megabytes(self) -> float:
        return self.bytes / 1_000_000


def inspect_reset(paths: dict[str, Path]) -> list[ResetTarget]:
    """Measure what a reset would delete, without deleting anything."""
    targets: list[ResetTarget] = []
    for name, path in paths.items():
        if not path.exists():
            continue
        files = [item for item in path.rglob("*") if item.is_file()]
        targets.append(
            ResetTarget(
                name=name,
                path=path,
                files=len(files),
                bytes=sum(item.stat().st_size for item in files),
            )
        )
    return targets


def reset(targets: list[ResetTarget]) -> list[str]:
    """Delete the output directories outright, returning anything that survived.

    Failures are reported rather than ignored: a destructive command that claims
    to have removed hundreds of megabytes while the database is still sitting
    there is worse than one that fails loudly. The usual cause on Windows is
    something holding `library.db` open - a database viewer attached to it, for
    instance - and the fix is to close that, not to work around it here.
    """
    for target in targets:
        resolved = target.path.resolve()
        if resolved.parent == resolved or resolved == Path.cwd().resolve():
            raise LibraryError(f"refusing to delete {resolved} - that is not an output directory")

    failed: list[str] = []
    for target in targets:
        try:
            shutil.rmtree(target.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed.append(f"{target.path}: {exc.strerror or exc}")
    return failed


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
    source: str = "youtube",
    limit: int = 5,
    library_root: Path | str = "library",
    downloads: Path | str = "downloads",
    min_duration: float = DEFAULT_MIN_DURATION,
    model: str | None = None,
    pexels_key: str | None = None,
    client: ClientOptions | None = None,
    on_progress=None,
) -> HarvestResult:
    """Grow the clip library from `query`, using the chosen source."""
    if source not in SOURCES:
        raise LibraryError(f"unknown source {source!r}; expected one of {', '.join(SOURCES)}")
    library = Library(library_root)
    result = HarvestResult()
    if source == "pexels":
        _harvest_pexels(
            query,
            library,
            result,
            limit=limit,
            downloads=Path(downloads),
            key=pexels_key,
            on_progress=on_progress,
        )
        library.save()
        return result
    return _harvest_youtube(
        query,
        library,
        result,
        limit=limit,
        downloads=downloads,
        min_duration=min_duration,
        model=model,
        client=client,
        on_progress=on_progress,
    )


def _harvest_pexels(
    query: str,
    library: Library,
    result: HarvestResult,
    *,
    limit: int,
    downloads: Path,
    key: str | None,
    on_progress=None,
) -> None:
    """File clean stock footage - no scene splitting, no model calls.

    Pexels writes a descriptive slug into every video URL, so the description
    that the YouTube path needs a model to infer is simply read off the URL.
    """
    videos = pexels.search(query, limit=limit, key=key)
    if on_progress:
        on_progress(f"pexels: {len(videos)} portrait clips for {query!r}")
    staging = downloads / "pexels"
    for video in videos:
        source_id = f"pexels-{video.id}"
        if library.has_source(source_id):
            result.skipped.append(source_id)
            continue
        raw = staging / f"{source_id}.mp4"
        try:
            if on_progress:
                on_progress(f"  fetch {video.description[:52]}")
            pexels.download(video, raw)
            clip = import_clip(
                raw,
                library,
                clip_id_hint=query,
                source_id=source_id,
                source_url=video.url,
                source_title=video.description,
                author=video.author,
                description=video.description,
                keywords=video.keywords,
                provider="pexels",
                duration=video.duration,
                max_seconds=PEXELS_MAX_CLIP_SECONDS,
            )
            result.added.append(clip)
            library.save()
        except (PexelsError, LibraryError, OSError) as exc:
            result.failed.append((source_id, str(exc)[:200]))
            if on_progress:
                on_progress(f"  failed: {str(exc)[:120]}")
        finally:
            raw.unlink(missing_ok=True)


def _harvest_youtube(
    query: str,
    library: Library,
    result: HarvestResult,
    *,
    limit: int,
    downloads: Path | str,
    min_duration: float,
    model: str | None,
    client: ClientOptions | None,
    on_progress=None,
) -> HarvestResult:
    """Find Shorts, download them, and file every scene as a reusable clip."""
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

    @property
    def credits(self) -> list[dict]:
        """Attribution for footage whose licence asks for it (Pexels does)."""
        seen: dict[str, dict] = {}
        for match in self.matches:
            if match.clip and match.clip.provider == "pexels":
                seen.setdefault(
                    match.clip.source_id,
                    {
                        "author": match.clip.channel,
                        "url": match.clip.source_url,
                        "provider": "pexels",
                    },
                )
        return list(seen.values())

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "segment_count": len(self.segments),
            "matched": sum(1 for match in self.matches if match.clip),
            "output": str(self.output) if self.output else None,
            "credits": self.credits,
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
    source: str = "any",
    client: ClientOptions | None = None,
    on_progress=None,
) -> RemixResult:
    """Rebuild a Short: its audio, someone else's pictures."""
    library = Library(library_root)
    if not library.clips:
        raise LibraryError(
            f"the library at {library.root} is empty - run `faceless harvest` first"
        )

    if source != "any" and not any(clip.provider == source for clip in library.clips):
        raise LibraryError(
            f"no {source} clips in {library.root} - "
            f"harvest some with `faceless harvest <query> --source {source}`"
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
        on_progress(f"matching {len(segments)} segments against {library.count()} clips")
    # Never recut the video we are rebuilding.
    matches = choose(
        segments,
        library,
        exclude_sources={meta.get("id") or ""},
        model=model,
        provider=None if source == "any" else source,
    )

    result = RemixResult(
        target_id=meta.get("id") or "",
        title=meta.get("title") or "",
        segments=segments,
        matches=matches,
    )
    if dry_run:
        return result

    # Logged only for real renders. A dry run is exploration - counting it would
    # inflate "most reused" every time someone iterates on match quality.
    for match in matches:
        if match.clip:
            library.record_usage(
                match.clip,
                result.target_id,
                match.segment.index,
                match.query,
                match.score,
                match.reason,
            )

    # Keep source-filtered runs in their own file so an A/B against the
    # unfiltered remix does not silently clobber it.
    suffix = "" if source == "any" else f".{source}"
    output = Path(out_dir) / f"{result.target_id}.remix{suffix}.mp4"
    if on_progress:
        on_progress("rendering")
    result.output = render(
        matches, grabbed.video_path, output, library_root=Path(library_root)
    )
    return result
