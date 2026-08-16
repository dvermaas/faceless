"""Pexels stock footage as a clip source.

The YouTube path cuts clips out of finished Shorts, which means inheriting
whatever those creators burned into the picture - captions, logos, watermarks.
Pexels supplies clean single-shot footage instead: no on-screen text, no
competing narration, and already shot in portrait when asked for it.

Licensing: the Pexels licence allows free use including commercially, but asks
for a visible link back to Pexels and credit to the videographer. Every clip
keeps its page URL and author so `faceless remix` can print those.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_ROOT = "https://api.pexels.com/v1"
TARGET_WIDTH, TARGET_HEIGHT = 1080, 1920
DEFAULT_TIMEOUT = 120
# 200 requests/hour by default. One search covers a whole harvest, so the real
# traffic is the file downloads, which are not rate limited.
PER_PAGE_MAX = 80


class PexelsError(RuntimeError):
    """Raised when Pexels cannot be reached or refuses the request."""


@dataclass(slots=True)
class PexelsVideo:
    id: int
    url: str
    duration: float
    width: int
    height: int
    author: str
    author_url: str
    download_url: str
    description: str
    keywords: list[str] = field(default_factory=list)


def api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("PEXELS_API_KEY", "")
    if not key:
        raise PexelsError(
            "no Pexels API key. Set PEXELS_API_KEY or pass --pexels-key. "
            "Keys are free at https://www.pexels.com/api/"
        )
    return key


def describe_from_url(url: str) -> tuple[str, list[str]]:
    """Turn a Pexels page URL into a description and keywords.

    Pexels writes a human slug into every video URL
    (`.../video/majestic-hippopotamus-in-african-waterhole-35472256`), which
    describes the picture better than the `tags` field - that comes back empty.
    No model call needed: this text is already what we would ask one to produce.
    """
    slug = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d+$", "", slug)  # trailing id
    words = [word for word in slug.split("-") if word]
    description = " ".join(words)
    keywords = [word for word in words if len(word) > 2][:8]
    return description, keywords


def _pick_file(video_files: list[dict]) -> dict | None:
    """Choose the rendition closest to 1080x1920 without overshooting into 4K.

    The documented `quality` field comes back null in practice, so selection is
    by dimensions: the smallest rendition that still covers the target, falling
    back to the largest available when nothing does.
    """
    usable = [f for f in video_files if f.get("link") and f.get("height")]
    if not usable:
        return None
    covering = [f for f in usable if f["height"] >= TARGET_HEIGHT and f.get("width", 0) >= TARGET_WIDTH]
    if covering:
        return min(covering, key=lambda f: f["height"] * f.get("width", 0))
    return max(usable, key=lambda f: f["height"] * f.get("width", 0))


USER_AGENT = "faceless/0.1"


def _get(path: str, params: dict, key: str) -> dict:
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode(params)}"
    # Pexels blocks urllib's default `Python-urllib/x.y` agent with a 403, so a
    # real User-Agent is required even though the key is valid.
    request = urllib.request.Request(
        url, headers={"Authorization": key, "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PexelsError(
                f"Pexels rejected the request ({exc.code}) - check PEXELS_API_KEY"
            ) from exc
        if exc.code == 429:
            raise PexelsError("Pexels rate limit reached (429) - 200 requests/hour") from exc
        raise PexelsError(f"Pexels returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise PexelsError(f"cannot reach Pexels ({exc.reason})") from exc


def search(
    query: str,
    *,
    limit: int = 10,
    key: str | None = None,
    orientation: str = "portrait",
    min_duration: float = 2.0,
) -> list[PexelsVideo]:
    """Search Pexels for portrait video matching `query`."""
    resolved = api_key(key)
    payload = _get(
        "/videos/search",
        {
            "query": query,
            "orientation": orientation,
            "per_page": min(max(limit, 1), PER_PAGE_MAX),
        },
        resolved,
    )
    videos: list[PexelsVideo] = []
    for item in payload.get("videos") or []:
        chosen = _pick_file(item.get("video_files") or [])
        if not chosen:
            continue
        duration = float(item.get("duration") or 0)
        if duration < min_duration:
            continue
        page_url = item.get("url") or ""
        description, keywords = describe_from_url(page_url)
        user = item.get("user") or {}
        videos.append(
            PexelsVideo(
                id=int(item.get("id") or 0),
                url=page_url,
                duration=duration,
                width=int(chosen.get("width") or 0),
                height=int(chosen.get("height") or 0),
                author=str(user.get("name") or "unknown"),
                author_url=str(user.get("url") or ""),
                download_url=chosen["link"],
                description=description,
                keywords=keywords,
            )
        )
        if len(videos) >= limit:
            break
    return videos


def download(video: PexelsVideo, target: Path) -> Path:
    """Fetch the chosen rendition to disk."""
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(video.download_url, headers={"User-Agent": "faceless/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response, target.open("wb") as handle:
            while chunk := response.read(1 << 16):
                handle.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise PexelsError(f"failed downloading Pexels video {video.id}: {exc}") from exc
    return target
