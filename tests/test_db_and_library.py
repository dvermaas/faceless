"""The SQLite store: schema behaviour, FTS ranking, and back-compatibility."""

from __future__ import annotations

import json

import pytest

from faceless import db
from faceless.library import Clip, Library, slugify

from .conftest import make_clip


# -- FTS query building -------------------------------------------------


def test_fts_query_strips_punctuation():
    """Raw caption text reaching MATCH raises 'fts5: syntax error'."""
    built = db.fts_query(["a camel's stomach!", "water (a lot)"])
    assert "'" not in built and "!" not in built and "(" not in built
    assert '"camel"' in built


def test_fts_query_splits_snake_case():
    """Models answer with terms like looking_at_camera; unicode61 will not split them."""
    built = db.fts_query(["looking_at_camera"])
    assert '"looking"' in built and '"camera"' in built


def test_fts_query_drops_noise_words():
    assert db.fts_query(["a of to"]) == ""


# -- storage ------------------------------------------------------------


def test_clip_round_trips_through_the_database(library):
    library.add(make_clip())
    stored = library.clips[0]
    assert stored.clip_id == "lion-resting__pexels-1"
    assert stored.keywords == ["lion", "grass"]
    assert stored.provider == "pexels"


def test_sources_are_deduplicated(library):
    """Fifteen clips from one video must not create fifteen source rows."""
    for index in range(3):
        library.add(make_clip(clip_id=f"c{index}", path=f"clips/c{index}.mp4", source_id="same"))
    assert library.count() == 3
    assert len(library.source_ids) == 1


def test_adding_the_same_clip_twice_is_a_no_op(library):
    library.add(make_clip())
    library.add(make_clip())
    assert library.count() == 1


def test_has_source_drives_resumable_harvest(library):
    library.add(make_clip(source_id="already"))
    assert library.has_source("already") is True
    assert library.has_source("never") is False


def test_provider_defaults_to_youtube():
    """Index records written before Pexels support must still load."""
    clip = Clip(
        clip_id="x", path="p", source_id="s", source_url="", source_title="",
        channel="", scene_index=0, start=0, end=1, duration=1, text="",
    )
    assert clip.provider == "youtube"
    assert clip.has_burned_in_text is True


# -- search -------------------------------------------------------------


def test_search_ranks_the_better_description_first(stocked_library):
    found = stocked_library.search(["buffalo", "herd"])
    assert found[0][0].clip_id.startswith("buffalo-herd")


def test_search_returns_nothing_for_absent_subjects(stocked_library):
    """An honest empty result is what triggers query broadening upstream."""
    assert stocked_library.search(["helicopter", "runway"]) == []


def test_search_can_be_restricted_to_one_provider(stocked_library):
    found = stocked_library.search(["tiger"], provider="pexels")
    assert found == []
    found = stocked_library.search(["tiger"], provider="youtube")
    assert found and found[0][0].provider == "youtube"


def test_search_excludes_the_video_being_rebuilt(stocked_library):
    """Never recut the target's own footage into its remix."""
    found = stocked_library.search(["tiger"], exclude_sources={"abc123"})
    assert all(clip.source_id != "abc123" for clip, _ in found)


def test_search_matches_narration_as_well_as_description(stocked_library):
    found = stocked_library.search(["hungry", "monkey"])
    assert found and found[0][0].clip_id.startswith("tiger-walking")


# -- usage tracking -----------------------------------------------------


def test_usage_is_recorded_and_counted(stocked_library):
    clip = stocked_library.clips[0]
    stocked_library.record_usage(clip, "target1", 0, "lion", 1.0, "clear winner")
    stats = stocked_library.stats()
    assert stats["clips_used"] == 1
    assert stats["clips_never_used"] == stats["clips"] - 1


def test_fallback_prefers_never_used_clips(stocked_library):
    used = stocked_library.clips[0]
    stocked_library.record_usage(used, "t", 0, "q", 1.0, "r")
    spares = stocked_library.fallback_clips(limit=5)
    assert spares[0].clip_id != used.clip_id


def test_stats_break_down_by_provider(stocked_library):
    assert stocked_library.stats()["by_provider"] == {"pexels": 2, "youtube": 1}


# -- migration ----------------------------------------------------------


def test_legacy_json_index_is_imported(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "index.json").write_text(
        json.dumps({"clips": [make_clip().to_dict()]}), encoding="utf-8"
    )
    assert Library(root).count() == 1


def test_migration_does_not_run_twice(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "index.json").write_text(
        json.dumps({"clips": [make_clip().to_dict()]}), encoding="utf-8"
    )
    Library(root)
    assert Library(root).count() == 1


# -- naming -------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("camel drinking water in desert", "camel-drinking-water-desert"),
        ("A polar bear's skin is black!", "polar-bear-skin-black"),
        ("", "clip"),
        ("   ", "clip"),
    ],
)
def test_slugify_makes_browsable_names(text, expected):
    assert slugify(text) == expected


def test_slugify_respects_the_length_cap():
    assert len(slugify("word " * 40)) <= 48
