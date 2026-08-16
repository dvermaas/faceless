"""Segment building - where the audio-sync invariant is established."""

from __future__ import annotations

import pytest

from faceless.segments import build
from faceless.subtitles import TimedCue


def test_segments_tile_the_source_exactly(scenes):
    """Contiguity is why a rebuilt video lands on the source duration.

    If segments ever stop covering [0, duration] the render comes up short and
    ffmpeg's -shortest silently truncates the narration.
    """
    segments = build(scenes, [])
    assert segments[0].start == 0.0
    assert segments[-1].end == scenes[-1]["end"]
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.end == pytest.approx(later.start)


def test_short_scenes_are_merged_away(scenes):
    segments = build(scenes, [], min_duration=1.5)
    assert len(segments) < len(scenes)
    assert all(segment.duration >= 1.5 for segment in segments)


def test_merging_preserves_total_duration(scenes):
    total = sum(scene["duration"] for scene in scenes)
    segments = build(scenes, [], min_duration=1.5)
    assert sum(segment.duration for segment in segments) == pytest.approx(total)


def test_merge_prefers_the_shorter_neighbour():
    """Folding blindly into the previous shot grows one segment past the rest."""
    scenes = [
        {"index": 0, "start": 0.0, "end": 8.0, "duration": 8.0},
        {"index": 1, "start": 8.0, "end": 8.9, "duration": 0.9},
        {"index": 2, "start": 8.9, "end": 11.0, "duration": 2.1},
    ]
    segments = build(scenes, [], min_duration=1.5)
    assert len(segments) == 2
    # The 0.9s shot joined the 2.1s neighbour, not the 8s one.
    assert segments[0].duration == pytest.approx(8.0)
    assert segments[1].duration == pytest.approx(3.0)


def test_cues_land_in_the_scene_they_overlap_most(scenes):
    cues = [
        TimedCue(0.5, 2.5, "first shot line"),
        TimedCue(8.0, 9.5, "last shot line"),
    ]
    segments = build(scenes, cues, min_duration=1.5)
    assert "first shot line" in segments[0].text
    assert "last shot line" in segments[-1].text


def test_degenerate_cue_still_gets_a_home(scenes):
    """Rolling captions emit ~10ms cues that overlap nothing measurable."""
    cues = [TimedCue(4.100, 4.110, "blink and you miss it")]
    segments = build(scenes, cues, min_duration=1.5)
    assert any("blink and you miss it" in segment.text for segment in segments)


def test_merged_text_reads_in_playback_order():
    scenes = [
        {"index": 0, "start": 0.0, "end": 0.8, "duration": 0.8},
        {"index": 1, "start": 0.8, "end": 3.0, "duration": 2.2},
    ]
    cues = [TimedCue(0.1, 0.7, "first"), TimedCue(1.0, 2.5, "second")]
    segments = build(scenes, cues, min_duration=1.5)
    assert segments[0].text == "first second"


def test_no_scenes_means_no_segments():
    assert build([], []) == []


def test_single_short_scene_survives_alone():
    """Merging must not delete the only segment there is."""
    scenes = [{"index": 0, "start": 0.0, "end": 0.5, "duration": 0.5}]
    segments = build(scenes, [], min_duration=1.5)
    assert len(segments) == 1
    assert segments[0].duration == pytest.approx(0.5)
