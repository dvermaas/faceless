"""Opt-in checks against the real services.

Deselected by default. Run them when you want to confirm the environment is set
up, or when a downstream API may have changed under us:

    uv run pytest -m integration

Each one skips rather than fails when its prerequisite is missing, so a partial
setup gives a useful report instead of a wall of red.
"""

from __future__ import annotations

import os
import shutil

import pytest

from faceless import llm, pexels, search

pytestmark = pytest.mark.integration


def test_ffmpeg_is_on_path():
    assert shutil.which("ffmpeg"), "ffmpeg is required for harvest and remix"
    assert shutil.which("ffprobe"), "ffprobe is required to measure clips"


def test_ollama_serves_the_configured_model():
    try:
        installed = llm.available_models()
    except llm.LLMError as exc:
        pytest.skip(f"ollama not reachable: {exc}")
    assert llm.default_model() in installed, (
        f"{llm.default_model()} is not installed; pull it or set FACELESS_OLLAMA_MODEL"
    )


def test_ollama_returns_schema_constrained_json():
    """Constrained decoding is what the whole matching path depends on."""
    try:
        reply = llm.generate_json(
            "Name one animal. Respond as JSON.",
            {"type": "object", "properties": {"animal": {"type": "string"}}, "required": ["animal"]},
        )
    except llm.LLMError as exc:
        pytest.skip(f"ollama not reachable: {exc}")
    assert isinstance(reply.get("animal"), str) and reply["animal"]


def test_pexels_search_returns_portrait_video():
    if not os.environ.get("PEXELS_API_KEY"):
        pytest.skip("PEXELS_API_KEY is not set")
    videos = pexels.search("hippopotamus", limit=2)
    assert videos, "Pexels returned nothing for a common subject"
    for video in videos:
        assert video.download_url.startswith("http")
        assert video.description, "the URL slug should always yield a description"
        assert video.height >= video.width, "orientation=portrait was requested"


def test_youtube_shorts_search_still_uses_the_expected_filter():
    """If YouTube changes the Shorts filter, --shorts silently returns long-form."""
    results = search.search("capcut tutorial", limit=5, shorts=True)
    assert results, "the Shorts filter returned nothing - it may have changed"
    assert all(item["is_short"] for item in results)
