"""Command line entry point for faceless."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from yt_dlp.utils import DownloadError

from . import __version__
from .download import DEFAULT_LANGS, DEFAULT_TEMPLATE, GrabError, grab
from .library import Library, LibraryError
from .llm import DEFAULT_MODEL, LLMError
from .pexels import PexelsError
from .remix import SOURCES, harvest, remix
from .render import RenderError
from .scenes import DEFAULT_THRESHOLD, SceneDetectionError
from .search import SearchError, search
from .segments import DEFAULT_MIN_DURATION
from .ytdl import ClientOptions, format_count, format_upload_date


def _client(args: argparse.Namespace) -> ClientOptions:
    return ClientOptions(
        cookies_from_browser=args.cookies_from_browser,
        cookies=args.cookies,
        verbose=args.verbose,
    )


def _print_json(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _print_results(results: list[dict]) -> None:
    for position, item in enumerate(results, start=1):
        facts = [item["channel"] or "unknown channel"]
        if item.get("duration_string"):
            facts.append(item["duration_string"])
        facts.append(f"{format_count(item.get('view_count'))} views")
        uploaded = format_upload_date(item.get("upload_date"))
        if uploaded:
            facts.append(uploaded)
        if item.get("is_short"):
            facts.append("short")
        if item.get("live_status") in {"is_live", "is_upcoming"}:
            facts.append(item["live_status"].replace("_", " "))
        if "has_text" in item:
            tracks = item.get("subtitle_langs") or item.get("auto_caption_langs") or []
            facts.append(f"{len(tracks)} caption tracks" if tracks else "no captions")

        print(f"{position:2}. {item['title'] or '(untitled)'}")
        print(f"    {' · '.join(facts)}")
        print(f"    {item['url']}")
        if position != len(results):
            print()


def cmd_find(args: argparse.Namespace) -> int:
    results = search(
        args.query,
        limit=args.limit,
        full=args.full,
        shorts=args.shorts,
        client=_client(args),
    )
    if args.json:
        _print_json(results)
    else:
        _print_results(results)
    return 0


def cmd_grab(args: argparse.Namespace) -> int:
    result = grab(
        args.url,
        out_dir=args.out,
        text_only=args.text_only,
        subs=args.subs,
        langs=args.lang,
        auto_subs=not args.no_auto_subs,
        fmt=args.format,
        template=args.template,
        timestamps=args.timestamps,
        scenes=args.scenes,
        scene_threshold=args.scene_threshold,
        write_meta=not args.no_meta,
        write_info_json=args.info_json,
        client=_client(args),
    )
    if args.json:
        _print_json(result.to_dict())
        return 0

    print(f"{result.title or result.id}")
    if result.video_path:
        print(f"  video     {result.video_path}")
    for sub in result.subtitles:
        kind = "auto" if sub.auto else "manual"
        print(f"  subs      {sub.path} ({sub.lang}, {kind})")
        if sub.text_path:
            print(f"  text      {sub.text_path}")
    if result.scenes is not None:
        shortest = min(scene["duration"] for scene in result.scenes)
        longest = max(scene["duration"] for scene in result.scenes)
        mean = sum(scene["duration"] for scene in result.scenes) / len(result.scenes)
        print(
            f"  scenes    {len(result.scenes)} cuts "
            f"(shortest {shortest:.1f}s, mean {mean:.1f}s, longest {longest:.1f}s)"
        )
    if result.meta_path:
        print(f"  metadata  {result.meta_path}")
    if result.info_json_path:
        print(f"  info      {result.info_json_path}")
    return 0


def _progress(args: argparse.Namespace):
    """Progress lines go to stderr so --json output stays pipeable."""
    if args.json:
        return None
    return lambda message: print(message, file=sys.stderr)


def cmd_harvest(args: argparse.Namespace) -> int:
    result = harvest(
        args.query,
        source=args.source,
        limit=args.limit,
        library_root=args.library,
        downloads=args.downloads,
        min_duration=args.min_segment,
        model=args.model,
        pexels_key=args.pexels_key,
        client=_client(args),
        on_progress=_progress(args),
    )
    if args.json:
        _print_json(result.to_dict())
        return 0

    sources = len({clip.source_id for clip in result.added})
    print(f"added {len(result.added)} clips from {sources} videos")
    for clip in result.added:
        print(f"  {clip.duration:5.2f}s  {clip.clip_id}")
    if result.skipped:
        print(f"skipped {len(result.skipped)} already-harvested: {', '.join(result.skipped)}")
    for video_id, error in result.failed:
        print(f"failed {video_id}: {error}", file=sys.stderr)
    return 0 if result.added or result.skipped else 1


def cmd_remix(args: argparse.Namespace) -> int:
    result = remix(
        args.url,
        library_root=args.library,
        downloads=args.downloads,
        out_dir=args.out,
        min_duration=args.min_segment,
        model=args.model,
        dry_run=args.dry_run,
        source=args.source,
        client=_client(args),
        on_progress=_progress(args),
    )
    if args.json:
        _print_json(result.to_dict())
        return 0

    print(f"{result.title or result.target_id}")
    for match in result.matches:
        segment = match.segment
        window = f"{segment.start:6.2f}-{segment.end:6.2f}"
        if match.clip is None:
            print(f"  {window}  (no clip)  {match.query[:44]}")
            continue
        print(f"  {window}  {match.query[:38]:38}  ->  {match.clip.clip_id}")
        if args.dry_run:
            print(f"{'':16}score {match.score:.2f}  {match.reason[:70]}")
    if result.credits:
        # The Pexels licence asks for a link back and a videographer credit.
        print("  credits (Pexels licence):")
        for credit in result.credits:
            print(f"    {credit['author']} - {credit['url']}")
    if result.output:
        print(f"  output    {result.output}")
    elif args.dry_run:
        print("  (dry run - nothing rendered)")
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    lib = Library(args.library)

    if args.search:
        found = lib.search(args.search.split(), limit=args.limit, provider=args.provider)
        if args.json:
            _print_json([{"clip": c.to_dict(), "score": round(s, 3)} for c, s in found])
            return 0
        print(f"{len(found)} matches for {args.search!r}")
        for clip, score in found:
            print(f"  {score:5.2f}  {clip.duration:5.2f}s  [{clip.provider:7}]  {clip.clip_id}")
        return 0

    if args.gaps:
        # Queries that fell through to filler are exactly what the library is
        # missing - this list is the next harvest, written by the pipeline.
        rows = lib.connection.execute(
            """
            SELECT query, count(*) AS n, max(used_at) AS last_seen FROM usages
            WHERE score <= 0 OR reason LIKE 'filler%'
            GROUP BY lower(query) ORDER BY n DESC, last_seen DESC LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        if args.json:
            _print_json([dict(row) for row in rows])
            return 0
        if not rows:
            print("no gaps recorded - every segment so far found real footage")
            return 0
        print(f"{len(rows)} queries that found nothing (harvest these):")
        for row in rows:
            print(f"  {row['n']}x  {row['query']}")
        print("\n  faceless harvest \"<query>\" --source pexels -n 3")
        return 0

    if args.unused:
        rows = lib.connection.execute(
            """
            SELECT c.clip_key, c.duration, s.provider FROM clips c
            JOIN sources s ON s.id = c.source_id
            LEFT JOIN usages u ON u.clip_id = c.id
            WHERE u.id IS NULL AND (? IS NULL OR s.provider = ?)
            ORDER BY c.clip_key LIMIT ?
            """,
            (args.provider, args.provider, args.limit),
        ).fetchall()
        if args.json:
            _print_json([dict(row) for row in rows])
            return 0
        print(f"{len(rows)} clips never used in a remix")
        for row in rows:
            print(f"  {row['duration']:5.2f}s  [{row['provider']:7}]  {row['clip_key']}")
        return 0

    stats = lib.stats()
    if args.json:
        _print_json(stats)
        return 0
    print(f"{stats['clips']} clips from {stats['sources']} sources, {stats['total_seconds']:.0f}s")
    for provider, count in sorted(stats["by_provider"].items()):
        print(f"  {provider:8} {count}")
    print(f"  used {stats['clips_used']}, never used {stats['clips_never_used']}")
    top = lib.connection.execute(
        """
        SELECT c.clip_key, count(*) AS n FROM usages u
        JOIN clips c ON c.id = u.clip_id GROUP BY c.id ORDER BY n DESC LIMIT 5
        """
    ).fetchall()
    if top:
        print("  most reused:")
        for row in top:
            print(f"    {row['n']}x  {row['clip_key']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER[:PROFILE]",
        help="load cookies from a local browser (helps when YouTube asks to verify you are human)",
    )
    common.add_argument("--cookies", metavar="FILE", help="netscape-format cookie file")
    common.add_argument("-v", "--verbose", action="store_true", help="show yt-dlp output")

    parser = argparse.ArgumentParser(
        prog="faceless",
        description="AI-in-the-loop pipeline for producing YouTube Shorts.",
    )
    parser.add_argument("--version", action="version", version=f"faceless {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser(
        "find",
        parents=[common],
        help="search YouTube and print matching videos with their metadata",
        description="Search YouTube for videos. QUERY may also be a channel, playlist or video URL.",
    )
    find.add_argument("query", help="search terms, or a YouTube URL to enumerate")
    find.add_argument(
        "-n", "--limit", type=int, default=10, metavar="N", help="number of results (default: 10)"
    )
    find.add_argument("--json", action="store_true", help="emit JSON instead of a readable list")
    find.add_argument(
        "--shorts",
        action="store_true",
        help="return only Shorts, using YouTube's Shorts filter (or a channel's Shorts tab)",
    )
    find.add_argument(
        "--full",
        action="store_true",
        help="resolve each hit for tags, chapters and available caption tracks (slower)",
    )
    find.set_defaults(func=cmd_find)

    grab_cmd = subparsers.add_parser(
        "grab",
        parents=[common],
        help="download a video, and/or its subtitles",
        description="Download a YouTube video into an output directory.",
    )
    grab_cmd.add_argument("url", help="video URL or bare video id")
    grab_cmd.add_argument(
        "-o", "--out", default="downloads", type=Path, metavar="DIR", help="output directory"
    )
    grab_cmd.add_argument(
        "--text-only",
        action="store_true",
        help="skip the media and download only subtitles, converted to plain text",
    )
    grab_cmd.add_argument(
        "--subs", action="store_true", help="download subtitles alongside the video"
    )
    grab_cmd.add_argument(
        "--lang",
        default=DEFAULT_LANGS,
        metavar="CODES",
        help=f"comma-separated subtitle languages (default: {DEFAULT_LANGS}); "
        "bare codes also match regional and auto variants",
    )
    grab_cmd.add_argument(
        "--no-auto-subs", action="store_true", help="ignore auto-generated captions"
    )
    grab_cmd.add_argument(
        "-f", "--format", metavar="SELECTOR", help="yt-dlp format selector (default: best available)"
    )
    grab_cmd.add_argument(
        "--template", default=DEFAULT_TEMPLATE, metavar="TMPL", help="yt-dlp output template"
    )
    grab_cmd.add_argument(
        "--timestamps", action="store_true", help="keep cue timestamps in the plain-text transcript"
    )
    grab_cmd.add_argument(
        "--scenes",
        action="store_true",
        help="detect shot cuts in the downloaded video and record them in the metadata",
    )
    grab_cmd.add_argument(
        "--scene-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="N",
        help=f"scene cut sensitivity, lower finds more cuts (default: {DEFAULT_THRESHOLD:g})",
    )
    grab_cmd.add_argument(
        "--no-meta", action="store_true", help="do not write the .meta.json sidecar"
    )
    grab_cmd.add_argument(
        "--info-json",
        action="store_true",
        help="also write yt-dlp's full .info.json (large: every format and caption track)",
    )
    grab_cmd.add_argument("--json", action="store_true", help="emit JSON describing what was saved")
    grab_cmd.set_defaults(func=cmd_grab)

    pipeline = argparse.ArgumentParser(add_help=False)
    pipeline.add_argument(
        "--library", default="library", type=Path, metavar="DIR", help="clip library directory"
    )
    pipeline.add_argument(
        "--downloads", default="downloads", type=Path, metavar="DIR", help="where sources are kept"
    )
    pipeline.add_argument(
        "--min-segment",
        type=float,
        default=DEFAULT_MIN_DURATION,
        metavar="SEC",
        help=f"merge shots shorter than this (default: {DEFAULT_MIN_DURATION:g}s)",
    )
    pipeline.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help=f"ollama model for descriptions and matching (default: {DEFAULT_MODEL})",
    )

    harvest_cmd = subparsers.add_parser(
        "harvest",
        parents=[common, pipeline],
        help="grow the clip library from YouTube Shorts or Pexels stock footage",
        description=(
            "Grow the clip library. --source youtube finds Shorts, downloads them, and "
            "files every scene as a clip - those carry the source's burned-in captions. "
            "--source pexels fetches clean single-shot stock instead. Both write to the "
            "same library, and already-harvested sources are skipped, so running this "
            "repeatedly keeps growing the collection."
        ),
    )
    harvest_cmd.add_argument(
        "query", help="search terms; a channel/playlist URL also works for --source youtube"
    )
    harvest_cmd.add_argument(
        "--source",
        choices=SOURCES,
        default="youtube",
        help=(
            "where footage comes from: youtube cuts clips out of finished Shorts "
            "(carries their burned-in captions), pexels fetches clean stock "
            "(needs PEXELS_API_KEY). Default: youtube"
        ),
    )
    harvest_cmd.add_argument(
        "-n", "--limit", type=int, default=5, metavar="N", help="videos to harvest (default: 5)"
    )
    harvest_cmd.add_argument(
        "--pexels-key",
        default=None,
        metavar="KEY",
        help="Pexels API key; defaults to the PEXELS_API_KEY environment variable",
    )
    harvest_cmd.add_argument("--json", action="store_true", help="emit JSON describing what was added")
    harvest_cmd.set_defaults(func=cmd_harvest)

    remix_cmd = subparsers.add_parser(
        "remix",
        parents=[common, pipeline],
        help="rebuild a Short using library footage over its original audio",
        description=(
            "Rebuild a video shot for shot: keep its audio, replace every shot with a "
            "library clip matched to what is being said at that moment. Clips from the "
            "video being rebuilt are never reused."
        ),
    )
    remix_cmd.add_argument("url", help="video URL or bare video id to rebuild")
    remix_cmd.add_argument(
        "-o", "--out", default="remixes", type=Path, metavar="DIR", help="output directory"
    )
    remix_cmd.add_argument(
        "--source",
        choices=("any", *SOURCES),
        default="any",
        help=(
            "restrict footage to one source. `--source pexels` is the way to get "
            "a remix with no burned-in captions. Default: any"
        ),
    )
    remix_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print the shot-by-shot plan and render nothing",
    )
    remix_cmd.add_argument("--json", action="store_true", help="emit JSON describing the rebuild")
    remix_cmd.set_defaults(func=cmd_remix)

    library_cmd = subparsers.add_parser(
        "library",
        help="inspect the clip library",
        description=(
            "Show what the library holds, full-text search it, or list clips no "
            "remix has ever used. With no flags, prints a summary."
        ),
    )
    library_cmd.add_argument(
        "--library", default="library", type=Path, metavar="DIR", help="clip library directory"
    )
    library_cmd.add_argument("--search", metavar="TERMS", help="full-text search clip descriptions")
    library_cmd.add_argument(
        "--unused",
        action="store_true",
        help="list clips never chosen by a remix - the ones earning nothing",
    )
    library_cmd.add_argument(
        "--gaps",
        action="store_true",
        help="list queries that found no footage - what to harvest next",
    )
    library_cmd.add_argument("--provider", choices=SOURCES, help="restrict to one source")
    library_cmd.add_argument("-n", "--limit", type=int, default=20, metavar="N", help="row limit")
    library_cmd.add_argument("--json", action="store_true", help="emit JSON")
    library_cmd.set_defaults(func=cmd_library)

    return parser


def _force_utf8() -> None:
    # Windows consoles default to a legacy code page, which mangles (or crashes on)
    # non-ASCII video titles and JSON payloads.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        SearchError,
        GrabError,
        SceneDetectionError,
        LibraryError,
        PexelsError,
        LLMError,
        RenderError,
        DownloadError,
    ) as exc:
        message = str(exc)
        print(f"faceless: {message}", file=sys.stderr)
        # Match the hint to the service that actually failed - a Pexels 403 has
        # nothing to do with YouTube cookies.
        if isinstance(exc, PexelsError):
            if "429" in message:
                print(
                    "hint: Pexels allows 200 requests/hour. Wait, or request a higher "
                    "limit at https://www.pexels.com/api/",
                    file=sys.stderr,
                )
        elif "429" in message or "403" in message or "Sign in to confirm" in message:
            print(
                "hint: YouTube is throttling this client. Wait a minute, or pass "
                "--cookies-from-browser chrome to authenticate.",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        print("faceless: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
