"""Pick a library clip for each segment of the video being rebuilt.

Two stages. A lexical pass scores every candidate cheaply and keeps a shortlist;
the local model then chooses among the shortlist. The split matters because the
library grows without bound - scoring hundreds of clips through a model per
segment would dominate the runtime, and scoring them by word overlap alone picks
the wrong one whenever the words differ but the meaning does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .library import Clip
from .llm import LLMError, generate_json
from .segments import Segment

SHORTLIST = 8
# How much worse a clip must score before reusing one is preferred over a fresh
# but unrelated clip. Roughly "one strong keyword hit".
REUSE_PENALTY = 0.35
# Lead over the runner-up that makes a candidate an outright winner. The model
# is there to break ties between comparable clips, not to overrule clear
# evidence - left to rerank freely it will talk itself out of an exact subject
# match ("not a specific hippopotamus, but a farm with many species...").
CLEAR_MARGIN = 0.15
_STOPWORDS = frozenset(
    "a an the of and or to in on at for with from is are was were be been being this "
    "that these those it its as by number one two three four five six seven eight nine "
    "ten you your they their them can could will would so but not have has had do does".split()
)

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query", "keywords"],
}

_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["choice", "reason"],
}


@dataclass(slots=True)
class Match:
    segment: Segment
    clip: Clip | None
    query: str
    score: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "segment": self.segment.to_dict(),
            "clip_id": self.clip.clip_id if self.clip else None,
            "clip_path": self.clip.path if self.clip else None,
            "clip_description": self.clip.visual_description if self.clip else None,
            "clip_source": self.clip.source_id if self.clip else None,
            "query": self.query,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


def tokens(*parts: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", " ".join(parts).lower())
    return {word for word in words if len(word) > 2 and word not in _STOPWORDS}


def query_for(segment: Segment, *, model: str | None = None) -> dict:
    """Ask what should be on screen while this narration plays."""
    prompt = (
        "You are choosing stock footage for a short video.\n"
        f"During this shot the narrator says: {segment.text!r}\n\n"
        "Describe what should be VISIBLE on screen - the subject and setting, not the words. "
        "Use 3-8 plain words, no punctuation, e.g. 'owl turning its head'. "
        "Also give 3-6 single-word search keywords.\n"
        "Respond as JSON."
    )
    try:
        reply = generate_json(prompt, _QUERY_SCHEMA, model=model)
    except LLMError:
        # Falling back to the raw narration still matches on shared nouns, which
        # is worse than a real query but far better than skipping the segment.
        return {"query": segment.text, "keywords": []}
    # Small models sometimes answer with an identifier ("shrimp_heart_location")
    # rather than a phrase; punctuation carries no meaning here either way.
    query = re.sub(r"[^\w\s]|_", " ", str(reply.get("query") or "")).strip()
    query = re.sub(r"\s+", " ", query) or segment.text
    keywords = [
        re.sub(r"[^\w\s]|_", " ", str(word)).strip().lower()
        for word in reply.get("keywords") or []
        if str(word).strip()
    ]
    return {"query": query, "keywords": [word for word in keywords if word][:8]}


def lexical_score(want: set[str], clip: Clip) -> float:
    """Overlap between the wanted visuals and what a clip is known to show.

    The clip's own description is weighted above its narration: the description
    is about the picture, the narration only correlates with it.
    """
    if not want:
        return 0.0
    described = tokens(clip.visual_description, " ".join(clip.keywords))
    narrated = tokens(clip.text)
    return (2.0 * len(want & described) + len(want & narrated)) / (2.0 * len(want))


def _rerank(query: str, candidates: list[tuple[Clip, float]], *, model: str | None) -> tuple[int, str]:
    listing = "\n".join(
        f"{index}. {clip.visual_description or clip.text[:60] or 'unknown'}"
        for index, (clip, _) in enumerate(candidates)
    )
    prompt = (
        f"We need footage showing: {query!r}\n\n"
        f"Available clips:\n{listing}\n\n"
        "Choose the number of the clip that best shows the wanted subject. "
        "If none match well, choose the closest. Respond as JSON with the number and a short reason."
    )
    try:
        reply = generate_json(prompt, _CHOICE_SCHEMA, model=model)
    except LLMError as exc:
        return 0, f"lexical top pick ({exc.__class__.__name__})"
    choice = reply.get("choice")
    reason = str(reply.get("reason") or "").strip()
    if not isinstance(choice, int) or not 0 <= choice < len(candidates):
        return 0, "lexical top pick (model returned an out-of-range choice)"
    return choice, reason


def choose(
    segments: list[Segment],
    clips: list[Clip],
    *,
    exclude_sources: set[str] = frozenset(),
    model: str | None = None,
    allow_reuse: bool = True,
) -> list[Match]:
    """Match every segment to a clip, in order."""
    pool = [clip for clip in clips if clip.source_id not in exclude_sources]
    matches: list[Match] = []
    used: set[str] = set()

    for segment in segments:
        wanted = query_for(segment, model=model)
        want = tokens(wanted["query"], " ".join(wanted["keywords"]))

        available = pool if allow_reuse else [clip for clip in pool if clip.clip_id not in used]
        if not available:
            matches.append(Match(segment, None, wanted["query"], 0.0, "no clip available"))
            continue

        # Repeating a clip is a cost, not a prohibition. Excluding used clips
        # outright means an early segment can consume the only bear shot and
        # leave a later "bears hibernate" segment matching a snake.
        scored = sorted(
            (
                (clip, lexical_score(want, clip) - (REUSE_PENALTY if clip.clip_id in used else 0.0))
                for clip in available
            ),
            key=lambda pair: (pair[1], pair[0].duration >= segment.duration),
            reverse=True,
        )
        shortlist = scored[:SHORTLIST]
        if len(shortlist) == 1:
            index, reason = 0, "only candidate"
        elif shortlist[0][1] - shortlist[1][1] >= CLEAR_MARGIN:
            index, reason = 0, f"clear lexical winner ({shortlist[0][1]:.2f})"
        else:
            index, reason = _rerank(wanted["query"], shortlist, model=model)
        clip, score = shortlist[index]
        used.add(clip.clip_id)
        matches.append(Match(segment, clip, wanted["query"], score, reason))

    return matches
