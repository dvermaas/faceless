"""`faceless find` - query YouTube and normalize the metadata yt-dlp gives back."""

from __future__ import annotations

from urllib.parse import quote_plus

from yt_dlp import YoutubeDL

from .ytdl import ClientOptions, format_duration

# YouTube's own "Shorts" search filter, read out of the filter menu on the
# results page. The `sp` value is a base64'd protobuf; this one sets the type
# field to 9. Using it means YouTube returns only Shorts rather than us pulling
# down pages of long-form videos and discarding them.
SHORTS_FILTER = "EgIQCQ%3D%3D"

# Shorts have been capped at 3 minutes since late 2024.
SHORT_MAX_DURATION = 180

_CHANNEL_TABS = frozenset(
    {"videos", "shorts", "streams", "playlists", "featured", "community", "about", "live"}
)


class SearchError(RuntimeError):
    """Raised when a query returns nothing usable."""


def _is_url(query: str) -> bool:
    return query.startswith(("http://", "https://", "www.", "ytsearch"))


def shorts_search_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={SHORTS_FILTER}"


def shorts_tab_url(url: str) -> str:
    """Point a channel URL at its Shorts tab, leaving anything else alone."""
    base = url.rstrip("/")
    tail = base.rsplit("/", 1)[-1].lower()
    if tail == "shorts":
        return base
    if any(marker in base for marker in ("/watch", "/shorts/", "youtu.be/", "list=", "/results")):
        return url
    if tail in _CHANNEL_TABS:
        return f"{base.rsplit('/', 1)[0]}/shorts"
    return f"{base}/shorts"


def _canonical_url(entry: dict, video_id: str | None) -> str | None:
    """Always hand back the `watch?v=` form so downstream stages get one shape."""
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return entry.get("webpage_url") or entry.get("url")


def _detect_short(entry: dict) -> bool:
    """Is this a Short?

    A `/shorts/` URL is proof, but only flat search entries keep one -
    `webpage_url` rewrites it to `watch?v=`. Full extractions are identified by
    shape instead: Shorts are portrait and at most three minutes long, which no
    ordinary landscape upload is.
    """
    for candidate in (entry.get("original_url"), entry.get("url"), entry.get("webpage_url")):
        if candidate and "/shorts/" in candidate:
            return True
    width, height, duration = entry.get("width"), entry.get("height"), entry.get("duration")
    if width and height and duration is not None:
        return height > width and duration <= SHORT_MAX_DURATION
    return False


def _best_thumbnail(entry: dict) -> str | None:
    thumb = entry.get("thumbnail")
    if thumb:
        return thumb
    thumbnails = entry.get("thumbnails") or []
    if not thumbnails:
        return None
    best = max(
        thumbnails,
        key=lambda t: (t.get("preference") or 0, t.get("width") or 0, t.get("height") or 0),
    )
    return best.get("url")


def _original_langs(tracks: dict) -> list[str]:
    """Drop YouTube's machine-translated caption tracks.

    Auto-captions are advertised in ~200 languages, all translated from one
    source track. Translations carry `tlang=` in their URL; only the source
    track does not, and that is the one worth knowing about.
    """
    langs = [
        lang
        for lang, formats in tracks.items()
        if any("tlang=" not in (fmt.get("url") or "") for fmt in formats or [])
    ]
    return sorted(langs) if langs else sorted(tracks)


_PARENT_FIELDS = (
    "channel",
    "channel_id",
    "channel_url",
    "channel_follower_count",
    "uploader",
    "uploader_id",
    "uploader_url",
)


def _inherit(entry: dict, parent: dict) -> dict:
    """Fill gaps from the containing playlist.

    A channel's Shorts tab returns lockup renderers that carry no channel
    fields of their own, so entries would otherwise come back with no uploader
    even though we know exactly whose channel we asked for.
    """
    if not parent:
        return entry
    merged = dict(entry)
    for key in _PARENT_FIELDS:
        if parent.get(key) and not merged.get(key):
            merged[key] = parent[key]
    return merged


def normalize_entry(entry: dict, *, full: bool = False) -> dict:
    """Flatten a yt-dlp entry into the stable shape the rest of the pipeline consumes."""
    video_id = entry.get("id")
    url = _canonical_url(entry, video_id)
    duration = entry.get("duration")

    result = {
        "id": video_id,
        "title": entry.get("title"),
        "url": url,
        "duration": duration,
        "duration_string": entry.get("duration_string") or format_duration(duration),
        "channel": entry.get("channel") or entry.get("uploader"),
        "channel_id": entry.get("channel_id") or entry.get("uploader_id"),
        "channel_url": entry.get("channel_url") or entry.get("uploader_url"),
        "channel_follower_count": entry.get("channel_follower_count"),
        "view_count": entry.get("view_count"),
        "like_count": entry.get("like_count"),
        "comment_count": entry.get("comment_count"),
        "upload_date": entry.get("upload_date"),
        "timestamp": entry.get("timestamp"),
        "description": entry.get("description"),
        "thumbnail": _best_thumbnail(entry),
        "live_status": entry.get("live_status"),
        "availability": entry.get("availability"),
        "age_limit": entry.get("age_limit"),
        "language": entry.get("language"),
        "is_short": _detect_short(entry),
    }

    if full:
        subtitles = entry.get("subtitles") or {}
        captions = entry.get("automatic_captions") or {}
        result.update(
            {
                "tags": entry.get("tags") or [],
                "categories": entry.get("categories") or [],
                "chapters": entry.get("chapters") or [],
                "subtitle_langs": _original_langs(subtitles),
                "auto_caption_langs": _original_langs(captions),
                "has_text": bool(subtitles or captions),
            }
        )
    return result


def search(
    query: str,
    *,
    limit: int = 10,
    full: bool = False,
    shorts: bool = False,
    client: ClientOptions | None = None,
) -> list[dict]:
    """Search YouTube for `query`, or list entries when `query` is a URL.

    With `full`, every hit is resolved individually - slower, but adds tags,
    chapters and (importantly for `grab --text-only`) which caption tracks exist.

    With `shorts`, the query is aimed at a Shorts-only surface: YouTube's Shorts
    search filter for search terms, or the channel's Shorts tab for a channel URL.
    """
    client = client or ClientOptions()
    opts = client.base_opts()
    opts.update(
        {
            "skip_download": True,
            "extract_flat": False if full else "in_playlist",
            "ignoreerrors": True,
            "playlistend": limit,
            "noplaylist": False,
        }
    )

    if _is_url(query):
        target = shorts_tab_url(query) if shorts else query
    elif shorts:
        target = shorts_search_url(query)
    else:
        target = f"ytsearch{limit}:{query}"

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
        if info is None:
            raise SearchError(f"no results for {query!r}")
        info = ydl.sanitize_info(info)

    entries = info.get("entries")
    if entries is None:
        entries = [info]

    parent = {key: info.get(key) for key in _PARENT_FIELDS}
    results = [normalize_entry(_inherit(entry, parent), full=full) for entry in entries if entry]
    if shorts:
        # The filter above should make this a no-op; it is here so that
        # `--shorts` is a guarantee rather than a request.
        results = [item for item in results if item["is_short"]]
    if not results:
        raise SearchError(f"no {'shorts ' if shorts else ''}results for {query!r}")
    return results[:limit]
