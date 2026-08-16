"""Rendering and reset. ffmpeg is stubbed - no encoding actually runs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from faceless import render, remix
from faceless.match import Match
from faceless.render import RenderError
from faceless.segments import Segment

from .conftest import make_clip


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """Record ffmpeg invocations and create whatever output they name."""
    commands: list[list[str]] = []

    def fake_run(command, capture_output=False, text=False, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="4.0\n", stderr="")
        target = command[-1]
        if target.endswith((".mp4", ".txt")):
            path = tmp_path / target if not str(target).startswith(str(tmp_path)) else target
            try:
                open(target, "wb").write(b"fake")
            except OSError:
                pass
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    return commands


def _match(index: int, duration: float, clip=None) -> Match:
    return Match(
        segment=Segment(index=index, start=index * duration, end=(index + 1) * duration, text="x"),
        clip=clip if clip is not None else make_clip(),
        query="q",
        score=1.0,
        reason="r",
    )


def test_render_refuses_when_a_segment_has_no_clip(tmp_path, fake_ffmpeg):
    """Skipping unmatched segments produced 34.7s of video over 42.7s of audio."""
    matches = [_match(0, 3.0), Match(Segment(1, 3.0, 6.0, "x"), None, "q", 0.0, "none")]
    with pytest.raises(RenderError, match="no clip"):
        render.render(matches, tmp_path / "audio.mp4", tmp_path / "out.mp4", library_root=tmp_path)


def test_render_refuses_with_nothing_matched(tmp_path, fake_ffmpeg):
    matches = [Match(Segment(0, 0.0, 3.0, "x"), None, "q", 0.0, "none")]
    with pytest.raises(RenderError, match="nothing to render"):
        render.render(matches, tmp_path / "audio.mp4", tmp_path / "out.mp4", library_root=tmp_path)


def test_render_reports_a_missing_clip_file(tmp_path, fake_ffmpeg):
    with pytest.raises(RenderError, match="missing from disk"):
        render.render(
            [_match(0, 3.0)], tmp_path / "audio.mp4", tmp_path / "out.mp4", library_root=tmp_path
        )


def test_each_segment_is_cut_to_its_exact_duration(tmp_path, fake_ffmpeg):
    clip_path = tmp_path / "clips" / "lion-resting__pexels-1.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")

    render.render(
        [_match(0, 2.5), _match(1, 2.5)],
        tmp_path / "audio.mp4",
        tmp_path / "out.mp4",
        library_root=tmp_path,
    )
    fits = [c for c in fake_ffmpeg if "-t" in c]
    assert len(fits) == 2
    for command in fits:
        assert command[command.index("-t") + 1] == "2.500"


def test_a_short_clip_is_looped_to_fill_the_segment(tmp_path, fake_ffmpeg):
    """ffprobe is stubbed at 4s, so a 10s segment needs repeats."""
    clip_path = tmp_path / "clips" / "lion-resting__pexels-1.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")

    render.render(
        [_match(0, 10.0)], tmp_path / "audio.mp4", tmp_path / "out.mp4", library_root=tmp_path
    )
    fit = next(c for c in fake_ffmpeg if "-stream_loop" in c)
    assert int(fit[fit.index("-stream_loop") + 1]) >= 1


def test_original_audio_is_mapped_from_the_source(tmp_path, fake_ffmpeg):
    clip_path = tmp_path / "clips" / "lion-resting__pexels-1.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake")

    render.render(
        [_match(0, 3.0)], tmp_path / "audio.mp4", tmp_path / "out.mp4", library_root=tmp_path
    )
    mux = fake_ffmpeg[-1]
    assert "0:v:0" in mux and "1:a:0" in mux
    assert "copy" in mux  # picture is copied, never re-encoded at the mux


# -- reset --------------------------------------------------------------


def test_inspect_measures_without_deleting(tmp_path):
    (tmp_path / "downloads").mkdir()
    (tmp_path / "downloads" / "a.mp4").write_bytes(b"0123456789")
    targets = remix.inspect_reset({"downloads": tmp_path / "downloads"})
    assert targets[0].files == 1
    assert targets[0].bytes == 10
    assert (tmp_path / "downloads" / "a.mp4").exists()


def test_absent_directories_are_ignored(tmp_path):
    assert remix.inspect_reset({"library": tmp_path / "nope"}) == []


def test_reset_removes_the_directory(tmp_path):
    root = tmp_path / "library"
    (root / "clips").mkdir(parents=True)
    (root / "clips" / "a.mp4").write_bytes(b"x")
    targets = remix.inspect_reset({"library": root})
    assert remix.reset(targets) == []
    assert not root.exists()


def test_reset_refuses_a_filesystem_root(tmp_path):
    """A mistyped --library must not be able to walk off the project."""
    target = remix.ResetTarget(name="library", path=Path(tmp_path.anchor), files=0, bytes=0)
    with pytest.raises(remix.LibraryError, match="refusing"):
        remix.reset([target])


def test_reset_reports_paths_it_could_not_remove(tmp_path, monkeypatch):
    """A destructive command that lies about what it deleted is the worst case."""
    root = tmp_path / "library"
    root.mkdir()

    def boom(path):
        raise PermissionError(13, "in use by another process")

    monkeypatch.setattr(remix.shutil, "rmtree", boom)
    failed = remix.reset(remix.inspect_reset({"library": root}))
    assert failed and "in use" in failed[0]
