"""Shorts detection and URL shaping - all pure, no YouTube contacted."""

from __future__ import annotations

from faceless import search


def test_shorts_url_is_youtubes_own_filter():
    url = search.shorts_search_url("capcut tutorial")
    assert "search_query=capcut+tutorial" in url
    assert f"sp={search.SHORTS_FILTER}" in url


def test_channel_url_is_pointed_at_the_shorts_tab():
    assert search.shorts_tab_url("https://www.youtube.com/@x").endswith("/shorts")
    assert search.shorts_tab_url("https://www.youtube.com/@x/videos").endswith("/shorts")
    # Already there - do not stack another segment on.
    assert search.shorts_tab_url("https://www.youtube.com/@x/shorts").endswith("/shorts")
    assert search.shorts_tab_url("https://www.youtube.com/@x/shorts").count("/shorts") == 1


def test_video_urls_are_left_alone():
    """Only channels have a shorts tab; rewriting a video URL breaks it."""
    for url in (
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://www.youtube.com/shorts/abc",
    ):
        assert search.shorts_tab_url(url) == url


def test_a_shorts_url_proves_it_is_a_short():
    assert search._detect_short({"url": "https://www.youtube.com/shorts/abc"}) is True


def test_portrait_and_short_enough_is_a_short():
    """`webpage_url` rewrites /shorts/ away, so shape has to carry the signal."""
    entry = {"webpage_url": "https://www.youtube.com/watch?v=abc", "width": 1080, "height": 1920, "duration": 40}
    assert search._detect_short(entry) is True


def test_landscape_is_not_a_short_however_brief():
    """Duration alone is the wrong test - a 40s landscape clip is a normal video."""
    entry = {"webpage_url": "https://www.youtube.com/watch?v=abc", "width": 1920, "height": 1080, "duration": 40}
    assert search._detect_short(entry) is False


def test_portrait_but_too_long_is_not_a_short():
    entry = {"webpage_url": "https://www.youtube.com/watch?v=abc", "width": 1080, "height": 1920, "duration": 400}
    assert search._detect_short(entry) is False


def test_machine_translated_caption_tracks_are_dropped():
    """YouTube advertises ~200 translations; only the source track is real."""
    tracks = {
        "en": [{"url": "https://x/api/timedtext?lang=en"}],
        "fr": [{"url": "https://x/api/timedtext?lang=en&tlang=fr"}],
        "de": [{"url": "https://x/api/timedtext?lang=en&tlang=de"}],
    }
    assert search._original_langs(tracks) == ["en"]


def test_all_translated_falls_back_to_listing_everything():
    """Better to report something than to claim a video has no captions."""
    tracks = {"fr": [{"url": "https://x?tlang=fr"}]}
    assert search._original_langs(tracks) == ["fr"]


def test_normalize_entry_canonicalises_the_url():
    entry = {"id": "abc", "url": "https://www.youtube.com/shorts/abc", "title": "t", "duration": 20}
    record = search.normalize_entry(entry)
    assert record["url"] == "https://www.youtube.com/watch?v=abc"
    assert record["is_short"] is True


def test_normalize_entry_full_adds_caption_fields():
    entry = {"id": "abc", "title": "t", "subtitles": {}, "automatic_captions": {}}
    record = search.normalize_entry(entry, full=True)
    assert record["subtitle_langs"] == []
    assert record["has_text"] is False


def test_inherit_fills_channel_from_the_listing():
    """A channel's Shorts tab returns entries with no channel fields of their own."""
    entry = {"id": "abc", "title": "t"}
    merged = search._inherit(entry, {"channel": "MrBeast", "channel_id": "UC1"})
    assert merged["channel"] == "MrBeast"


def test_inherit_never_overwrites_what_the_entry_has():
    entry = {"id": "abc", "channel": "Real Channel"}
    merged = search._inherit(entry, {"channel": "Listing Channel"})
    assert merged["channel"] == "Real Channel"
