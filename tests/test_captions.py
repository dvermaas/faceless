"""ASS caption generation. ffmpeg is stubbed - nothing is measured or encoded."""

from __future__ import annotations

import subprocess

import pytest

from faceless import captions, remix
from faceless.captions import CaptionError, CaptionStyle, build_ass, measure_widths
from faceless.subtitles import Word


def _events(script: str) -> list[str]:
    return [line for line in script.splitlines() if line.startswith("Dialogue:")]


def _text_of(event: str) -> str:
    """The drawn text, after the override block."""
    return event.rsplit("}", 1)[1]


# -- colours and times ---------------------------------------------------


def test_colour_is_written_back_to_front():
    """ASS stores colour as &HAABBGGRR, so red and blue swap places."""
    assert captions._ass_colour("#FF0000") == "&H000000FF"
    assert captions._ass_colour("#0000FF") == "&H00FF0000"
    assert captions._ass_colour("00FF00") == "&H0000FF00"


@pytest.mark.parametrize("bad", ["red", "#FFF", "#GGGGGG", ""])
def test_a_colour_that_is_not_a_hex_triple_is_refused(bad):
    with pytest.raises(CaptionError, match="RRGGBB"):
        captions._ass_colour(bad)


def test_times_are_centiseconds():
    """ASS has no millisecond field; rendering one produces an unplayable line."""
    assert captions._ass_time(0.0) == "0:00:00.00"
    assert captions._ass_time(62.5) == "0:01:02.50"
    assert captions._ass_time(3661.239) == "1:01:01.24"


# -- the script ----------------------------------------------------------


def test_one_event_per_word():
    words = [Word(0.0, 0.5, "a"), Word(0.5, 1.0, "b"), Word(1.0, 1.5, "c")]
    assert len(_events(build_ass(words, CaptionStyle(), 1080, 1920))) == 3


def test_words_are_upper_cased_unless_asked_otherwise():
    words = [Word(0.0, 0.5, "Lion")]
    assert _text_of(_events(build_ass(words, CaptionStyle(), 1080, 1920))[0]) == "LION"
    mixed = build_ass(words, CaptionStyle(uppercase=False), 1080, 1920)
    assert _text_of(_events(mixed)[0]) == "Lion"


def test_position_is_measured_up_from_the_bottom():
    """0.30 puts the middle of the word 30% of the frame above the bottom edge."""
    script = build_ass([Word(0.0, 0.5, "x")], CaptionStyle(position=0.30), 1080, 1920)
    assert "\\pos(540,1344)" in script  # 1920 - 0.30 * 1920
    higher = build_ass([Word(0.0, 0.5, "x")], CaptionStyle(position=0.5), 1080, 1920)
    assert "\\pos(540,960)" in higher


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_a_position_outside_the_frame_is_refused(bad):
    with pytest.raises(CaptionError, match="fraction"):
        CaptionStyle(position=bad)


def test_a_size_of_zero_is_refused():
    with pytest.raises(CaptionError, match="positive"):
        CaptionStyle(size=0)


def test_outline_and_shadow_scale_with_the_size():
    """Otherwise a bigger font comes out looking thinner, not fatter."""
    small, large = CaptionStyle(size=100), CaptionStyle(size=200)
    assert large.outline == pytest.approx(small.outline * 2)
    assert large.shadow == pytest.approx(small.shadow * 2)


def test_markup_characters_are_stripped_from_the_words():
    """A brace in a transcript would open an override block and be executed."""
    words = [Word(0.0, 0.5, "{\\b1}bold")]
    assert _text_of(_events(build_ass(words, CaptionStyle(), 1080, 1920))[0]) == "B1BOLD"


def test_captioning_nothing_is_an_error():
    """Silently emitting an empty script would render a video with no captions."""
    with pytest.raises(CaptionError, match="no words"):
        build_ass([], CaptionStyle(), 1080, 1920)


def test_the_pop_can_be_turned_off():
    plain = build_ass([Word(0.0, 0.5, "x")], CaptionStyle(pop=False), 1080, 1920)
    assert "\\t(" not in plain
    assert "\\t(" in build_ass([Word(0.0, 0.5, "x")], CaptionStyle(), 1080, 1920)


# -- fitting -------------------------------------------------------------


def test_a_word_too_wide_for_the_frame_is_scaled_down():
    """libass will not break inside a word - unfitted, it runs off both edges."""
    style = CaptionStyle(size=200, pop=False)
    wide = captions._fit("COUNTERATTACKED", style, {"COUNTERATTACKED": 618.0}, 950)
    assert 40 <= wide < 100


def test_a_word_that_already_fits_is_left_alone():
    style = CaptionStyle(size=200, pop=False)
    assert captions._fit("THE", style, {"THE": 106.0}, 950) == 100


def test_the_pop_overshoot_has_to_fit_too():
    """The word is briefly drawn 12% larger; that is the width that must fit."""
    measured = {"WORD": 420.0}
    flat = captions._fit("WORD", CaptionStyle(size=200, pop=False), measured, 950)
    popped = captions._fit("WORD", CaptionStyle(size=200, pop=True), measured, 950)
    assert flat == 100 and popped < 100


def test_an_unmeasured_word_is_drawn_at_full_size():
    """Measurement is best-effort; losing it must cost fitting, not the render."""
    assert captions._fit("MYSTERY", CaptionStyle(), {}, 950) == 100


def test_fitting_never_shrinks_a_word_to_illegibility():
    huge = captions._fit("X", CaptionStyle(size=200), {"X": 99_000.0}, 950)
    assert huge == 40


def test_a_fitted_word_carries_its_scale_into_the_pop():
    """The pop must animate down to the fitted size, not back up to 100%."""
    words = [Word(0.0, 0.5, "counterattacked")]
    script = build_ass(
        words, CaptionStyle(size=200), 1080, 1920, measured={"COUNTERATTACKED": 618.0}
    )
    event = _events(script)[0]
    assert "\\fscx66\\fscy66)" in event  # settles at the fitted size
    assert "\\fscx74\\fscy74\\t" in event  # starts 12% above it


# -- measurement ---------------------------------------------------------


def test_widths_are_read_off_cropdetect(monkeypatch):
    stderr = (
        "[Parsed_cropdetect_1 @ 0] x1:0 x2:0 y1:0 y2:0 w:106 h:66 x:1 y:1 pts:0 t:0 limit:24\n"
        "[Parsed_cropdetect_1 @ 0] x1:0 x2:0 y1:0 y2:0 w:618 h:66 x:1 y:1 pts:1 t:1 limit:24\n"
    )
    monkeypatch.setattr(
        captions.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=stderr),
    )
    # measure_widths sorts the vocabulary, so pts 0 is COUNTER... and pts 1 THE.
    assert measure_widths(["THE", "COUNTERATTACKED"], "Impact") == {
        "COUNTERATTACKED": 106.0,
        "THE": 618.0,
    }


def test_a_frame_cropdetect_found_nothing_in_is_not_a_measurement(monkeypatch):
    """An empty frame reports a negative width, which would fit as "infinitely wide"."""
    stderr = "[Parsed_cropdetect_1 @ 0] x1:0 x2:0 y1:0 y2:0 w:-3998 h:-398 x:4000 y:400 pts:0 t:0\n"
    monkeypatch.setattr(
        captions.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=stderr),
    )
    assert measure_widths(["THE"], "Impact") == {}


def test_measuring_nothing_calls_no_encoder(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("ffmpeg must not run with no words to measure")

    monkeypatch.setattr(captions.subprocess, "run", explode)
    assert measure_widths([], "Impact") == {}


def test_a_missing_ffmpeg_costs_fitting_and_nothing_else(monkeypatch):
    def missing(*args, **kwargs):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(captions.subprocess, "run", missing)
    assert measure_widths(["THE"], "Impact") == {}


# -- fonts ---------------------------------------------------------------


def test_a_substituted_font_is_noticed():
    """libass never errors on an unknown font; it swaps one in and says nothing."""
    swapped = "[Parsed_subtitles_0 @ 0] fontselect: (Comic Papyrus, 700, 0) -> Arial, 0, Arial"
    assert captions.missing_font(swapped, "Comic Papyrus")


def test_the_font_that_was_asked_for_is_not_reported_as_missing():
    found = "[Parsed_subtitles_0 @ 0] fontselect: (Impact, 700, 0) -> Impact, 0, Impact"
    assert not captions.missing_font(found, "Impact")
    spaced = "[Parsed_subtitles_0 @ 0] fontselect: (Arial Black, 700, 0) -> Arial Black, 0, Arial"
    assert not captions.missing_font(spaced, "Arial Black")


def test_stderr_without_a_fontselect_line_is_not_read_as_a_substitution():
    assert not captions.missing_font("frame= 1 fps=0.0 q=-0.0", "Impact")


# -- the remix wiring ----------------------------------------------------


@pytest.fixture
def installed_font(monkeypatch):
    """Pretend every font resolves, so these tests are about the captions."""
    monkeypatch.setattr(remix, "probe_font", lambda font: True)


def test_captions_are_written_beside_the_video(tmp_path, monkeypatch, installed_font):
    monkeypatch.setattr(remix, "write_ass", lambda words, style, target, w, h: target)
    vtt = tmp_path / "target.en.vtt"
    vtt.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n \na<00:00:00.5><c> lion</c>\n",
        encoding="utf-8",
    )
    target = tmp_path / "out.ass"
    assert remix._write_captions(vtt, target, CaptionStyle()) == target


def test_asking_for_captions_without_a_subtitle_track_is_an_error(tmp_path, installed_font):
    """Quietly rendering without them would look like the flag was ignored."""
    with pytest.raises(CaptionError, match="no subtitle track"):
        remix._write_captions(None, tmp_path / "out.ass", CaptionStyle())


def test_a_subtitle_track_with_no_words_is_an_error(tmp_path, installed_font):
    empty = tmp_path / "empty.en.vtt"
    empty.write_text("WEBVTT\n\n", encoding="utf-8")
    with pytest.raises(CaptionError, match="no words"):
        remix._write_captions(empty, tmp_path / "out.ass", CaptionStyle())


def test_an_uninstalled_font_stops_the_render(monkeypatch):
    """libass would substitute silently and the whole encode would be wasted."""
    monkeypatch.setattr(remix, "probe_font", lambda font: False)
    with pytest.raises(CaptionError, match="no font named"):
        remix._check_font(CaptionStyle(font="Nonesuch"))


def test_an_installed_font_passes_the_check(monkeypatch):
    monkeypatch.setattr(remix, "probe_font", lambda font: True)
    assert remix._check_font(CaptionStyle(font="Impact")) is None
