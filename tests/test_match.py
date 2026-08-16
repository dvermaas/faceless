"""Match selection. The model is stubbed - no Ollama contacted."""

from __future__ import annotations

import pytest

from faceless import match
from faceless.llm import LLMError
from faceless.segments import Segment

from .conftest import make_clip


@pytest.fixture
def segment() -> Segment:
    return Segment(index=0, start=0.0, end=3.0, text="a lion walked into a buffalo herd")


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the model with a scripted reply, and count the calls."""
    calls: list[str] = []

    def install(reply: dict | Exception):
        def fake(prompt, schema, **kwargs):
            calls.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(match, "generate_json", fake)
        return calls

    return install


# -- query building -----------------------------------------------------


def test_query_strips_identifier_style_answers(segment, stub_llm):
    """Small models sometimes answer 'shrimp_heart_location' instead of a phrase."""
    stub_llm({"query": "shrimp_heart_location!", "keywords": ["shrimp_heart"]})
    wanted = match.query_for(segment)
    assert wanted["query"] == "shrimp heart location"
    assert "_" not in " ".join(wanted["keywords"])


def test_query_falls_back_to_narration_when_the_model_fails(segment, stub_llm):
    stub_llm(LLMError("ollama is down"))
    assert match.query_for(segment)["query"] == segment.text


def test_broaden_returns_nothing_when_the_model_fails(stub_llm):
    stub_llm(LLMError("down"))
    assert match.broaden("black panther") == []


# -- scoring ------------------------------------------------------------

def test_normalize_rescales_bm25_to_the_best_hit():
    """REUSE_PENALTY and CLEAR_MARGIN are fractions of 'best available'."""
    scored = [(make_clip(), 8.0), (make_clip(clip_id="b"), 2.0)]
    normalized = match._normalize(scored)
    assert normalized[0][1] == pytest.approx(1.0)
    assert normalized[1][1] == pytest.approx(0.25)


def test_normalize_handles_an_empty_result():
    assert match._normalize([]) == []


# -- selection ----------------------------------------------------------


def test_clear_winner_skips_the_model(segment, stocked_library, stub_llm):
    """The model breaks ties; it must not get to overrule strong evidence.

    It once picked a generic farm clip over an exact hippopotamus match.
    """
    calls = stub_llm({"query": "lion buffalo herd", "keywords": ["lion", "buffalo", "herd"]})
    matches = match.choose([segment], stocked_library)
    assert matches[0].clip.clip_id.startswith("buffalo-herd")
    assert matches[0].reason.startswith("clear winner")
    # One call to build the query, none to rerank - that is the whole point.
    assert len(calls) == 1


def test_every_segment_gets_a_clip_even_with_no_text_match(stocked_library, stub_llm):
    """An unmatched segment shortens the render and truncates the audio."""
    stub_llm({"query": "submarine periscope", "keywords": ["submarine"]})
    segments = [Segment(index=0, start=0.0, end=3.0, text="a submarine surfaced")]
    matches = match.choose(segments, stocked_library)
    assert matches[0].clip is not None
    assert "filler" in matches[0].reason


def test_no_clip_only_when_the_library_is_empty(library, stub_llm):
    stub_llm({"query": "anything", "keywords": []})
    matches = match.choose([Segment(0, 0.0, 3.0, "x")], library)
    assert matches[0].clip is None


def test_reuse_is_penalised_not_forbidden(stocked_library, stub_llm):
    """A hard no-reuse rule let one segment consume the only bear clip.

    With three clips and four segments the fourth has to repeat something rather
    than leave a hole.
    """
    stub_llm({"query": "lion buffalo tiger", "keywords": ["lion", "buffalo", "tiger"]})
    segments = [Segment(index=i, start=i * 3.0, end=i * 3.0 + 3.0, text="x") for i in range(4)]
    matches = match.choose(segments, stocked_library)
    assert all(m.clip is not None for m in matches)
    assert len({m.clip.clip_id for m in matches}) < len(matches)


def test_target_footage_is_never_reused(stocked_library, stub_llm):
    stub_llm({"query": "tiger", "keywords": ["tiger"]})
    matches = match.choose(
        [Segment(0, 0.0, 3.0, "tiger")], stocked_library, exclude_sources={"abc123"}
    )
    assert matches[0].clip is None or matches[0].clip.source_id != "abc123"


def test_provider_filter_restricts_the_pool(stocked_library, stub_llm):
    stub_llm({"query": "tiger jungle", "keywords": ["tiger"]})
    matches = match.choose([Segment(0, 0.0, 3.0, "t")], stocked_library, provider="pexels")
    assert matches[0].clip.provider == "pexels"


def test_broadening_runs_before_giving_up(stocked_library, monkeypatch):
    """FTS cannot connect 'black panther' to a clip described as a leopard."""
    monkeypatch.setattr(
        match, "query_for", lambda segment, model=None: {"query": "black panther", "keywords": []}
    )
    monkeypatch.setattr(match, "broaden", lambda query, model=None: ["tiger"])
    matches = match.choose([Segment(0, 0.0, 3.0, "a panther")], stocked_library)
    assert matches[0].clip.clip_id.startswith("tiger-walking")
    assert "filler" not in matches[0].reason


def test_out_of_range_model_choice_falls_back_to_the_top_hit(stocked_library, monkeypatch):
    monkeypatch.setattr(
        match, "query_for", lambda segment, model=None: {"query": "lion tiger", "keywords": []}
    )
    monkeypatch.setattr(
        match, "generate_json", lambda *a, **k: {"choice": 99, "reason": "nonsense"}
    )
    matches = match.choose([Segment(0, 0.0, 3.0, "x")], stocked_library)
    assert matches[0].clip is not None
    assert "out-of-range" in matches[0].reason
