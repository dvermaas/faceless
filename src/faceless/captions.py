"""Burned-in word-by-word captions, in the style Shorts actually use.

One word on screen at a time, centred, heavy, and timed to the moment it is
spoken. The words and their timings come from `subtitles.parse_words`; this
module turns them into an ASS script and hands that to libass through ffmpeg.

ASS rather than a wall of `drawtext` filters: a 40-second Short runs to well
over a hundred words, and that many chained `drawtext` filters is a filtergraph
ffmpeg spends longer parsing than encoding - besides which the outline, shadow
and pop animation below are one style line in ASS and a fight in drawtext.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .subtitles import Word

# Impact is on every Windows install and is the meme-caption default. Arial
# Black and Segoe UI Black are the other two safe heavy faces here.
DEFAULT_FONT = "Impact"
# 200 fills roughly four fifths of a 1080-wide frame with an average word, which
# is the weight this style wants. 250 already crowds the margins.
DEFAULT_SIZE = 200
# Fraction of frame height between the bottom edge and the middle of the word.
DEFAULT_POSITION = 0.30
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
# Outline and shadow are fractions of the font size, so changing --caption-size
# keeps the proportions instead of thinning the text out.
_OUTLINE_RATIO = 0.075
_SHADOW_RATIO = 0.035
# How long the pop-in takes, in milliseconds, and how big the word starts.
_POP_MS = 90
_POP_SCALE = 112
# Side margin, as a fraction of frame width, kept clear at each edge.
_MARGIN_RATIO = 0.06

# Word widths are measured once at this size on a canvas wide enough that
# nothing can touch an edge, then scaled: glyph advance is linear in font size.
_MEASURE_SIZE = 100
_MEASURE_WIDTH, _MEASURE_HEIGHT = 4000, 400
# cropdetect reports the bounding box of the non-black content of each frame. A
# frame it found nothing in reports a negative width, so the sign is matched and
# those are dropped rather than read as a measurement.
_CROP = re.compile(r"\bw:(-?\d+)\s+h:-?\d+\s+x:\d+\s+y:\d+\s+pts:(\d+)")
# cropdetect's own default threshold, given as an integer on purpose: this build
# reads the documented 0-1 fractional form differently, and `limit=0.06` matches
# the whole frame while `limit=64` matches nothing at all.
_CROP_LIMIT = 24


class CaptionError(RuntimeError):
    """Raised when captions cannot be built or burned in."""


def _ass_colour(value: str) -> str:
    """`#RRGGBB` -> `&H00BBGGRR`. ASS stores colour backwards, with alpha first."""
    match = _HEX.match(value.strip())
    if not match:
        raise CaptionError(f"colour must be #RRGGBB, not {value!r}")
    red, green, blue = (match.group(1)[index : index + 2] for index in (0, 2, 4))
    return f"&H00{blue}{green}{red}".upper()


def _ass_time(seconds: float) -> str:
    """Seconds -> `H:MM:SS.cc`. ASS keeps centiseconds, not milliseconds."""
    seconds = max(seconds, 0.0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    """Strip the characters ASS reads as markup rather than as text.

    Braces open an override block and a backslash starts an escape (`\\N` is a
    line break), so either arriving from a transcript would be executed rather
    than drawn. ASS offers no way to quote them and no spoken word contains
    them, so they are simply dropped.
    """
    return text.replace("{", "").replace("}", "").replace("\\", "")


@dataclass(slots=True)
class CaptionStyle:
    """How the words look. Everything here is exposed on the command line."""

    font: str = DEFAULT_FONT
    size: int = DEFAULT_SIZE
    colour: str = "#FFFFFF"
    outline_colour: str = "#000000"
    position: float = DEFAULT_POSITION
    uppercase: bool = True
    pop: bool = True

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise CaptionError(f"caption size must be positive, not {self.size}")
        if not 0.0 <= self.position <= 1.0:
            raise CaptionError(
                f"caption position is a fraction of frame height, so 0.0-1.0, not {self.position}"
            )
        # Fail here rather than let libass quietly render a default-coloured video.
        _ass_colour(self.colour)
        _ass_colour(self.outline_colour)

    @property
    def outline(self) -> float:
        return round(self.size * _OUTLINE_RATIO, 1)

    @property
    def shadow(self) -> float:
        return round(self.size * _SHADOW_RATIO, 1)


def _header(font: str, size: int, width: int, height: int, outline: float, shadow: float) -> list[str]:
    """The ASS preamble and the single style every word is drawn in."""
    margin = round(width * _MARGIN_RATIO)
    return [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        # Without this the outline keeps a fixed pixel width while the pop
        # animation scales the glyphs, and the border visibly breathes.
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Bold is -1 (true) on top of an already-heavy face: libass synthesises
        # weight when the family has no bold cut, which is the look this wants.
        f"Style: Word,{font},{size},{{primary}},{{primary}},{{outline_colour}},&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},5,{margin},{margin},{margin},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def measure_widths(words: list[str], font: str) -> dict[str, float]:
    """Exact rendered width of each distinct word, in pixels at `_MEASURE_SIZE`.

    libass will not break inside a word, so a word wider than the frame does not
    wrap - it runs off both edges, which is the single worst-looking thing this
    feature can do. Fitting it needs real widths, and estimating them from the
    character count cannot work across fonts: Impact is half the width of Arial
    Black at the same size.

    So ask the renderer. Each word is drawn on its own frame of a canvas wide
    enough that nothing can clip, and `cropdetect` reports the bounding box of
    what was actually drawn. One ffmpeg pass covers the whole vocabulary, and it
    is measured in the very font libass went on to use.

    Returns whatever it managed to measure; callers treat a missing word as
    "do not scale", so a failure here costs fitting, not the render.
    """
    distinct = sorted({word for word in words if word})
    if not distinct:
        return {}

    # No outline or shadow while measuring: they pad the box by a known amount
    # that `_fit` adds back, and leaving them in would make short words look
    # proportionally wider than they are.
    head = _header(font, _MEASURE_SIZE, _MEASURE_WIDTH, _MEASURE_HEIGHT, 0, 0)
    head = [
        line.replace("{primary}", "&H00FFFFFF").replace("{outline_colour}", "&H00000000")
        for line in head
    ]
    centre = f"{{\\an5\\pos({_MEASURE_WIDTH // 2},{_MEASURE_HEIGHT // 2})}}"
    events = [
        f"Dialogue: 0,{_ass_time(index)},{_ass_time(index + 1)},Word,,0,0,0,,{centre}{_escape(word)}"
        for index, word in enumerate(distinct)
    ]

    workdir = Path(tempfile.mkdtemp(prefix="faceless-measure-"))
    try:
        (workdir / "measure.ass").write_text("\n".join(head + events) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin",
                "-f", "lavfi",
                "-i", f"color=c=black:s={_MEASURE_WIDTH}x{_MEASURE_HEIGHT}:r=1:d={len(distinct)}",
                # skip=0 and reset_count=1 make cropdetect report every frame
                # rather than settling on a running maximum.
                "-vf",
                f"subtitles=measure.ass,cropdetect=limit={_CROP_LIMIT}:round=2:skip=0:reset_count=1",
                "-f", "null", "-",
            ],  # fmt: skip
            capture_output=True,
            text=True,
            cwd=workdir,
        )
    except OSError:
        return {}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    measured: dict[str, float] = {}
    for match in _CROP.finditer(result.stderr):
        width, index = int(match.group(1)), int(match.group(2))
        if width > 0 and index < len(distinct):
            measured[distinct[index]] = float(width)
    return measured


def _fit(word: str, style: CaptionStyle, measured: dict[str, float], usable: float) -> int:
    """Percentage to draw `word` at so it stays inside the margins.

    100 unless the word is too wide, and never below 40 - past that the text is
    too small to read at arm's length and clipping would be the lesser evil.
    """
    natural = measured.get(word)
    if not natural:
        return 100
    # The outline is drawn outside the glyphs, so it eats into the usable width
    # on both sides, and it scales with the text.
    width = natural * style.size / _MEASURE_SIZE + 2 * style.outline
    # The pop overshoots before settling, so the *overshoot* is what has to fit.
    if style.pop:
        width *= _POP_SCALE / 100
    if width <= usable:
        return 100
    return max(40, int(usable / width * 100))


def build_ass(
    words: list[Word],
    style: CaptionStyle,
    width: int,
    height: int,
    *,
    measured: dict[str, float] | None = None,
) -> str:
    """Render the word list as an ASS script sized for a `width`x`height` frame."""
    if not words:
        raise CaptionError("no words to caption - the video has no usable captions")

    head = [
        line.replace("{primary}", _ass_colour(style.colour)).replace(
            "{outline_colour}", _ass_colour(style.outline_colour)
        )
        for line in _header(style.font, style.size, width, height, style.outline, style.shadow)
    ]

    # Anchor 5 is middle-centre, so \pos names the middle of the word: a long
    # word grows in both directions instead of drifting off one edge.
    centre_x, centre_y = width // 2, round(height * (1.0 - style.position))
    usable = width - 2 * round(width * _MARGIN_RATIO)
    measured = measured or {}

    events = []
    for word in words:
        text = _escape(word.text.upper() if style.uppercase else word.text)
        scale = _fit(text, style, measured, usable)
        if style.pop:
            grown = round(scale * _POP_SCALE / 100)
            sizing = f"\\fscx{grown}\\fscy{grown}\\t(0,{_POP_MS},\\fscx{scale}\\fscy{scale})"
        else:
            sizing = "" if scale == 100 else f"\\fscx{scale}\\fscy{scale}"
        events.append(
            f"Dialogue: 0,{_ass_time(word.start)},{_ass_time(word.end)},Word,,0,0,0,,"
            f"{{\\an5\\pos({centre_x},{centre_y}){sizing}}}{text}"
        )
    return "\n".join(head + events) + "\n"


def write_ass(
    words: list[Word],
    style: CaptionStyle,
    target: Path,
    width: int,
    height: int,
    *,
    fit: bool = True,
) -> Path:
    """Build the ASS script for `words` and write it to `target`."""
    measured = None
    if fit:
        wanted = [word.text.upper() if style.uppercase else word.text for word in words]
        measured = measure_widths([_escape(text) for text in wanted], style.font)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        build_ass(words, style, width, height, measured=measured), encoding="utf-8"
    )
    return target


def missing_font(stderr: str, font: str) -> bool:
    """Did libass substitute a different face for the one that was asked for?

    libass never fails on an unknown font - it quietly falls back, so a typo in
    --caption-font otherwise produces a finished video in the wrong typeface
    with nothing to say so. Its info-level `fontselect` line names what it
    settled on, and that is the only place the substitution surfaces.
    """
    wanted = re.sub(r"[^a-z0-9]", "", font.lower())
    for line in stderr.splitlines():
        if "fontselect" not in line or "->" not in line:
            continue
        chosen = re.sub(r"[^a-z0-9]", "", line.rsplit("->", 1)[1].lower())
        if wanted and wanted not in chosen:
            return True
    return False


def probe_font(font: str) -> bool:
    """Ask libass to resolve `font` against one frame of nothing, and watch.

    Cheap enough to run before a render (a 64x64 frame, no encoder) and worth
    it: finding out the typeface was wrong after the encode means encoding
    again.
    """
    workdir = Path(tempfile.mkdtemp(prefix="faceless-font-"))
    try:
        (workdir / "probe.ass").write_text(
            build_ass([Word(0.0, 0.1, "x")], CaptionStyle(font=font), 1080, 1920),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "ffmpeg", "-v", "info", "-nostdin",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-vf", "subtitles=probe.ass", "-frames:v", "1", "-f", "null", "-",
            ],  # fmt: skip
            capture_output=True,
            text=True,
            cwd=workdir,
        )
    except OSError:
        return True  # cannot tell; never block a render over a warning
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return not missing_font(result.stderr, font)
