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


def _scan(raw: str, *, ends_cue) -> list[tuple[str, str, list[str]]]:
    """Extract ``(start, end, text_lines)`` triples from VTT or SRT content.

    `ends_cue` decides which line closes a cue, and the two callers below
    disagree about it on purpose - see `_parse_blocks`.
    """
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
            current = lines[index]
            if ends_cue(current) or _CUE_TIME.match(current.strip()):
                break
            if current.strip():
                text_lines.append(current.strip())
            index += 1
        blocks.append((start, end, text_lines))
    return blocks


def _parse_blocks(raw: str) -> list[tuple[str, str, list[str]]]:
    """Blocks for the transcript paths, where *any* blank-looking line ends a cue.

    This is the one-cue-late defect: YouTube writes a space-only line between
    the timing line and the karaoke text, and treating that as the end of the
    cue loses the cue's words to the next one. Changing the rule here shifts
    every transcript timing and every remix with them, so it stays pinned by a
    test until somebody fixes it deliberately. The word-timing path below is
    unaffected, so captions land on the right frame regardless.
    """
    return _scan(raw, ends_cue=lambda line: not line.strip())


def _parse_blocks_raw(raw: str) -> list[tuple[str, str, list[str]]]:
    """Blocks for the word-timing path, per the WebVTT rule: only "" ends a cue.

    A line holding a space is not an empty line, so the karaoke text following
    one still belongs to its own cue - which is what puts per-word stamps where
    the words were actually spoken.
    """
    return _scan(raw, ends_cue=lambda line: line == "")


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


class Word(NamedTuple):
    start: float
    end: float
    text: str


_INLINE_TIME = re.compile(r"<(\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3})>")
_NON_SPEECH = re.compile(r"^[\[(][^\])]*[\])]$")
# A word held much longer than this is a pause, not speech; the caption drops
# rather than sitting stale on screen until the next line starts.
MAX_HOLD = 1.0
MIN_HOLD = 0.08


def _words_between(text: str, start: float, end: float) -> list[Word]:
    """Spread one chunk of caption text evenly over `start`-`end`.

    YouTube times the *first* word of each `<c>` chunk, and a chunk is almost
    always exactly one word - but "in the" style chunks do occur, and the
    even-split fallback below feeds whole cues through here. Splitting evenly is
    a guess either way; it is only ever applied inside a span whose bounds are
    real.
    """
    tokens = [token for token in text.split() if token and not _NON_SPEECH.match(token)]
    if not tokens:
        return []
    span = max(end - start, 0.0) / len(tokens)
    return [
        Word(start + position * span, start + (position + 1) * span, token)
        for position, token in enumerate(tokens)
    ]


def _karaoke_words(start: float, end: float, line: str) -> list[Word]:
    """Read per-word timings off one `a<00:00:00.24><c> b</c>` caption line.

    The leading text carries no stamp of its own - it starts when the cue does.
    Every later chunk is stamped explicitly, and runs until the next stamp.
    """
    parts = _INLINE_TIME.split(line)
    chunks: list[tuple[float, str]] = [(start, _clean(parts[0]))]
    for position in range(1, len(parts) - 1, 2):
        chunks.append((_seconds(parts[position]), _clean(parts[position + 1])))

    words: list[Word] = []
    for position, (chunk_start, text) in enumerate(chunks):
        chunk_end = chunks[position + 1][0] if position + 1 < len(chunks) else end
        words.extend(_words_between(text, chunk_start, max(chunk_end, chunk_start)))
    return words


def parse_words(raw: str, *, max_hold: float = MAX_HOLD) -> list[Word]:
    """Per-word timings, for captions that appear as the word is spoken.

    This is the third and last of this module's output paths, and the only one
    that reads YouTube's inline karaoke stamps instead of throwing them away.
    That is also why it sidesteps the one-cue-late defect that afflicts
    `parse_timed_cues`: a word's time is read from its own `<...>` stamp, not
    from the cue that happens to carry it, so a mis-parsed cue boundary cannot
    shift it. Do not "unify" this with the other two paths.

    Three kinds of line turn up and each is handled where it is met, rather
    than by choosing one strategy for the whole file:

    * stamped, so read per word - the case that matters;
    * unstamped, and a rolling repeat of a line already seen - dropped;
    * unstamped and new, which is every line of a manual or SRT track and the
      occasional tail of an auto one - spread evenly over its own cue.

    That last case is why this does not simply ignore unstamped lines: doing so
    silently drops the closing words of any file that ends on a repeat, and
    loses manual subtitle tracks entirely.
    """
    recent: deque[str] = deque(maxlen=_DEDUPE_WINDOW)
    words: list[Word] = []
    for start, end, lines in _parse_blocks_raw(raw):
        for line in lines:
            text = _SPEAKER.sub("", _clean(line)).strip()
            if not text or text in recent:
                continue
            recent.append(text)
            if _INLINE_TIME.search(line):
                words.extend(_karaoke_words(_seconds(start), _seconds(end), line))
            else:
                words.extend(_words_between(text, _seconds(start), _seconds(end)))

    words.sort(key=lambda word: word.start)
    return _tighten(words, max_hold)


def _tighten(words: list[Word], max_hold: float) -> list[Word]:
    """Trim each word so exactly one is ever on screen, and none linger.

    Overlapping words would put two on screen at once, which is the one thing
    this style must not do, so a word never outlives the start of the next.
    """
    out: list[Word] = []
    seen: set[tuple[int, str]] = set()
    for position, word in enumerate(words):
        key = (round(word.start * 1000), word.text)
        if key in seen:
            continue
        seen.add(key)
        ceiling = words[position + 1].start if position + 1 < len(words) else word.end
        end = min(max(word.end, word.start + MIN_HOLD), word.start + max_hold, ceiling)
        if end <= word.start:
            end = min(ceiling, word.start + max_hold)
        if end > word.start:
            out.append(Word(word.start, end, word.text))
    return out
