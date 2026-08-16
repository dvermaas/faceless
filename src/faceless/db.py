"""SQLite storage for the clip library.

Replaces a single `index.json` that was rewritten in full after every harvested
source - 133KB at 181 clips, and a kill mid-write would truncate the collection.
Rows are appended in a transaction instead, so a crash costs the clip in flight
rather than everything.

Three tables:

* `sources` - one row per harvested video, so fifteen clips cut from one Short
  no longer repeat its title, URL and author fifteen times.
* `clips` - one row per file on disk, plus an FTS5 index over the text used to
  find it. BM25 ranking replaces a hand-rolled token-overlap score and does not
  need every clip loaded into memory to run.
* `usages` - every match decision ever made. Not needed to build a video, but it
  is what turns the library into something you can reason about: which clips get
  reused, and which queries never find footage (that list is the harvest plan).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
DB_NAME = "library.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY,
    provider      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    author        TEXT,
    harvested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    clip_key     TEXT NOT NULL UNIQUE,
    path         TEXT NOT NULL UNIQUE,
    scene_index  INTEGER NOT NULL DEFAULT 0,
    start        REAL NOT NULL DEFAULT 0,
    end          REAL NOT NULL DEFAULT 0,
    duration     REAL NOT NULL,
    width        INTEGER,
    height       INTEGER,
    fps          REAL,
    narration    TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    keywords     TEXT DEFAULT '[]',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS clips_source ON clips(source_id);
CREATE INDEX IF NOT EXISTS clips_duration ON clips(duration);

CREATE TABLE IF NOT EXISTS usages (
    id             INTEGER PRIMARY KEY,
    clip_id        INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    target_id      TEXT NOT NULL,
    segment_index  INTEGER NOT NULL,
    query          TEXT,
    score          REAL,
    reason         TEXT,
    used_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS usages_clip ON usages(clip_id);
CREATE INDEX IF NOT EXISTS usages_target ON usages(target_id);

CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
    description, keywords, narration,
    content='clips', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS clips_ai AFTER INSERT ON clips BEGIN
    INSERT INTO clips_fts(rowid, description, keywords, narration)
    VALUES (new.id, new.description, new.keywords, new.narration);
END;

CREATE TRIGGER IF NOT EXISTS clips_ad AFTER DELETE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, description, keywords, narration)
    VALUES ('delete', old.id, old.description, old.keywords, old.narration);
END;

CREATE TRIGGER IF NOT EXISTS clips_au AFTER UPDATE ON clips BEGIN
    INSERT INTO clips_fts(clips_fts, rowid, description, keywords, narration)
    VALUES ('delete', old.id, old.description, old.keywords, old.narration);
    INSERT INTO clips_fts(rowid, description, keywords, narration)
    VALUES (new.id, new.description, new.keywords, new.narration);
END;
"""

# FTS5 treats punctuation as syntax; a caption fragment containing one would
# otherwise raise "fts5: syntax error" mid-harvest. Underscores go too - models
# return snake_case terms like "looking_at_camera", and the unicode61 tokenizer
# would not split them back into searchable words.
_FTS_UNSAFE = re.compile(r"[^a-z0-9\s]", re.IGNORECASE)


def connect(root: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the library database under `root`."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL lets a read (a remix) run while a write (a harvest) is in progress.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(_SCHEMA)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
    return connection


def fts_query(terms: list[str]) -> str:
    """Build an OR query, quoting each term so punctuation cannot break syntax."""
    cleaned = {_FTS_UNSAFE.sub(" ", term).strip().lower() for term in terms}
    words = {word for term in cleaned for word in term.split() if len(word) > 2}
    return " OR ".join(f'"{word}"' for word in sorted(words))


def upsert_source(
    connection: sqlite3.Connection,
    *,
    provider: str,
    external_id: str,
    url: str = "",
    title: str = "",
    author: str = "",
) -> int:
    """Insert a source if new, and return its row id either way."""
    connection.execute(
        """
        INSERT INTO sources (provider, external_id, url, title, author)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (provider, external_id) DO UPDATE SET
            url = excluded.url, title = excluded.title, author = excluded.author
        """,
        (provider, external_id, url, title, author),
    )
    row = connection.execute(
        "SELECT id FROM sources WHERE provider = ? AND external_id = ?",
        (provider, external_id),
    ).fetchone()
    return int(row["id"])


def insert_clip(connection: sqlite3.Connection, source_row: int, clip: dict) -> int:
    cursor = connection.execute(
        """
        INSERT INTO clips (
            source_id, clip_key, path, scene_index, start, end, duration,
            width, height, fps, narration, description, keywords
        ) VALUES (
            :source_id, :clip_key, :path, :scene_index, :start, :end, :duration,
            :width, :height, :fps, :narration, :description, :keywords
        )
        ON CONFLICT (clip_key) DO NOTHING
        """,
        {
            "source_id": source_row,
            "clip_key": clip["clip_key"],
            "path": clip["path"],
            "scene_index": clip.get("scene_index", 0),
            "start": clip.get("start", 0.0),
            "end": clip.get("end", 0.0),
            "duration": clip["duration"],
            "width": clip.get("width"),
            "height": clip.get("height"),
            "fps": clip.get("fps"),
            "narration": clip.get("narration") or "",
            "description": clip.get("description") or "",
            "keywords": json.dumps(clip.get("keywords") or []),
        },
    )
    return int(cursor.lastrowid or 0)


def record_usage(
    connection: sqlite3.Connection,
    *,
    clip_key: str,
    target_id: str,
    segment_index: int,
    query: str,
    score: float,
    reason: str,
) -> None:
    connection.execute(
        """
        INSERT INTO usages (clip_id, target_id, segment_index, query, score, reason)
        SELECT id, ?, ?, ?, ?, ? FROM clips WHERE clip_key = ?
        """,
        (target_id, segment_index, query, score, reason, clip_key),
    )
