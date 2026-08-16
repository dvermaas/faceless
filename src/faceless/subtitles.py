"""Turn downloaded caption files (VTT/SRT) into plain text an LLM can read.

YouTube's auto-captions are "rolling": each cue repeats the tail of the previous
one so the on-screen text scrolls. Dumping them verbatim doubles the token count
and reads like a stutter, so cues are de-duplicated against a small window of
recently emitted lines.
"""

from __future__ import annotations

import html
import re
import textwrap
from collections import deque
from pathlib import Path
from typing import NamedTuple

_CUE_TIME = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(?P<end>\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3})"
)
_TAG = re.compile(r"</?[^>]+>")
_SPEAKER = re.compile(r"^[-\s]*\[[^\]]{1,40}\]\s*")
_WRAP_WIDTH = 100
_DEDUPE_WINDOW = 4


def _clean(line: str) -> str:
    text = _TAG.sub("", line)
    text = html.unescape(text)
    text = text.replace("​", "").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _normalize_timestamp(stamp: str) -> str:
    """``00:01:02.500`` / ``01:02.500`` -> ``00:01:02``."""
    clock = stamp.replace(",", ".").split(".")[0]
    parts = clock.split(":")
    while len(parts) < 3:
        parts.insert(0, "00")
    return ":".join(part.zfill(2) for part in parts)


def _seconds(stamp: str) -> float:
    """``00:01:02.500`` / ``01:02.500`` -> ``62.5``."""
    clock, _, fraction = stamp.replace(",", ".").partition(".")
    parts = [int(part) for part in clock.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, secs = parts[-3:]
    return hours * 3600 + minutes * 60 + secs + float(f"0.{fraction or 0}")


def _parse_blocks(raw: str) -> list[tuple[str, str, list[str]]]:
    """Extract ``(start, end, text_lines)`` triples from VTT or SRT content."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[str, str, list[str]]] = []
    index = 0
    while index < len(lines):
        match = _CUE_TIME.match(lines[index].strip())
        if not match:
            index += 1
            continue
        start, end = match.group("start"), match.group("end")
        index += 1
        text_lines: list[str] = []
        while index < len(lines):
            current = lines[index].strip()
            if not current or _CUE_TIME.match(current):
                break
            text_lines.append(current)
            index += 1
        blocks.append((start, end, text_lines))
    return blocks


def parse_cues(raw: str) -> list[tuple[str, list[str]]]:
    """Extract ``(start, text_lines)`` pairs from VTT or SRT content."""
    return [(start, text_lines) for start, _, text_lines in _parse_blocks(raw)]


class TimedCue(NamedTuple):
    start: float
    end: float
    text: str


def parse_timed_cues(raw: str) -> list[TimedCue]:
    """Cues with float second bounds, rolling auto-caption repeats removed.

    `to_lines` rounds timestamps to whole seconds and drops cue end times, which
    is fine for reading but too coarse to decide which shot a line was spoken
    over. This keeps both bounds at full precision.
    """
    recent: deque[str] = deque(maxlen=_DEDUPE_WINDOW)
    cues: list[TimedCue] = []
    for start, end, text_lines in _parse_blocks(raw):
        fresh: list[str] = []
        for line in text_lines:
            text = _SPEAKER.sub("", _clean(line)).strip()
            if not text or text in recent:
                continue
            recent.append(text)
            fresh.append(text)
        if fresh:
            cues.append(TimedCue(_seconds(start), _seconds(end), " ".join(fresh)))
    return cues


def to_lines(raw: str) -> list[tuple[str, str]]:
    """Cleaned, de-duplicated ``(timestamp, text)`` pairs."""
    recent: deque[str] = deque(maxlen=_DEDUPE_WINDOW)
    out: list[tuple[str, str]] = []
    for start, text_lines in parse_cues(raw):
        stamp = _normalize_timestamp(start)
        for line in text_lines:
            text = _clean(line)
            text = _SPEAKER.sub("", text).strip()
            if not text or text in recent:
                continue
            recent.append(text)
            out.append((stamp, text))
    return out


def to_text(raw: str, *, timestamps: bool = False) -> str:
    """Render caption content as plain text, optionally keeping cue timestamps."""
    lines = to_lines(raw)
    if not lines:
        return ""
    if timestamps:
        return "\n".join(f"[{stamp}] {text}" for stamp, text in lines) + "\n"
    body = " ".join(text for _, text in lines)
    return textwrap.fill(body, width=_WRAP_WIDTH) + "\n"


def convert_file(path: Path, *, timestamps: bool = False) -> Path | None:
    """Write ``<name>.txt`` next to a caption file. Returns None if it held no text."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = to_text(raw, timestamps=timestamps)
    if not text.strip():
        return None
    target = path.with_suffix(".txt")
    target.write_text(text, encoding="utf-8")
    return target
