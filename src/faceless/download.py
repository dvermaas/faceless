"""`faceless grab` - pull a video, its captions, or captions only."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from yt_dlp import YoutubeDL

from .scenes import DEFAULT_THRESHOLD, detect_scenes
from .search import normalize_entry
from .subtitles import convert_file
from .ytdl import ClientOptions, has_ffmpeg

DEFAULT_TEMPLATE = "%(id)s.%(ext)s"
DEFAULT_LANGS = "en"
_SUB_FORMAT = "vtt/srt/best"
_REGEXY = re.compile(r"[.*?\[\]()^$|+]")


class GrabError(RuntimeError):
    """Raised when there is nothing to save."""


@dataclass(slots=True)
class SubtitleFile:
    lang: str
    path: Path
    auto: bool
    text_path: Path | None = None


@dataclass(slots=True)
class GrabResult:
    id: str | None
    title: str | None
    url: str | None
    duration: int | float | None
    channel: str | None
    video_path: Path | None = None
    meta_path: Path | None = None
    info_json_path: Path | None = None
    subtitles: list[SubtitleFile] = field(default_factory=list)
    scenes: list[dict] | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "duration": self.duration,
            "channel": self.channel,
            "video": str(self.video_path) if self.video_path else None,
            "meta": str(self.meta_path) if self.meta_path else None,
            "info_json": str(self.info_json_path) if self.info_json_path else None,
            "subtitles": [
                {
                    "lang": sub.lang,
                    "path": str(sub.path),
                    "auto": sub.auto,
                    "text": str(sub.text_path) if sub.text_path else None,
                }
                for sub in self.subtitles
            ],
            "scene_count": len(self.scenes) if self.scenes is not None else None,
            "scenes": self.scenes,
        }


def parse_langs(spec: str) -> list[str]:
    """Split a ``--lang`` value into individual requests."""
    return [token.strip() for token in spec.split(",") if token.strip()] or ["en"]


def _is_translation(formats: list[dict] | None) -> bool:
    """YouTube's machine translations carry `tlang=` in every track URL."""
    return bool(formats) and all("tlang=" in (fmt.get("url") or "") for fmt in formats)


def available_tracks(info: dict) -> dict[str, bool]:
    """Map caption language -> is_auto, excluding machine translations."""
    tracks: dict[str, bool] = {}
    for auto, source in ((True, "automatic_captions"), (False, "subtitles")):
        for lang, formats in (info.get(source) or {}).items():
            if lang == "live_chat" or _is_translation(formats):
                continue
            tracks[lang] = auto  # manual wins: subtitles are merged in last
    return tracks


def select_langs(info: dict, requested: list[str]) -> list[str]:
    """Resolve requested languages to one concrete caption track each.

    A bare code like `en` should yield a single transcript, not every English
    variant YouTube offers (`en`, `en-orig`, `en-GB`, ...) - asking for all of
    them produces near-identical files and trips YouTube's rate limiter.
    Patterns and `all` keep the take-everything behaviour.
    """
    tracks = available_tracks(info)
    chosen: list[str] = []
    for token in requested:
        if token == "all":
            chosen.extend(tracks)
            continue
        if _REGEXY.search(token):
            pattern = re.compile(token, re.IGNORECASE)
            chosen.extend(lang for lang in tracks if pattern.fullmatch(lang))
            continue

        lowered = token.lower()
        matches = [
            lang
            for lang in tracks
            if lang.lower() == lowered or lang.lower().startswith(f"{lowered}-")
        ]
        if not matches:
            continue
        # Prefer a human-written track, then the exact code, then the original
        # audio's auto-captions, then whatever regional variant sorts first.
        best = min(
            matches,
            key=lambda lang: (
                tracks[lang],
                lang.lower() != lowered,
                not lang.lower().endswith("-orig"),
                lang,
            ),
        )
        chosen.append(best)

    seen: set[str] = set()
    return [lang for lang in chosen if not (lang in seen or seen.add(lang))]


def _default_format() -> str:
    # Merging separate video/audio streams needs ffmpeg; fall back to a
    # progressive stream when it is missing rather than failing the download.
    return "bv*+ba/b" if has_ffmpeg() else "b[ext=mp4]/b"


def _locate_subtitle(out_dir: Path, video_id: str | None, lang: str, sub: dict) -> Path | None:
    filepath = sub.get("filepath")
    if filepath and Path(filepath).exists():
        return Path(filepath)
    if not video_id:
        return None
    for candidate in sorted(out_dir.glob(f"{video_id}*.{lang}.*")):
        if candidate.suffix.lower() in {".vtt", ".srt", ".ass", ".ttml", ".srv3"}:
            return candidate
    return None


def _single_video(info: dict | None, url: str) -> dict:
    """Reduce an extraction result to one video dict."""
    if info is None:
        raise GrabError(f"nothing extracted from {url}")
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise GrabError(f"nothing extracted from {url}")
        return entries[0]
    return info


def grab(
    url: str,
    *,
    out_dir: Path | str = "downloads",
    text_only: bool = False,
    subs: bool = False,
    langs: str = DEFAULT_LANGS,
    auto_subs: bool = True,
    fmt: str | None = None,
    template: str = DEFAULT_TEMPLATE,
    timestamps: bool = False,
    scenes: bool = False,
    scene_threshold: float = DEFAULT_THRESHOLD,
    write_meta: bool = True,
    write_info_json: bool = False,
    client: ClientOptions | None = None,
) -> GrabResult:
    """Download `url` into `out_dir`.

    `text_only` skips the media entirely and keeps just the caption tracks
    (converted to plain `.txt` alongside the raw `.vtt`).

    `scenes` runs cut detection over the downloaded video and records the shot
    list in the result and the `.meta.json` sidecar.
    """
    client = client or ClientOptions()
    if scenes and text_only:
        raise GrabError("--scenes needs the video; it cannot be combined with --text-only")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    want_subs = subs or text_only

    opts = client.base_opts()
    opts.update(
        {
            "ignoreerrors": False,
            "noplaylist": True,
            "outtmpl": {"default": str(out_dir / template)},
            "writeinfojson": write_info_json,
        }
    )
    if text_only:
        opts["skip_download"] = True
    else:
        opts["format"] = fmt or _default_format()
        opts["merge_output_format"] = "mp4"
    track_kinds: dict[str, bool] = {}
    with YoutubeDL(opts) as ydl:
        if want_subs:
            # Which caption tracks exist is only knowable after extraction, so
            # extract first, choose languages, then download from that same
            # result - one round trip, the way --load-info-json works.
            info = _single_video(ydl.extract_info(url, download=False), url)
            track_kinds = available_tracks(info)
            selected = select_langs(info, parse_langs(langs))
            if not auto_subs:
                selected = [lang for lang in selected if not track_kinds.get(lang, True)]
            if not selected:
                offered = ", ".join(sorted(track_kinds)) or "none"
                raise GrabError(
                    f"no {langs!r} subtitles for {info.get('title') or url} "
                    f"(available: {offered})"
                )
            ydl.params.update(
                {
                    "writesubtitles": True,
                    "writeautomaticsub": auto_subs,
                    "subtitleslangs": selected,
                    "subtitlesformat": _SUB_FORMAT,
                    # Space out requests; YouTube answers caption bursts with 429.
                    "sleep_interval_subtitles": 1 if len(selected) > 1 else 0,
                }
            )
            info = _single_video(ydl.process_ie_result(info, download=True), url)
        else:
            info = _single_video(ydl.extract_info(url, download=True), url)

        # Path the output template resolved to, minus its extension.
        stem = Path(os.path.splitext(ydl.prepare_filename(info))[0])
        info = ydl.sanitize_info(info)

    video_id = info.get("id")
    result = GrabResult(
        id=video_id,
        title=info.get("title"),
        url=info.get("webpage_url") or url,
        duration=info.get("duration"),
        channel=info.get("channel") or info.get("uploader"),
    )

    info_json = info.get("infojson_filename")
    if info_json and Path(info_json).exists():
        result.info_json_path = Path(info_json)

    if not text_only:
        for download in info.get("requested_downloads") or []:
            filepath = download.get("filepath")
            if filepath and Path(filepath).exists():
                result.video_path = Path(filepath)
                break

    if want_subs:
        for lang, sub in (info.get("requested_subtitles") or {}).items():
            path = _locate_subtitle(out_dir, video_id, lang, sub or {})
            if path is None:
                continue
            entry = SubtitleFile(lang=lang, path=path, auto=track_kinds.get(lang, True))
            entry.text_path = convert_file(path, timestamps=timestamps)
            result.subtitles.append(entry)

    if text_only and not result.subtitles:
        raise GrabError(
            f"no {langs} subtitles available for {result.title or url}"
            + ("" if auto_subs else " (auto-generated captions were disabled)")
        )

    if scenes:
        if result.video_path is None:
            raise GrabError("no video file was saved, so there is nothing to detect scenes in")
        result.scenes = detect_scenes(result.video_path, threshold=scene_threshold)

    # Written last so the sidecar can carry the scene list.
    if write_meta:
        # yt-dlp's own .info.json runs to ~1MB per video (every format, every
        # translated caption track). The pipeline only needs the same fields
        # `faceless find --full` returns, in the same shape.
        meta = normalize_entry(info, full=True)
        if result.scenes is not None:
            meta["scene_count"] = len(result.scenes)
            meta["scenes"] = result.scenes
        meta_path = Path(f"{stem}.meta.json")
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        result.meta_path = meta_path

    return result
