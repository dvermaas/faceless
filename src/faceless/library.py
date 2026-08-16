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

    def to_dict(self) -> dict:
        return asdict(self)


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


class Library:
    """Clip index backed by a JSON file on disk."""

    def __init__(self, root: Path | str = "library") -> None:
        self.root = Path(root)
        self.index_path = self.root / INDEX_NAME
        self.clips: list[Clip] = []
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LibraryError(f"{self.index_path} is not valid JSON: {exc}") from exc
        self.clips = [Clip(**record) for record in raw.get("clips", [])]

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "clips": [clip.to_dict() for clip in self.clips]}
        self.index_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @property
    def source_ids(self) -> set[str]:
        return {clip.source_id for clip in self.clips}

    def has_source(self, source_id: str) -> bool:
        return source_id in self.source_ids

    def add(self, clip: Clip) -> None:
        self.clips.append(clip)

    def candidates(self, *, exclude_sources: set[str] = frozenset()) -> list[Clip]:
        return [clip for clip in self.clips if clip.source_id not in exclude_sources]

    def stats(self) -> dict:
        return {
            "clips": len(self.clips),
            "sources": len(self.source_ids),
            "total_seconds": round(sum(clip.duration for clip in self.clips), 1),
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
