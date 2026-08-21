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
uv run pytest                            # unit tests - fast, no network
uv run pytest -m integration             # opt-in checks against real services
uv run pytest tests/test_match.py -k reuse -x   # one module / one test
uv run python -m compileall -q src/faceless     # syntax check
```

**Unit tests never touch the network.** `tests/conftest.py` installs an autouse
fixture that raises on any socket call, so an accidental real request fails
loudly instead of quietly hammering YouTube from a test run. Mock at the
boundary instead: `yt_dlp.YoutubeDL`, `urllib.request.urlopen` (Ollama, Pexels),
`subprocess.run` (ffmpeg). Anything that genuinely needs a live service is
marked `@pytest.mark.integration` and deselected by default; `--strict-markers`
is on, so a typo'd marker is an error rather than a test that quietly runs for
real. There is **no linter configured**.

Tests cover the pure logic and the boundaries. They do not cover whether the
output *looks* right, so the empirical checks below still matter:

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

# caption sync: what parse_words says should be on screen at time T ...
python -c "import sys; sys.path.insert(0,'src'); from faceless.subtitles import parse_words; \
print([w.text for w in parse_words(open('downloads/<id>.en.vtt',encoding='utf-8').read()) \
if w.start <= 21.0 < w.end])"
# ... has to be the word in the frame at T. Sampling an already-burned file is
# just a seek; sample several times across the whole video, not only the opening.
ffmpeg -v error -ss 21.0 -i remixes/<id>.remix.mp4 -frames:v 1 -y f.jpg
```

If you preview a style by applying `subtitles=` as a *filter* instead, put `-ss`
**after** `-i`. Input seeking restarts the filter's clock, so every frame draws
the first word - it looks like total desync and is an artefact of the check.

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
| `subtitles.py` | VTT/SRT parsing; three distinct outputs (see below) |
| `scenes.py` | PySceneDetect wrapper; returns a contiguous shot list |
| `segments.py` | Aligns scenes with captions; merges shots too short to carry a clip |
| `db.py` | SQLite schema, FTS5 index, and query helpers |
| `library.py` | Cuts, names, describes, and indexes clips |
| `pexels.py` | Stock-footage source; API client and slug-derived descriptions |
| `llm.py` | Ollama client; schema-constrained JSON only |
| `match.py` | Lexical prefilter + LLM rerank |
| `captions.py` | Word-by-word ASS captions; libass styling, font checks, width fitting |
| `render.py` | ffmpeg fit/concat/mux, and the caption burn-in |
| `remix.py` | Orchestrates `harvest`, `remix`, and `reset` |
| `cli.py` | argparse wiring for all six subcommands |

Subcommands: `find`, `grab`, `harvest`, `remix`, `library`, `reset`. Tests live
in `tests/`, one module per source module plus `test_harness.py` (which verifies
the no-network guard) and `test_integration.py` (opt-in, real services).

Outputs are `remixes/<id>.remix.mp4`, or `<id>.remix.<source>.mp4` when
`--source` is set, so a filtered run never clobbers an unfiltered one. With
`--captions`, the ASS script is kept beside the video as `<same stem>.ass` -
it is the one output worth hand-editing and burning in again.

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

**`subtitles.py` has three output paths that must not be conflated.**
`to_lines`/`to_text` round to whole seconds and are for human-readable
transcripts; `parse_timed_cues` keeps float bounds and is for aligning captions
to shots; `parse_words` reads YouTube's inline `<00:00:00.560><c>` karaoke
stamps and is for burned-in captions. All three dedupe the rolling
auto-captions, which repeat each line across consecutive cues (~9x size
reduction). Changing one must not change the others.

The first two share `_parse_blocks`, which carries the one-cue-late defect
below. `parse_words` deliberately uses `_parse_blocks_raw` instead, which ends a
cue only on a genuinely empty line, per the WebVTT spec. Both call the same
`_scan` with different `ends_cue` predicates, so the disagreement is visible in
one place rather than duplicated. **A word's time comes from its own stamp, not
from the cue carrying it**, so captions land on the right frame even while the
transcript of the same file is two seconds behind.

**Word timings map onto output time unchanged.** Because segments tile the
source exactly, output time and source time are the same clock - the ASS is
timed against the source and burned onto the rebuild with no offset. If scene
contiguity is ever broken, captions drift along with the audio.

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

**libass never fails on a missing font.** It substitutes another face and says
nothing, so a typo in `--caption-font` produces a finished, wrong-looking video
with no error anywhere. `captions.probe_font` renders one 64x64 frame first and
reads libass's info-level `fontselect: (asked) -> (got)` line; `remix` refuses
before encoding rather than after. This build resolves fonts through
**directwrite, not fontconfig**, so that line reports a family name, not a file
path - there is no font file to read metrics from.

**libass will not break inside a word.** A word wider than the frame does not
wrap; it runs off both edges, which is the worst thing this feature can do. So
`captions.measure_widths` measures every distinct word for real - one ffmpeg
pass drawing each on its own frame of a 4000px canvas, with `cropdetect`
reporting the bounding boxes - and `_fit` scales the over-wide ones down
individually. Estimating from character count cannot work across fonts: Arial
Black renders "COUNTERATTACKED" 32% wider than Impact at the same size. The
measurement degrades to "no scaling" if anything goes wrong, so it costs
fitting, never the render.

**`cropdetect`'s `limit` must be given as an integer here.** The documented 0-1
fractional form parses differently in this build: `limit=0.06` matches the whole
frame and `limit=64` matches nothing at all, while `limit=24` (its own
documented default) is correct. A frame it found nothing in reports a *negative*
width, which would otherwise be read as a measurement.

**Absolute paths inside an ffmpeg filtergraph are a Windows escaping trap** -
`subtitles=C:/path/x.ass` needs the drive colon escaped, and getting it wrong
fails as an unparseable option minute-deep into an encode. `render` sidesteps it
by copying the ASS into its temp dir and running ffmpeg with `cwd` set there, so
the filter sees the bare name `captions.ass`. That is also why the mux resolves
its input and output paths first: relative ones would otherwise land in the temp
directory.

**Burning captions forces a re-encode.** Without them the mux copies the picture
through (`-c:v copy`); with them it has to run x264 again. That is the entire
cost difference between a captioned and an uncaptioned remix.

## State of the project

The pipeline works end to end and has been verified on real videos: a clean
`reset` → `grab` → `harvest --source pexels` → `remix --source pexels` run
produced `remixes/W_mE_LdGA9g.remix.pexels.mp4` at 42.37s against a 42.39s
source, audio bit-for-bit unchanged (mean -17.1 dB in both), all seven segments
matched, and no burned-in captions in any frame.

`remix --captions` has been verified on the same target: 129 words parsed from
the karaoke stamps, output still 42.37s with audio unchanged at -17.1 dB, and
twelve frames sampled across the video each showing the word being spoken at
that moment (the one apparent miss was sampled exactly on a word boundary). The
15-character "counterattacked" scaled to 66% and stayed inside the margins.

What is **not** done, roughly in the order it would pay off:

1. **Captions land about one cue late** - see the open defect below. Most likely
   cause of narration not lining up with the picture. Note this affects
   *matching* only: burned-in captions read the karaoke stamps and are unaffected.
2. **Scene detection misses some cuts** on softer transitions. `--scene-threshold`
   below 27 is the lever; nobody has swept it.
3. **Stock footage is tonally wrong for dramatic narration.** Pexels skews calm:
   a script about a lion being thrown by buffalo gets lions dozing. The subject
   matches, the action never does. No fix attempted.
4. **Captions can only be re-burned by re-running the whole remix.** The ASS is
   kept next to the video and can be edited by hand, but nothing will burn it in
   again without redoing the download, the match and the render. A `faceless
   caption <video> <ass>` subcommand would make iterating on font and size cheap;
   `render._run` already does the work.
5. **Harvest is manual.** `library --gaps` prints exactly what to harvest next,
   but nothing feeds that back into `harvest` automatically.

## Known limitations

**Captions land one cue late (open defect).** `subtitles._parse_blocks` treats a
whitespace-only line as the end of a cue's text. YouTube writes exactly such a
line between the timing line and the karaoke text, so the first cue yields no
text and its words are picked up from the *next* cue instead - roughly two
seconds later than they were spoken. Nothing is lost (rolling captions repeat
each line) but every caption is late, which drags narration one shot behind the
picture and therefore degrades matching. `tests/test_subtitles.py::
test_whitespace_only_line_ends_the_cue_text` pins the current behaviour so the
fix is a deliberate act. The fix is one line - have `_parse_blocks` pass
`_parse_blocks_raw`'s predicate to `_scan`, which already implements the WebVTT
rule that a line containing a space is *not* empty - but it shifts every
transcript timing and changes remix output, so re-verify the end-to-end run
after changing it. Burned-in captions do not depend on this and will not move.

**YouTube-sourced clips carry burned-in captions.** They are cut from *finished*
Shorts, so most arrive with the source's own words on screen, contradicting our
narration. It is the most visible flaw in such a remix and is inherent to that
footage source, not a bug. `remix --source pexels` avoids it entirely (verified:
a side-by-side on the same target had captions on most YouTube shots and none on
the Pexels ones). Removing text after the fact needs per-frame detection plus
inpainting and is not implemented. Note this is the *source's* text in the
footage, and is unrelated to `--captions`, which draws our own narration on top
- combining the two on YouTube-sourced footage stacks two sets of words.

**A word with no karaoke stamp is only evenly spaced.** Manual subtitle tracks
and SRT carry no per-word timing at all, so `parse_words` spreads each cue's
words across the cue. The captions stay in sync at cue granularity but individual
words drift within it. YouTube's auto-captions - the normal case - are stamped
per word and unaffected.

## Conventions

- Typed exceptions per module (`SearchError`, `GrabError`, `SceneDetectionError`,
  `LibraryError`, `LLMError`, `RenderError`, `PexelsError`), all caught in
  `cli.main` and printed as `faceless: <message>`. The hint is matched to the
  service that failed - a Pexels 403 has nothing to do with YouTube cookies.
- `reset` is the only destructive command. It prints what it will delete, asks
  for confirmation, treats an unanswered prompt as "no", takes `--yes` for
  scripting, and reports paths it could not remove rather than claiming success.
  A database viewer attached to `library.db` will block deletion on Windows.
- Every command supports `--json`; progress goes to **stderr** so stdout stays
  pipeable. `cli._force_utf8` is required - Windows consoles mangle non-ASCII
  video titles otherwise.
- Comments explain *why*, especially where a simpler-looking approach was tried
  and failed. Match the surrounding density.
