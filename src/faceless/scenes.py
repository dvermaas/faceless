"""Shot/cut detection over a downloaded video.

Faceless Shorts are cut together from stock footage, so the cut list is the
natural unit of edit: it says where a clip can be trimmed without slicing
through a shot, and which segment each line of narration belongs to.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_THRESHOLD = 27.0
DEFAULT_MIN_SCENE_LEN = 15  # frames; 0.5s at 30fps
_BACKENDS = ("pyav", "opencv")


class SceneDetectionError(RuntimeError):
    """Raised when a video cannot be opened or analysed."""


def _open_video(path: Path):
    """Open with the fastest backend available, falling back to OpenCV."""
    from scenedetect import open_video

    last_error: Exception | None = None
    for backend in _BACKENDS:
        try:
            return open_video(str(path), backend=backend)
        except Exception as exc:  # noqa: BLE001 - any backend failure is worth retrying
            last_error = exc
    raise SceneDetectionError(f"could not open {path}: {last_error}")


def _merge_fragments(scene_list: list, min_scene_len: int) -> list:
    """Fold sub-minimum scenes into their neighbour.

    `min_scene_len` governs the gap between detected cuts, but the final scene
    is always closed on the last frame - so a cut landing near the end leaves a
    one-frame fragment that is not a shot at all. Merging rather than dropping
    keeps the list contiguous and covering the whole video.
    """
    merged: list = []
    for start, end in scene_list:
        if merged and (end.frame_num - start.frame_num) < min_scene_len:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    # A fragment in first position has no predecessor, so it merges forward.
    if len(merged) > 1 and (merged[0][1].frame_num - merged[0][0].frame_num) < min_scene_len:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


def _scene(index: int, start, end) -> dict:
    return {
        "index": index,
        "start": round(start.seconds, 3),
        "end": round(end.seconds, 3),
        "duration": round(end.seconds - start.seconds, 3),
        "start_timecode": start.get_timecode(),
        "end_timecode": end.get_timecode(),
        "start_frame": start.frame_num,
        "end_frame": end.frame_num,
    }


def detect_scenes(
    path: Path | str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_scene_len: int = DEFAULT_MIN_SCENE_LEN,
) -> list[dict]:
    """Return the cut list for `path` as plain dicts.

    A video with no detected cuts yields one scene spanning the whole thing,
    rather than an empty list, so callers can treat the result uniformly.
    """
    # Imported lazily: pulling in scenedetect drags OpenCV along with it, and
    # `faceless find` has no reason to pay for that.
    from scenedetect import ContentDetector, SceneManager

    path = Path(path)
    if not path.exists():
        raise SceneDetectionError(f"no such video: {path}")

    video = _open_video(path)
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=min_scene_len))
    try:
        manager.detect_scenes(video, show_progress=False)
        scene_list = manager.get_scene_list()
    except Exception as exc:  # noqa: BLE001 - surface decoder errors as our own type
        raise SceneDetectionError(f"scene detection failed for {path}: {exc}") from exc

    if not scene_list:
        return [_scene(0, video.base_timecode, video.duration)]
    scene_list = _merge_fragments(scene_list, min_scene_len)
    return [_scene(index, start, end) for index, (start, end) in enumerate(scene_list)]
