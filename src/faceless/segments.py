"""Align a video's shot list with its narration.

A *segment* is one shot of the finished edit: a time range, and the words spoken
over it. Segments start as detected scenes, but scenes cut faster than footage
can carry - a 0.8s shot is a flash, not a shot - so short ones are merged until
every segment can hold a clip of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .subtitles import TimedCue

DEFAULT_MIN_DURATION = 1.5


@dataclass(slots=True)
class Segment:
    index: int
    start: float
    end: float
    text: str
    scenes: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": self.duration,
            "text": self.text,
            "scenes": self.scenes,
        }


def _assign(scenes: list[dict], cues: list[TimedCue]) -> list[list[str]]:
    """Bucket each cue into the scene it was mostly spoken over.

    Rolling auto-captions emit occasional near-zero-duration cues, which overlap
    nothing measurable - those fall back to whichever scene contains their start.
    """
    buckets: list[list[str]] = [[] for _ in scenes]
    for cue in cues:
        best, best_overlap = None, 0.0
        for position, scene in enumerate(scenes):
            overlap = min(cue.end, scene["end"]) - max(cue.start, scene["start"])
            if overlap > best_overlap:
                best, best_overlap = position, overlap
        if best is None:
            for position, scene in enumerate(scenes):
                if scene["start"] <= cue.start < scene["end"]:
                    best = position
                    break
        if best is not None:
            buckets[best].append(cue.text)
    return buckets


def _merge_shortest(segments: list[Segment], min_duration: float) -> list[Segment]:
    """Fold under-length segments into a neighbour, shortest-first.

    Merging into the *shorter* neighbour keeps durations even. Folding blindly
    into the previous one would grow a single segment far past the rest, and one
    clip then has to carry a disproportionate share of the video.
    """
    while len(segments) > 1:
        position = min(range(len(segments)), key=lambda i: segments[i].duration)
        if segments[position].duration >= min_duration:
            break
        before = segments[position - 1] if position > 0 else None
        after = segments[position + 1] if position + 1 < len(segments) else None
        if before is None:
            target, victim = after, segments[position]
        elif after is None:
            target, victim = before, segments[position]
        else:
            target = before if before.duration <= after.duration else after
            victim = segments[position]

        # Order the narration before moving the bounds - once target.start has
        # been widened there is no way left to tell which came first.
        ordered = (
            (victim.text, target.text)
            if victim.start < target.start
            else (target.text, victim.text)
        )
        target.text = " ".join(part for part in ordered if part)
        target.start = min(target.start, victim.start)
        target.end = max(target.end, victim.end)
        target.scenes = sorted(target.scenes + victim.scenes)
        segments.remove(victim)

    for index, segment in enumerate(segments):
        segment.index = index
    return segments


def build(
    scenes: list[dict],
    cues: list[TimedCue],
    *,
    min_duration: float = DEFAULT_MIN_DURATION,
) -> list[Segment]:
    """Turn a scene list and caption cues into the shot plan for a rebuild."""
    if not scenes:
        return []
    buckets = _assign(scenes, cues)
    segments = [
        Segment(
            index=position,
            start=float(scene["start"]),
            end=float(scene["end"]),
            text=" ".join(buckets[position]).strip(),
            scenes=[scene.get("index", position)],
        )
        for position, scene in enumerate(scenes)
    ]
    return _merge_shortest(segments, min_duration)
