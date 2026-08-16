"""Shared yt-dlp plumbing: option building, logging, small formatting helpers."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass


class StderrLogger:
    """Routes yt-dlp messages to stderr so stdout stays parseable JSON.

    Setting a logger makes yt-dlp bypass its own `quiet` handling and funnel all
    screen output through `debug()`, so progress chatter is filtered here.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._write(msg)

    def info(self, msg: str) -> None:
        if self.verbose:
            self._write(msg)

    def warning(self, msg: str) -> None:
        self._write(msg)

    def error(self, msg: str) -> None:
        self._write(msg)

    def _write(self, msg: str) -> None:
        print(msg, file=sys.stderr)


def parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse ``BROWSER[:PROFILE][::CONTAINER]`` into yt-dlp's cookie tuple."""
    browser, _, rest = spec.partition(":")
    profile, _, container = rest.partition("::")
    return (browser.strip().lower(), profile.strip() or None, None, container.strip() or None)


@dataclass(slots=True)
class ClientOptions:
    """Options shared by every command that talks to YouTube."""

    cookies_from_browser: str | None = None
    cookies: str | None = None
    verbose: bool = False

    def base_opts(self) -> dict:
        opts: dict = {
            "quiet": not self.verbose,
            "no_warnings": False,
            "noprogress": True,
            "logger": StderrLogger(self.verbose),
            "verbose": self.verbose,
        }
        if self.cookies_from_browser:
            opts["cookiesfrombrowser"] = parse_browser_spec(self.cookies_from_browser)
        if self.cookies:
            opts["cookiefile"] = self.cookies
        return opts


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def format_duration(seconds: float | int | None) -> str | None:
    """Seconds to ``H:MM:SS`` (or ``M:SS`` under an hour)."""
    if seconds is None:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_count(count: int | None) -> str:
    """Compact view/like counts: 1234567 -> ``1.2M``."""
    if count is None:
        return "?"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= threshold:
            value = count / threshold
            return f"{value:.1f}".rstrip("0").rstrip(".") + suffix
    return str(count)


def format_upload_date(upload_date: str | None) -> str | None:
    """``20240115`` -> ``2024-01-15``."""
    if not upload_date or len(upload_date) != 8 or not upload_date.isdigit():
        return upload_date or None
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
