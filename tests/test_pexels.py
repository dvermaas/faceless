"""Pexels client. HTTP is stubbed - no requests leave the machine."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from faceless import pexels


@pytest.fixture
def api_response():
    return {
        "videos": [
            {
                "id": 35472256,
                "url": "https://www.pexels.com/video/majestic-hippopotamus-in-african-waterhole-35472256/",
                "duration": 33,
                "width": 2160,
                "height": 3840,
                "tags": [],
                "user": {"name": "Someone", "url": "https://www.pexels.com/@someone"},
                "video_files": [
                    {"link": "https://x/360.mp4", "quality": None, "width": 360, "height": 640},
                    {"link": "https://x/1080.mp4", "quality": None, "width": 1080, "height": 1920},
                    {"link": "https://x/4k.mp4", "quality": None, "width": 2160, "height": 3840},
                ],
            }
        ]
    }


@pytest.fixture
def stub_http(monkeypatch):
    def install(payload, status=200):
        def fake_urlopen(request, timeout=None):
            if status != 200:
                raise urllib.error.HTTPError(
                    request.full_url, status, "err", {}, io.BytesIO(b"nope")
                )
            captured["headers"] = dict(request.headers)
            return io.BytesIO(json.dumps(payload).encode())

        captured: dict = {}
        monkeypatch.setattr(pexels.urllib.request, "urlopen", fake_urlopen)
        return captured

    return install


# -- description from the URL slug --------------------------------------


def test_description_comes_from_the_url_slug():
    """`tags` comes back empty in practice; the slug is the real description."""
    description, keywords = pexels.describe_from_url(
        "https://www.pexels.com/video/majestic-hippopotamus-in-african-waterhole-35472256/"
    )
    assert description == "majestic hippopotamus in african waterhole"
    assert "hippopotamus" in keywords
    assert "35472256" not in description


def test_slug_without_a_trailing_id_still_parses():
    description, _ = pexels.describe_from_url("https://www.pexels.com/video/lone-wolf/")
    assert description == "lone wolf"


# -- rendition choice ---------------------------------------------------


def test_rendition_is_chosen_by_dimensions_not_quality(api_response):
    """The documented `quality` field comes back null, so it cannot be used."""
    chosen = pexels._pick_file(api_response["videos"][0]["video_files"])
    assert chosen["width"] == 1080 and chosen["height"] == 1920


def test_falls_back_to_the_largest_when_nothing_covers_the_target():
    files = [
        {"link": "a", "width": 360, "height": 640},
        {"link": "b", "width": 540, "height": 960},
    ]
    assert pexels._pick_file(files)["width"] == 540


def test_no_usable_file_returns_none():
    assert pexels._pick_file([{"quality": "hd"}]) is None


# -- search -------------------------------------------------------------


def test_search_sends_the_key_and_a_real_user_agent(api_response, stub_http):
    """Pexels 403s urllib's default agent even when the key is valid."""
    captured = stub_http(api_response)
    pexels.search("hippo", key="secret")
    headers = {name.lower(): value for name, value in captured["headers"].items()}
    assert headers["authorization"] == "secret"
    assert "python-urllib" not in headers["user-agent"].lower()


def test_search_maps_the_response(api_response, stub_http):
    stub_http(api_response)
    videos = pexels.search("hippo", key="k")
    assert len(videos) == 1
    video = videos[0]
    assert video.description == "majestic hippopotamus in african waterhole"
    assert video.download_url == "https://x/1080.mp4"
    assert video.author == "Someone"


def test_short_videos_are_skipped(api_response, stub_http):
    api_response["videos"][0]["duration"] = 1
    stub_http(api_response)
    assert pexels.search("hippo", key="k", min_duration=2.0) == []


@pytest.mark.parametrize(
    "status, expected", [(401, "PEXELS_API_KEY"), (403, "PEXELS_API_KEY"), (429, "rate limit")]
)
def test_http_errors_become_readable_messages(api_response, stub_http, status, expected):
    stub_http(api_response, status=status)
    with pytest.raises(pexels.PexelsError) as excinfo:
        pexels.search("hippo", key="k")
    assert expected in str(excinfo.value)


def test_missing_key_is_reported_before_any_request(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(pexels.PexelsError, match="PEXELS_API_KEY"):
        pexels.api_key(None)


def test_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "from-env")
    assert pexels.api_key(None) == "from-env"
