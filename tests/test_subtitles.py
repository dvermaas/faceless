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
    assert subtitles.parse_words("") == []


# -- the word path, which burned-in captions are timed from ---------------


def test_each_word_is_timed_from_its_own_karaoke_stamp(rolling_vtt):
    words = subtitles.parse_words(rolling_vtt)
    spoken = {word.text: word.start for word in words}
    assert spoken["camel"] == pytest.approx(0.240)
    assert spoken["can"] == pytest.approx(0.640)
    assert spoken["lot"] == pytest.approx(2.900)


def test_the_first_word_starts_when_its_cue_does(rolling_vtt):
    """The lead word carries no stamp of its own - the cue's start is its start."""
    assert subtitles.parse_words(rolling_vtt)[0] == subtitles.Word(0.080, 0.240, "a")


def test_the_word_path_is_not_one_cue_late(rolling_vtt):
    """The defect that shifts `parse_timed_cues` must not reach the captions.

    A word's time comes from its own inline stamp, so where the cue boundary
    was misread is irrelevant - which is why a caption lands on the right frame
    while the transcript of the same file is about two seconds behind.
    """
    assert subtitles.parse_timed_cues(rolling_vtt)[0].start == pytest.approx(2.110)
    assert subtitles.parse_words(rolling_vtt)[0].start == pytest.approx(0.080)


def test_only_one_word_is_ever_on_screen(rolling_vtt):
    """Two at once is the one thing this caption style must never do."""
    words = subtitles.parse_words(rolling_vtt)
    assert all(this.end <= following.start for this, following in zip(words, words[1:]))


def test_a_word_does_not_linger_through_a_silence(rolling_vtt):
    """`can` runs to the next cue 1.5s later; holding it that long reads as stuck."""
    held = next(word for word in subtitles.parse_words(rolling_vtt) if word.text == "can")
    assert held.end - held.start == pytest.approx(subtitles.MAX_HOLD)


def test_rolling_repeats_are_not_captioned_twice(rolling_vtt):
    words = subtitles.parse_words(rolling_vtt)
    assert [word.text for word in words].count("camel") == 1


def test_a_closing_line_that_was_never_stamped_is_still_captioned(rolling_vtt):
    """The last line of this file is plain text; ignoring it would lose the ending."""
    words = subtitles.parse_words(rolling_vtt)
    assert [word.text for word in words[-3:]] == ["of", "water", "today"]


def test_a_track_with_no_stamps_at_all_is_spread_across_its_cues():
    """Manual subtitles and SRT carry no karaoke timing; even spacing is the best guess."""
    srt = "1\n00:00:01,000 --> 00:00:04,000\nhello there world\n\n"
    words = subtitles.parse_words(srt)
    assert [word.text for word in words] == ["hello", "there", "world"]
    assert words[0].start == pytest.approx(1.0)
    assert words[-1].end == pytest.approx(4.0)


def test_bracketed_sound_effects_are_not_captioned():
    """`[music]` is timed like a word but is not one, and reads as a mistake."""
    vtt = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        " \n"
        "possible<00:00:00.500><c> [music]</c><00:00:01.000><c> mistake.</c>\n"
    )
    assert [word.text for word in subtitles.parse_words(vtt)] == ["possible", "mistake."]
