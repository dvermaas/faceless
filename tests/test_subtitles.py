"""Caption parsing: the two output paths, and the rolling-duplicate collapse."""

from __future__ import annotations

import pytest

from faceless import subtitles


def test_seconds_parses_both_stamp_shapes():
    assert subtitles._seconds("00:01:02.500") == pytest.approx(62.5)
    assert subtitles._seconds("01:02.500") == pytest.approx(62.5)
    assert subtitles._seconds("00:00:00,080") == pytest.approx(0.08)  # SRT comma


def test_timed_cues_keep_sub_second_precision(rolling_vtt):
    """Whole-second rounding is fine for reading, useless for aligning to shots."""
    cues = subtitles.parse_timed_cues(rolling_vtt)
    assert cues[0].start == pytest.approx(2.11)
    assert all(cue.end > cue.start for cue in cues)
    assert all(isinstance(cue.start, float) for cue in cues)
    assert any(cue.start % 1 for cue in cues), "fractional seconds must survive"


def test_whitespace_only_line_ends_the_cue_text():
    """Documents current behaviour, which costs about two seconds of alignment.

    YouTube writes a space-only line between the timing line and the karaoke
    text. `_parse_blocks` treats any line that strips to nothing as the end of
    the cue, so that first cue yields no text at all and the words are picked up
    from the following cue instead - roughly one cue later than they were
    spoken. Nothing is lost (rolling captions repeat the line) but every caption
    lands late, which drags narration one shot behind the picture.
    """
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.080 --> 00:00:02.110\n"
        " \n"
        "spoken early\n\n"
        "00:00:02.110 --> 00:00:04.000\n"
        "spoken early\n"
    )
    cues = subtitles.parse_timed_cues(vtt)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(2.11)  # not 0.08, where it was said


def test_rolling_duplicates_are_collapsed(rolling_vtt):
    """Each auto-caption cue repeats the previous line; it must appear once."""
    cues = subtitles.parse_timed_cues(rolling_vtt)
    spoken = " ".join(cue.text for cue in cues)
    assert spoken.count("a camel can") == 1
    assert spoken.count("drink a lot") == 1


def test_inline_tags_and_entities_are_stripped(rolling_vtt):
    text = subtitles.to_text(rolling_vtt)
    assert "<c>" not in text
    assert "00:00:00.240" not in text
    assert "&nbsp;" not in text
    assert "of water today" in text


def test_to_text_timestamps_are_whole_seconds(rolling_vtt):
    """The human-readable path stays coarse - that is its job."""
    text = subtitles.to_text(rolling_vtt, timestamps=True)
    assert text.startswith("[00:00:02] ")
    # HH:MM:SS only - no fractional part on this path.
    assert all("." not in line.split("]")[0] for line in text.splitlines())


def test_parse_cues_still_returns_start_and_lines(rolling_vtt):
    """The legacy shape is relied on by to_text; adding end times must not move it."""
    cues = subtitles.parse_cues(rolling_vtt)
    assert isinstance(cues[0][0], str)
    assert isinstance(cues[0][1], list)


def test_srt_input_is_accepted():
    srt = "1\n00:00:01,000 --> 00:00:03,000\nhello there\n\n"
    cues = subtitles.parse_timed_cues(srt)
    assert len(cues) == 1
    assert cues[0].text == "hello there"
    assert cues[0].start == pytest.approx(1.0)


def test_empty_input_yields_nothing():
    assert subtitles.parse_timed_cues("") == []
    assert subtitles.to_text("") == ""
