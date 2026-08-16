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
- **Ollama** serving on `:11434` with `gemma4:12b` - all descriptions, queries,
  and match reranking. No API key, no cloud calls. Override with `--model` /
  `FACELESS_OLLAMA_MODEL` / `FACELESS_OLLAMA_HOST`.

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
| `library.py` | Cuts, names, describes, and indexes clips |
| `llm.py` | Ollama client; schema-constrained JSON only |
| `match.py` | Lexical prefilter + LLM rerank |
| `render.py` | ffmpeg fit/concat/mux |
| `remix.py` | Orchestrates `harvest` and `remix` |
| `cli.py` | argparse wiring for all four subcommands |

### Invariants the design leans on

**Scenes partition the video.** `detect_scenes` returns contiguous ranges
covering `0 → duration`, and `segments.build` preserves that through merging.
This is why a rebuilt video lands on the source duration and the original audio
stays in sync with no drift correction. Breaking contiguity breaks sync.

**`meta.json` is the interchange format.** It uses the same schema as
`find --full` (`search.normalize_entry`), plus `scenes`/`scene_count`. Both
`harvest` and `remix` read it back off disk rather than passing objects around,
which is what makes those commands resumable and independently runnable.

**Library clips inherit their source's narration.** That text - not any image
analysis - is what makes a clip findable later. A clip with no captions has
nothing to match on, so videos without English subtitles are skipped at harvest.

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

**The local model will overrule strong evidence if allowed to.** It once picked a
generic farm clip over an exact hippopotamus match (lexical 0.143 vs 0.500),
reasoning itself out of the obvious answer. `match.CLEAR_MARGIN` therefore only
lets it break ties. Similarly, reuse is a scoring penalty rather than a
prohibition - a hard no-reuse rule let an early segment consume the only bear
clip and left a later bear segment matching a snake.

## Known limitation

Harvested clips are cut from *finished* Shorts, so most carry the source's
burned-in captions - words that now contradict our narration. It is the most
visible flaw in a rendered remix and is inherent to this footage source, not a
bug. Unsolved; see README for the options.

## Conventions

- Typed exceptions per module (`SearchError`, `GrabError`, `SceneDetectionError`,
  `LibraryError`, `LLMError`, `RenderError`), all caught in `cli.main` and
  printed as `faceless: <message>` with a hint for throttling errors.
- Every command supports `--json`; progress goes to **stderr** so stdout stays
  pipeable. `cli._force_utf8` is required - Windows consoles mangle non-ASCII
  video titles otherwise.
- Comments explain *why*, especially where a simpler-looking approach was tried
  and failed. Match the surrounding density.
