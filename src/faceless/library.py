"""The clip library: every scene we ever download, cut out and kept.

Each harvested Short is split along its own detected cuts, and every resulting
clip is filed with the narration that was spoken over it plus a one-line
description of what is on screen. The library is the pipeline's memory - each
grab makes the next rebuild more likely to find footage it needs.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import db
from .llm import LLMError, generate_json
from .segments import Segment
from .subtitles import parse_timed_cues

INDEX_NAME = "index.json"
CLIP_DIR = "clips"
TARGET_WIDTH, TARGET_HEIGHT, TARGET_FPS = 1080, 1920, 30
_SLUG_MAX = 48
_STOPWORDS = frozenset(
    "a an the of and or to in on at for with from is are was were be been being "
    "this that these those it its as by".split()
)

_DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "visual_description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["visual_description", "keywords"],
}


class LibraryError(RuntimeError):
    """Raised when the library cannot be read, written, or added to."""


@dataclass(slots=True)
class Clip:
    clip_id: str
    path: str
    source_id: str
    source_url: str
    source_title: str
    channel: str
    scene_index: int
    start: float
    end: float
    duration: float
    text: str
    visual_description: str = ""
    keywords: list[str] = field(default_factory=list)
    # Defaulted so index files written before Pexels support still load.
    provider: str = "youtube"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def has_burned_in_text(self) -> bool:
        """Clips cut from finished Shorts usually carry the source's captions."""
        return self.provider == "youtube"


def slugify(text: str, *, limit: int = _SLUG_MAX) -> str:
    """Turn a description into a readable, filesystem-safe file stem.

    Names carry the point of the library: the folder should be skimmable, and
    `polar-bear-in-snow__abc123_07.mp4` says what it is where `abc123_07.mp4`
    says nothing.
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    # Drop the orphan letters possessives leave behind ("bear's" -> "bear", "s").
    words = [word for word in re.split(r"[^a-z0-9]+", ascii_only) if len(word) > 1 or word.isdigit()]
    kept = [word for word in words if word not in _STOPWORDS] or words
    slug = ""
    for word in kept:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > limit:
            break
        slug = candidate
    return slug or "clip"


def describe(text: str, source_title: str, *, model: str | None = None) -> dict:
    """Ask the local model what is on screen during this clip.

    The narration is a proxy for the picture, not the picture itself - a line
    about camels plays over footage of camels - so the model is asked to
    translate what is *said* into what is likely *shown*.
    """
    prompt = (
        "You are cataloguing stock footage cut from short videos.\n"
        f"The video is titled: {source_title!r}\n"
        f"During this clip the narrator says: {text!r}\n\n"
        "Describe what is most likely VISIBLE on screen during this clip - the subject and "
        "setting, not the narration. Use 3-8 plain words, no punctuation, e.g. "
        "'camel drinking water in desert'. Also give 3-6 single-word search keywords.\n"
        "Respond as JSON."
    )
    reply = generate_json(prompt, _DESCRIPTION_SCHEMA, model=model)
    description = str(reply.get("visual_description") or "").strip()
    # Small models repeat themselves in list fields; keep first-seen order.
    seen: dict[str, None] = {}
    for word in reply.get("keywords") or []:
        cleaned = str(word).strip().lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return {"visual_description": description, "keywords": list(seen)[:8]}


_CLIP_SELECT = """
SELECT c.clip_key, c.path, c.scene_index, c.start, c.end, c.duration,
       c.narration, c.description, c.keywords,
       s.provider, s.external_id, s.url, s.title, s.author
FROM clips c JOIN sources s ON s.id = c.source_id
"""


def _to_clip(row) -> Clip:
    return Clip(
        clip_id=row["clip_key"],
        path=row["path"],
        source_id=row["external_id"],
        source_url=row["url"] or "",
        source_title=row["title"] or "",
        channel=row["author"] or "",
        scene_index=row["scene_index"],
        start=row["start"],
        end=row["end"],
        duration=row["duration"],
        text=row["narration"] or "",
        visual_description=row["description"] or "",
        keywords=json.loads(row["keywords"] or "[]"),
        provider=row["provider"],
    )


class Library:
    """Clip collection backed by SQLite (see `db.py` for the schema)."""

    def __init__(self, root: Path | str = "library") -> None:
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME
        self.connection = db.connect(self.root)
        self._migrate_json()

    def _migrate_json(self) -> None:
        """Import a pre-SQLite `index.json` once, then leave it in place.

        The file is kept rather than deleted: it is the only copy of the old
        format, and re-importing is a no-op thanks to the uniqueness constraints.
        """
        if not self.index_path.exists() or self.count():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LibraryError(f"{self.index_path} is not valid JSON: {exc}") from exc
        for record in raw.get("clips", []):
            self.add(Clip(**record))

    # -- writing ---------------------------------------------------------

    def add(self, clip: Clip) -> None:
        with self.connection:
            source_row = db.upsert_source(
                self.connection,
                provider=clip.provider,
                external_id=clip.source_id,
                url=clip.source_url,
                title=clip.source_title,
                author=clip.channel,
            )
            db.insert_clip(
                self.connection,
                source_row,
                {
                    "clip_key": clip.clip_id,
                    "path": clip.path,
                    "scene_index": clip.scene_index,
                    "start": clip.start,
                    "end": clip.end,
                    "duration": clip.duration,
                    "width": TARGET_WIDTH,
                    "height": TARGET_HEIGHT,
                    "fps": TARGET_FPS,
                    "narration": clip.text,
                    "description": clip.visual_description,
                    "keywords": clip.keywords,
                },
            )

    def save(self) -> None:
        """Retained for callers written against the JSON store; writes commit."""
        self.connection.commit()

    def record_usage(self, clip: Clip, target_id: str, segment_index: int, query: str,
                     score: float, reason: str) -> None:
        with self.connection:
            db.record_usage(
                self.connection,
                clip_key=clip.clip_id,
                target_id=target_id,
                segment_index=segment_index,
                query=query,
                score=score,
                reason=reason,
            )

    # -- reading ---------------------------------------------------------

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) AS n FROM clips").fetchone()["n"])

    @property
    def clips(self) -> list[Clip]:
        return [_to_clip(row) for row in self.connection.execute(_CLIP_SELECT)]

    @property
    def source_ids(self) -> set[str]:
        return {
            row["external_id"]
            for row in self.connection.execute("SELECT external_id FROM sources")
        }

    def has_source(self, source_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sources WHERE external_id = ? LIMIT 1", (source_id,)
        ).fetchone()
        return row is not None

    def search(
        self,
        terms: list[str],
        *,
        limit: int = 8,
        exclude_sources: set[str] = frozenset(),
        provider: str | None = None,
    ) -> list[tuple[Clip, float]]:
        """Rank clips against `terms` with BM25, best first.

        FTS5 does the shortlisting in the database, so growing the library does
        not slow matching down the way scoring every clip in Python did.
        Returned scores are flipped to positive-is-better for the caller.
        """
        match = db.fts_query(terms)
        if not match:
            return []
        sql = f"""
            SELECT c.clip_key, c.path, c.scene_index, c.start, c.end, c.duration,
                   c.narration, c.description, c.keywords,
                   s.provider, s.external_id, s.url, s.title, s.author,
                   bm25(clips_fts, 3.0, 2.0, 1.0) AS rank
            FROM clips_fts
            JOIN clips c ON c.id = clips_fts.rowid
            JOIN sources s ON s.id = c.source_id
            WHERE clips_fts MATCH ?
        """
        params: list = [match]
        if provider:
            sql += " AND s.provider = ?"
            params.append(provider)
        if exclude_sources:
            sql += f" AND s.external_id NOT IN ({','.join('?' * len(exclude_sources))})"
            params.extend(exclude_sources)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, params).fetchall()
        return [(_to_clip(row), -float(row["rank"])) for row in rows]

    def fallback_clips(
        self,
        *,
        limit: int = 8,
        exclude_sources: set[str] = frozenset(),
        provider: str | None = None,
    ) -> list[Clip]:
        """Least-used clips, for segments whose query matches nothing.

        Every segment must get footage: the shot list tiles the source exactly,
        so a hole shortens the video and `-shortest` then truncates the audio.
        Better to show a loosely-related clip than to lose eight seconds of
        narration. Preferring the least-used ones spreads the filler around.
        """
        sql = f"""
            {_CLIP_SELECT}
            LEFT JOIN usages u ON u.clip_id = c.id
            WHERE 1 = 1
        """
        params: list = []
        if provider:
            sql += " AND s.provider = ?"
            params.append(provider)
        if exclude_sources:
            sql += f" AND s.external_id NOT IN ({','.join('?' * len(exclude_sources))})"
            params.extend(exclude_sources)
        sql += " GROUP BY c.id ORDER BY count(u.id) ASC, c.duration DESC LIMIT ?"
        params.append(limit)
        return [_to_clip(row) for row in self.connection.execute(sql, params)]

    def stats(self) -> dict:
        providers = {
            row["provider"]: row["n"]
            for row in self.connection.execute(
                "SELECT s.provider, count(*) AS n FROM clips c "
                "JOIN sources s ON s.id = c.source_id GROUP BY s.provider"
            )
        }
        totals = self.connection.execute(
            "SELECT count(*) AS clips, coalesce(sum(duration), 0) AS secs FROM clips"
        ).fetchone()
        sources = self.connection.execute("SELECT count(*) AS n FROM sources").fetchone()
        used = self.connection.execute(
            "SELECT count(DISTINCT clip_id) AS n FROM usages"
        ).fetchone()
        return {
            "clips": totals["clips"],
            "sources": sources["n"],
            "total_seconds": round(float(totals["secs"]), 1),
            "by_provider": providers,
            "clips_used": used["n"],
            "clips_never_used": totals["clips"] - used["n"],
        }


def cut_clip(video: Path, start: float, end: float, target: Path) -> None:
    """Cut one scene out of a video, normalized for the library.

    Re-encoded rather than stream-copied: a copy can only cut on keyframes, and
    at these durations that moves a boundary by more than the shot is long.
    Audio is dropped - only the picture is ever reused.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    scale = (
        f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_WIDTH}:{TARGET_HEIGHT},fps={TARGET_FPS},setsar=1"
    )
    command = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video),
        "-vf", scale, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-y", str(target),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        raise LibraryError(f"ffmpeg failed cutting {video.name} @ {start:.2f}s: {result.stderr[:300]}")


def import_clip(
    video: Path,
    library: Library,
    *,
    clip_id_hint: str,
    source_id: str,
    source_url: str,
    source_title: str,
    author: str,
    description: str,
    keywords: list[str],
    provider: str,
    duration: float,
    max_seconds: float | None = None,
) -> Clip:
    """File a whole video file as one library clip.

    Stock footage is a single continuous shot, so there is nothing to split on -
    unlike a finished Short, where the cuts are the whole point. Long clips are
    trimmed because segments run 2-4 seconds and the renderer only ever uses the
    head of one; keeping 30 seconds of it would be storage for nothing.
    """
    kept = min(duration, max_seconds) if max_seconds else duration
    stem = f"{slugify(description or clip_id_hint)}__{source_id}"
    target = library.root / CLIP_DIR / f"{stem}.mp4"
    cut_clip(video, 0.0, kept, target)
    clip = Clip(
        clip_id=stem,
        path=str(target.relative_to(library.root)).replace("\\", "/"),
        source_id=source_id,
        source_url=source_url,
        source_title=source_title,
        channel=author,
        scene_index=0,
        start=0.0,
        end=round(kept, 3),
        duration=round(kept, 3),
        text="",
        visual_description=description,
        keywords=keywords,
        provider=provider,
    )
    library.add(clip)
    return clip


def clips_from(
    video: Path,
    meta: dict,
    segments: list[Segment],
    library: Library,
    *,
    model: str | None = None,
    describe_clips: bool = True,
) -> list[Clip]:
    """Split one downloaded video into library clips."""
    source_id = meta.get("id") or video.stem
    source_title = meta.get("title") or ""
    made: list[Clip] = []
    for segment in segments:
        info = {"visual_description": "", "keywords": []}
        if describe_clips and segment.text:
            try:
                info = describe(segment.text, source_title, model=model)
            except LLMError:
                # A clip with no description is still usable footage - it falls
                # back to matching on narration text. Losing the whole harvest
                # over one bad reply is not a trade worth making.
                pass
        slug = slugify(info["visual_description"] or segment.text or source_title)
        stem = f"{slug}__{source_id}_{segment.index:02d}"
        target = library.root / CLIP_DIR / f"{stem}.mp4"
        cut_clip(video, segment.start, segment.end, target)
        clip = Clip(
            clip_id=stem,
            path=str(target.relative_to(library.root)).replace("\\", "/"),
            source_id=source_id,
            source_url=meta.get("url") or "",
            source_title=source_title,
            channel=meta.get("channel") or "",
            scene_index=segment.index,
            start=segment.start,
            end=segment.end,
            duration=segment.duration,
            text=segment.text,
            visual_description=info["visual_description"],
            keywords=info["keywords"],
        )
        library.add(clip)
        made.append(clip)
    return made


def segments_for(meta: dict, subtitle_path: Path | None, min_duration: float) -> list[Segment]:
    """Rebuild the shot plan for an already-downloaded video."""
    from .segments import build

    cues = (
        parse_timed_cues(subtitle_path.read_text(encoding="utf-8", errors="replace"))
        if subtitle_path and subtitle_path.exists()
        else []
    )
    return build(meta.get("scenes") or [], cues, min_duration=min_duration)
