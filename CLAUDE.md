# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-in-the-loop pipeline for producing YouTube Shorts. It discovers Shorts,
downloads them with transcripts and a shot list, cuts every shot into a reusable
clip library, and rebuilds a target Short from that library over its original
audio.

## Commands

```sh
uv sync                                  # install; run after touching pyproject.toml
uv run faceless <subcommand> ...         # run the CLI
uv run python -m compileall -q src/faceless   # syntax check
```

There is **no test suite and no linter configured**. Verification in this project
is empirical: run the CLI against a real video and inspect the artifacts. The
checks that have actually caught bugs here:

```sh
# scene/segment sanity - contiguity and coverage are the invariants that matter
uv run faceless grab "<url>" --subs --scenes
python -c "import json; d=json.load(open('downloads/<id>.meta.json')); print(d['scene_count'], d['scenes'][-1]['end'], d['duration'])"

# matching quality, without rendering or re-downloading
uv run faceless remix "<url>" --dry-run

# render correctness: output duration must equal source duration
ffprobe -v error -show_entries format=duration -of csv=p=0 remixes/<id>.remix.mp4

# audio must be untouched - compare against the source
ffmpeg -hide_banner -i <file> -af volumedetect -f null NUL 2>&1 | grep volume
```

**Library checks:**

```sh
uv run faceless library          # counts, providers, reuse
uv run faceless library --gaps   # what to harvest next
```

**Look at the video.** Numeric checks pass on output that is visually wrong.
Extracting a frame per segment into a contact sheet (`ffmpeg ... -filter_complex
tile=5x3`) and viewing it is what exposed both the scene-detection accuracy and
the burned-in-caption problem. Note `-pattern_type glob` is unsupported on this
Windows ffmpeg build; use numbered files and `-i 'f_%03d.jpg'`.

## External dependencies

Not Python packages, and all three are load-bearing:

- **ffmpeg** on PATH - clip cutting, fitting, concat, muxing. Hard requirement
  for `harvest`/`remix`.
- **A JavaScript runtime** (deno/node/bun) - drives `yt-dlp-ejs`. Without it
  YouTube media URLs return `403`.
- **Ollama** serving on `:11434` with `gemma4:12b` - descriptions, queries, and
  match reranking. No API key, no cloud calls. Override with `--model` /
  `FACELESS_OLLAMA_MODEL` / `FACELESS_OLLAMA_HOST`. Not needed for
  `harvest --source pexels`.
- **`PEXELS_API_KEY`** (env var or `--pexels-key`) - only for
  `harvest --source pexels`. Never write it to a file in the repo.

## Architecture

Data flows one way, and each stage writes an artifact the next stage reads:

```
find ──► grab ──► meta.json + .vtt + .mp4 ──► harvest ──► library/ ──► remix ──► remixes/
                    (scenes)   (captions)      (cut+describe)  (clips+index)  (match+render)
```

| Module | Responsibility |
|---|---|
| `search.py` | YouTube discovery; Shorts filtering and detection |
| `download.py` | yt-dlp orchestration; writes the `.meta.json` sidecar |
| `subtitles.py` | VTT/SRT parsing; two distinct outputs (see below) |
| `scenes.py` | PySceneDetect wrapper; returns a contiguous shot list |
| `segments.py` | Aligns scenes with captions; merges shots too short to carry a clip |
| `db.py` | SQLite schema, FTS5 index, and query helpers |
| `library.py` | Cuts, names, describes, and indexes clips |
| `pexels.py` | Stock-footage source; API client and slug-derived descriptions |
| `llm.py` | Ollama client; schema-constrained JSON only |
| `match.py` | Lexical prefilter + LLM rerank |
| `render.py` | ffmpeg fit/concat/mux |
| `remix.py` | Orchestrates `harvest` and `remix` |
| `cli.py` | argparse wiring for all five subcommands |

### Invariants the design leans on

**Scenes partition the video.** `detect_scenes` returns contiguous ranges
covering `0 → duration`, and `segments.build` preserves that through merging.
This is why a rebuilt video lands on the source duration and the original audio
stays in sync with no drift correction. Breaking contiguity breaks sync.

**Every segment must end up with a clip.** Because the shot list tiles the source
exactly, an unmatched segment shortens the picture and ffmpeg's `-shortest` then
silently truncates the narration - a render came out 34.7s against a 42.7s source
before this was caught. `match.choose` falls back to a least-used clip rather
than leaving a hole, and `render.render` refuses outright if any segment is still
unmatched. Do not "simplify" either by skipping unmatched segments.

**Search shortlists in SQLite, not in Python.** `Library.search` ranks with
FTS5/BM25 over description (weight 3), keywords (2) and narration (1), so
matching cost does not grow with the library. BM25 is unbounded and
corpus-relative, so `match._normalize` rescales each result set to 0-1 - the
`REUSE_PENALTY` and `CLEAR_MARGIN` constants are fractions of "best available for
this segment" and are meaningless against raw BM25. Terms must go through
`db.fts_query`, which strips punctuation and underscores; raw caption text passed
to `MATCH` raises "fts5: syntax error".

**`meta.json` is the interchange format.** It uses the same schema as
`find --full` (`search.normalize_entry`), plus `scenes`/`scene_count`. Both
`harvest` and `remix` read it back off disk rather than passing objects around,
which is what makes those commands resumable and independently runnable.

**Library clips inherit their source's narration.** That text - not any image
analysis - is what makes a clip findable later. A clip with no captions has
nothing to match on, so YouTube videos without English subtitles are skipped at
harvest.

**Two clip sources share one library and one index.** `harvest --source youtube`
splits finished Shorts along their cuts and needs a model call per clip to infer
a description; `harvest --source pexels` files whole single-shot stock clips and
needs no model at all, because Pexels writes a human description into every
video URL slug (its `tags` field comes back empty - do not reach for it).
`Clip.provider` records which, defaults to `"youtube"` so pre-Pexels index files
still load, and drives `remix --source`. Both paths normalize identically
(1080x1920, 30fps, no audio) so `render.py` never needs to care.

**`subtitles.py` has two output paths that must not be conflated.**
`to_lines`/`to_text` round to whole seconds and are for human-readable
transcripts; `parse_timed_cues` keeps float bounds and is for aligning captions
to shots. Both dedupe YouTube's rolling auto-captions, which repeat each line
across consecutive cues (~9x size reduction). Changing one must not change the
other.

## Non-obvious constraints, learned the hard way

Each of these cost real debugging time; none are guessable from the code.

**YouTube throttles hard.** Repeated requests to the same video, or a batch loop,
returns `403`/`429` intermittently - it is not a code bug and retrying the same
command often just works. `harvest` paces between videos and treats a failed
download as one lost source, not a lost run. `--cookies-from-browser chrome`
is the escape hatch.

**The Shorts search filter has thin yield on some topics.** `sp=EgIQCQ%3D%3D` is
YouTube's own Shorts filter (read off the filter menu, type=9), but "animal
facts" returns 2 results while "capcut tutorial" returns 8. **Channel URLs are
the reliable harvest source.** Some channels expose neither a shorts tab nor a
videos tab to yt-dlp - use the `channel_url` from a grab's metadata, never a
guessed `@handle`.

**`webpage_url` destroys the Shorts signal**, rewriting `/shorts/ID` to
`watch?v=ID`. Shorts are therefore also detected by shape: portrait *and* ≤3
minutes. Duration alone is wrong - a 40s landscape video is not a Short.

**PySceneDetect closes the final scene on the last frame**, leaving one-frame
fragments that are not shots. `_merge_fragments` folds anything under
`min_scene_len` into its neighbour. Decoding, not detection, dominates runtime -
the `pyav` backend is ~4x faster than OpenCV with identical results, which is why
`scenedetect[pyav]` is a hard dependency.

**Constrained decoding is flaky at volume.** Roughly 1 call in 20 truncates
mid-string - invisible in one test, fatal across the hundreds a harvest makes.
`generate_json` retries with nudged sampling; callers degrade rather than abort.

**Cold model loads take minutes.** The first Ollama call pages ~8GB from disk;
subsequent calls answer in ~1s. Both commands call `llm.warm()` up front so the
wait happens once and visibly. Timeouts are sized for this.

**`usages` is what makes the library inspectable.** Recorded on real renders only
(a dry run is exploration - counting it would inflate "most reused"). Its payoff
is `faceless library --gaps`: queries that fell through to filler are exactly the
footage the library lacks, so the pipeline writes its own harvest plan.

**Lexical search cannot bridge vocabulary gaps** - "black panther" shares no
words with "leopard resting in natural habitat" even though the footage fits.
`match.broaden` spends one model call on related subjects and re-searches before
giving up. This is the one place the model is clearly better than the index.

**The local model will overrule strong evidence if allowed to.** It once picked a
generic farm clip over an exact hippopotamus match (lexical 0.143 vs 0.500),
reasoning itself out of the obvious answer. `match.CLEAR_MARGIN` therefore only
lets it break ties. Similarly, reuse is a scoring penalty rather than a
prohibition - a hard no-reuse rule let an early segment consume the only bear
clip and left a later bear segment matching a snake.

**Pexels blocks urllib's default User-Agent with a 403** even when the key is
valid - `pexels.USER_AGENT` exists for that reason. Its documented `quality`
field also comes back `null` in practice, so renditions are selected by
dimensions.

## Known limitation

YouTube-sourced clips are cut from *finished* Shorts, so most carry the source's
burned-in captions - words that now contradict our narration. It is the most
visible flaw in such a remix and is inherent to that footage source, not a bug.
`remix --source pexels` avoids it entirely (verified: a side-by-side on the same
target had captions on most YouTube shots and none on the Pexels ones). Removing
text after the fact needs per-frame detection plus inpainting and is not
implemented.

## Conventions

- Typed exceptions per module (`SearchError`, `GrabError`, `SceneDetectionError`,
  `LibraryError`, `LLMError`, `RenderError`), all caught in `cli.main` and
  printed as `faceless: <message>` with a hint for throttling errors.
- Every command supports `--json`; progress goes to **stderr** so stdout stays
  pipeable. `cli._force_utf8` is required - Windows consoles mangle non-ASCII
  video titles otherwise.
- Comments explain *why*, especially where a simpler-looking approach was tried
  and failed. Match the surrounding density.
