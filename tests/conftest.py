"""Shared fixtures, and a hard stop on network access.

Every downstream service this project talks to - YouTube, Pexels, Ollama - is
mocked at its own boundary. The `no_network` fixture below is the backstop: if a
test ever reaches a real socket, it fails loudly instead of quietly hammering
someone else's API from a test run.
"""

from __future__ import annotations

import socket

import pytest

from faceless.library import Clip, Library


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Fail any test that opens a socket, unless it is marked `integration`."""
    if request.node.get_closest_marker("integration"):
        return

    def guard(*args, **kwargs):
        raise RuntimeError(
            "network access attempted in a unit test - mock the boundary "
            "(yt_dlp.YoutubeDL, urllib.request.urlopen, subprocess.run) instead"
        )

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def rolling_vtt() -> str:
    """Auto-captions in YouTube's rolling style: each cue repeats the last line.

    Also carries the inline `<c>` karaoke tags and a near-zero-duration cue that
    real caption files contain.
    """
    return (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "00:00:00.080 --> 00:00:02.110 align:start position:0%\n"
        " \n"
        "a<00:00:00.240><c> camel</c><00:00:00.640><c> can</c>\n"
        "\n"
        "00:00:02.110 --> 00:00:02.120 align:start position:0%\n"
        "a camel can\n"
        " \n"
        "\n"
        "00:00:02.120 --> 00:00:04.070 align:start position:0%\n"
        "a camel can\n"
        "drink<00:00:02.500><c> a</c><00:00:02.900><c> lot</c>\n"
        "\n"
        "00:00:04.070 --> 00:00:06.500 align:start position:0%\n"
        "drink a lot\n"
        "of&nbsp;water today\n"
    )


@pytest.fixture
def scenes() -> list[dict]:
    """A contiguous shot list, including two shots below the merge floor."""
    return [
        {"index": 0, "start": 0.0, "end": 3.0, "duration": 3.0},
        {"index": 1, "start": 3.0, "end": 3.8, "duration": 0.8},   # under 1.5s
        {"index": 2, "start": 3.8, "end": 6.5, "duration": 2.7},
        {"index": 3, "start": 6.5, "end": 7.2, "duration": 0.7},   # under 1.5s
        {"index": 4, "start": 7.2, "end": 10.0, "duration": 2.8},
    ]


def make_clip(**overrides) -> Clip:
    """A library clip with sane defaults; override only what a test cares about."""
    fields = {
        "clip_id": "lion-resting__pexels-1",
        "path": "clips/lion-resting__pexels-1.mp4",
        "source_id": "pexels-1",
        "source_url": "https://www.pexels.com/video/lion-resting-1/",
        "source_title": "lion resting",
        "channel": "Someone",
        "scene_index": 0,
        "start": 0.0,
        "end": 4.0,
        "duration": 4.0,
        "text": "",
        "visual_description": "lion resting in the grass",
        "keywords": ["lion", "grass"],
        "provider": "pexels",
    }
    fields.update(overrides)
    return Clip(**fields)


@pytest.fixture
def library(tmp_path) -> Library:
    """An empty library rooted in a temp directory."""
    return Library(tmp_path / "library")


@pytest.fixture
def stocked_library(library) -> Library:
    """A library with a handful of clips across both providers."""
    library.add(make_clip())
    library.add(
        make_clip(
            clip_id="buffalo-herd__pexels-2",
            path="clips/buffalo-herd__pexels-2.mp4",
            source_id="pexels-2",
            visual_description="herd of water buffalo in a field",
            keywords=["buffalo", "herd", "field"],
            duration=6.0,
        )
    )
    library.add(
        make_clip(
            clip_id="tiger-walking__abc123_04",
            path="clips/tiger-walking__abc123_04.mp4",
            source_id="abc123",
            source_url="https://www.youtube.com/watch?v=abc123",
            provider="youtube",
            visual_description="tiger walking through undergrowth",
            keywords=["tiger", "jungle"],
            text="a hungry tiger targeted a monkey",
            duration=2.0,
        )
    )
    return library
