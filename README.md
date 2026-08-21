# faceless

An AI-in-the-loop pipeline for producing YouTube Shorts. It finds Shorts, pulls
down their media and transcripts, detects every cut, files each shot as a
reusable clip, and rebuilds a target Short from that library over its original
audio, optionally with word-by-word captions burned over the top.

```
find ──► grab ──► harvest ──► remix
 discover  download   build the    rebuild a Short
           + cut list  clip library  from the library
```

Everything runs locally: YouTube via yt-dlp, and a local Ollama model for
descriptions and matching. No API keys.

## Setup

```sh
uv sync
```

Three external tools are expected, none of them Python packages:

- **ffmpeg** on `PATH` - merges video/audio streams, and does all the clip
  cutting, fitting and muxing. `harvest` and `remix` cannot run without it.
- **a JavaScript runtime** (deno, node or bun) - drives `yt-dlp-ejs`, which
  solves YouTube's JS challenges. Without it, media URLs come back `403`.
- **Ollama** serving on `localhost:11434` with `gemma4:12b` pulled - writes clip
  descriptions and drives matching. `find`, `grab`, and `harvest --source pexels`
  work without it.

```sh
ollama pull gemma4:12b
```

For Pexels harvesting, set a free API key from <https://www.pexels.com/api/>:

```sh
export PEXELS_API_KEY=...        # or pass --pexels-key
```

The key is only ever read from the environment or the flag — nothing writes it
to disk, so it cannot end up in a commit.

Output lands in `downloads/` (sources), `library/` (clips), and `remixes/`
(finished videos). All three are gitignored - they are large and reproducible.

## Tests

```sh
uv run pytest                  # ~140 unit tests, about a second, no network
uv run pytest -m integration   # opt-in: checks ffmpeg, Ollama, Pexels and YouTube
```

Unit tests mock every downstream service and a fixture fails the run if anything
opens a socket, so the suite never sends traffic to YouTube or Pexels. The
integration tests do talk to them; they are deselected by default and skip
individually when a prerequisite (a key, a model, a binary) is missing.

## Commands

### `faceless find <query>`

Searches YouTube and prints what it finds. `<query>` may also be a video,
channel or playlist URL, in which case its entries are listed instead.

```sh
faceless find "python asyncio" -n 5
faceless find "python asyncio" --json          # machine-readable
faceless find "python asyncio" --full --json   # + tags, chapters, caption tracks
faceless find "capcut tutorial" --shorts       # Shorts only
faceless find "https://youtube.com/@MrBeast" --shorts   # that channel's Shorts
```

`--full` resolves every hit individually. It is slower, but adds the fields you
need to decide whether a video is usable - notably `subtitle_langs` and
`auto_caption_langs`, which tell you whether `grab --text-only` will work.
Machine-translated caption tracks are filtered out; YouTube advertises ~200 of
them per video and none carry new information.

### Targeting Shorts

Plain search returns almost no Shorts - the same query that gives 14-minute and
2-hour tutorials returns 11-to-35 second Shorts once filtered. `--shorts` asks
YouTube for Shorts directly rather than over-fetching and discarding:

- a **search term** goes through YouTube's own Shorts search filter
  (`sp=EgIQCQ%3D%3D`, read from the filter menu on the results page)
- a **channel URL** is redirected to that channel's `/shorts` tab

Either way YouTube does the filtering, so we pull one page of Shorts instead of
pages of long-form videos. Results are then checked a second time locally, so
`--shorts` is a guarantee rather than a request.

Identifying a Short is less obvious than it looks. A `/shorts/` URL is proof,
but only flat search entries keep one - `webpage_url` rewrites it back to
`watch?v=`, so `--full` would lose the signal. Shorts are therefore also
recognised by shape: portrait orientation and at most three minutes (YouTube's
cap since late 2024). Duration alone is not enough - a 40-second landscape
upload is an ordinary video, not a Short.

Entries from a channel's Shorts tab carry no channel fields of their own, so
they inherit them from the channel being listed.

### `faceless grab <url>`

Downloads a video into `-o/--out` (default `downloads/`), named by video id.

```sh
faceless grab "https://youtu.be/VIDEO_ID"                 # video only
faceless grab "https://youtu.be/VIDEO_ID" --subs          # video + captions
faceless grab "https://youtu.be/VIDEO_ID" --text-only     # captions only
faceless grab "https://youtu.be/VIDEO_ID" --text-only --timestamps
```

`--text-only` skips the media entirely. Alongside the raw `.vtt` it writes a
cleaned `.txt` transcript: inline karaoke tags removed, HTML entities decoded,
and YouTube's rolling auto-caption duplicates collapsed. That last part matters
- a 1h42m tutorial goes from an 849KB `.vtt` to a 91KB transcript saying the
same thing. `--timestamps` keeps a `[HH:MM:SS]` prefix per line, which is what
you want when the next stage has to pick a clip out of a long video.

`--lang` takes comma-separated codes and resolves each to **one** track,
preferring human-written captions over auto-generated ones, and an exact code
match over a regional variant. Asking for `en` therefore yields a single
transcript rather than near-identical copies of `en`, `en-orig` and `en-GB`.
Pass a regex (`en.*`) or `all` to take everything.

Each grab also writes a `<id>.meta.json` sidecar using the same schema as
`find --full`. Use `--info-json` if you want yt-dlp's raw dump as well; it runs
to about 1MB per video, since it includes every format and caption track.

### Scene detection

`--scenes` runs PySceneDetect over the downloaded video and records the shot
list under `scene_count` and `scenes` in both the returned JSON and the
`.meta.json` sidecar. It needs the video, so it cannot be combined with
`--text-only`.

```sh
faceless grab "https://youtu.be/VIDEO_ID" --scenes
faceless grab "https://youtu.be/VIDEO_ID" --scenes --scene-threshold 20
```

Each scene carries `start`/`end`/`duration` in seconds, `start_timecode`/
`end_timecode`, and `start_frame`/`end_frame`. Scenes are contiguous and cover
the whole video, so they can be treated as a partition rather than a set of
markers.

These videos are cut together from stock footage, and the cuts are hard, so
detection is unusually reliable: thresholds of 20, 27 and 35 all found the same
boundaries on the test Short. `--scene-threshold` is there for footage with
softer transitions - lower finds more cuts.

Two details worth knowing:

- **Decoding dominates the runtime**, not the detection itself, which is why
  the `pyav` extra is a hard dependency: it cut a 43-second Short from ~16s to
  ~3.6s with byte-identical results. Downscaling, by contrast, bought almost
  nothing.
- **PySceneDetect always closes the final scene on the last frame**, so a cut
  landing near the end leaves a one-frame fragment that is not a shot. Anything
  under the minimum scene length is merged into its neighbour.

## Building videos

The last two commands turn the pipeline back on itself: harvested Shorts become
the footage library that rebuilt Shorts are made from.

### `faceless harvest <query|url>`

Grows the clip library. Two sources, chosen with `--source`, writing into the
same library and the same index:

```sh
faceless harvest "https://www.youtube.com/channel/UC..." -n 10   # --source youtube (default)
faceless harvest "owl" --source pexels -n 5
```

| | `--source youtube` | `--source pexels` |
|---|---|---|
| Footage | scenes cut from finished Shorts | clean single-shot stock |
| Burned-in captions | **yes** - inherited from the source | none |
| Clip descriptions | inferred by the local model from narration | read from the Pexels URL slug |
| Needs Ollama | yes | no |
| Needs captions | yes - videos without them are skipped | no |
| Cost | slow (download + detect + a model call per clip) | fast (one API call, then downloads) |
| Limits | YouTube throttles; harvest paces itself | 200 requests/hour |

Use YouTube when you want footage in the visual style of real Shorts, and Pexels
when you want clean pictures — see the caption limitation below.

Each clip is named for what it shows, so the library is skimmable from the shell:

```
library/clips/
  camel-drinking-water-desert__O3zowUqJSCc_04.mp4
  snake-shedding-skin-forest__O3zowUqJSCc_11.mp4
  spider-weaving-web__O3zowUqJSCc_13.mp4
```

The description behind that name is written by a local model from the narration
spoken over the clip, and stored in `library/index.json` alongside the source
video, timings, and keywords. Harvest is **resumable** — already-harvested
videos are skipped, so re-running the same command only adds what is new, and
every run makes the next rebuild more likely to find footage it needs.

Clips are normalized on the way in (1080x1920, 30fps, no audio track — only the
picture is ever reused). Videos with no English captions are skipped: without
narration there is nothing to describe the clip by.

**On YouTube yield:** the Shorts *search* filter returns very few results for
some topics — "animal facts" gave 2. A **channel URL** is the far better harvest
source; `https://www.youtube.com/channel/UC.../` returned 15. Note that some
channels publish Shorts without exposing a shorts tab, and yt-dlp then reports
no tab at all — use the `channel_url` from a `grab`'s metadata rather than
guessing a handle.

**On Pexels:** set `PEXELS_API_KEY` (free key at <https://www.pexels.com/api/>)
or pass `--pexels-key`. Searches ask for portrait orientation and pick the
rendition nearest 1080x1920, so clips need no reframing. Stock videos run 20-30
seconds while segments run 2-4, so only the first 10 seconds of each is kept —
the renderer never uses more. One query yields a handful of clips, so build a
library by running several:

```sh
for q in "sea turtle swimming" "polar bear snow" "butterfly flower"; do
  faceless harvest "$q" --source pexels -n 3
done
```

### `faceless library`

The library is a SQLite database (`library/library.db`) plus the clip files
themselves. Three tables: `sources` (one row per harvested video), `clips` (one
per file, with an FTS5 full-text index), and `usages` (every match decision ever
made).

```sh
faceless library                          # what's in it, and what gets reused
faceless library --search "owl perched"   # full-text search, BM25 ranked
faceless library --gaps                   # queries that found nothing
faceless library --unused --provider pexels
```

`--gaps` is the useful one. Every remix records why each clip was chosen, so the
queries that fell through to filler accumulate into a list of what the library is
missing — a harvest plan the pipeline writes for itself:

```
3 queries that found nothing (harvest these):
  1x  black panther and transparent fur
  1x  lizard basking in the sun
```

Harvest those and the gap closes. Search also drives matching, so a bigger
library does not make matching slower — the shortlisting happens in SQLite, not
by scoring every clip in Python.

Why a database rather than a folder and a JSON index: the old `index.json` was
rewritten in full after every harvested source (133KB at 181 clips, ~3.6MB at
5,000), and a crash mid-write would have truncated the whole collection. Rows are
appended in a transaction instead, and BM25 ranks far better than the token-overlap
score it replaced. An existing `index.json` is imported automatically on first run.

### `faceless remix <url|id>`

Rebuilds a video shot for shot: keeps its audio, replaces every shot with a
library clip matched to what is being said at that moment.

```sh
faceless remix "https://youtu.be/VIDEO_ID" --dry-run          # print the plan, render nothing
faceless remix "https://youtu.be/VIDEO_ID" --source pexels    # clean footage only
faceless remix "https://youtu.be/VIDEO_ID" --captions         # with word-by-word captions
faceless remix "https://youtu.be/VIDEO_ID" -o remixes
```

`--source` restricts which footage is eligible. `--source pexels` is how you get
a remix with no burned-in captions; the default `any` draws on the whole library.
Filtered runs write to `<id>.remix.<source>.mp4` so they do not overwrite an
unfiltered one — handy for comparing the two.

When a remix uses Pexels footage, the credits its licence requires are printed
at the end of the run and included under `credits` in `--json`.

Matching runs in stages. SQLite's FTS5 shortlists candidates by BM25; the local
model then picks from the shortlist, but only when the top two are close — given
a clear winner it is skipped, because left to rerank freely it will talk itself
out of an exact match ("not a specific hippopotamus, but a farm with many
species...").

When full-text search finds *nothing*, the model gets one more job: widening the
query. Search cannot connect "black panther" to a clip described as "leopard
resting in natural habitat" — no shared words — so one call turns the query into
related subjects (leopard, jaguar, big cat) and search runs again. Only if that
also fails does a segment get filler, and the query is logged as a gap.

Clips from the video being rebuilt are never reused. Start with `--dry-run`: it
prints each segment, the query derived from its narration, the chosen clip and
why, and renders nothing — it is the cheap loop for judging match quality.

### Word-by-word captions

`--captions` burns the target's own narration into the output one word at a
time, each appearing on the frame where it is spoken.

```sh
faceless remix "https://youtu.be/VIDEO_ID" --captions
faceless remix "https://youtu.be/VIDEO_ID" --captions --caption-color rainbow
faceless remix "https://youtu.be/VIDEO_ID" --captions \
    --caption-font "Arial Black" --caption-size 170 \
    --caption-color "#FFE600" --caption-position 0.42
```

| Flag | Default | What it does |
|---|---|---|
| `--captions` | off | burn captions in; nothing below applies without it |
| `--caption-font` | `Impact` | any installed font family |
| `--caption-size` | `190` | cap height against a 1080×1920 frame |
| `--caption-color` | `#FFFFFF` | `#RRGGBB`, or `rainbow` (`--caption-colour` also works) |
| `--caption-outline` | `#000000` | outline and drop-shadow colour |
| `--caption-position` | `0.30` | height above the bottom edge, as a fraction of the frame |
| `--caption-mixed-case` | off | keep the transcript's capitalisation instead of upper-casing |
| `--no-caption-pop` | off | draw each word flat instead of popping it in |

`--caption-color rainbow` gives every word its own colour from a six-colour
palette — red, orange, yellow, green, cyan, violet. The colours are dealt as
shuffled packs rather than picked independently, so all six appear once per six
words in an order that still looks arbitrary, and the same colour never lands
twice in a row. Picking at random clumps badly enough to read as a bug; plain
cycling reads as a marching rainbow. The shuffle is seeded, so re-rendering the
same video gives the same colours and two runs stay comparable.

The palette is deliberately short and saturated — each word is on screen for
about a fifth of a second over moving footage. Indigo and true blue are left out
because both go muddy against dark footage even with the outline. The outline
stays a single colour, which is what keeps the bright fills readable.

The timing comes from YouTube's own per-word stamps — auto-captions carry a
`<00:00:00.560>` before every word — so a word appears when it is said rather
than when its subtitle cue starts. Sound effects like `[music]` are dropped, a
word never overlaps the next, and one left hanging by a pause in the speech
clears after a second instead of sitting there.

Words too wide for the frame are scaled down individually. libass will not break
inside a word, so without this a long one runs off both edges; every distinct
word is measured in the real font first, and only the offenders shrink.

`Impact`, `Arial Black` and `Segoe UI Black` are the safe heavy faces on
Windows. A font that is not installed is refused up front — libass would
otherwise substitute another silently and hand you a finished video in the wrong
typeface.

The ASS script is written next to the video (`<id>.remix.pexels.ass`), so you
can read exactly what was drawn and when, or edit it by hand. Re-burning an
edited one currently means re-running the remix.

### `faceless reset`

Deletes the clip library, the downloads and the rendered remixes, to start clean.

```sh
faceless reset                      # shows what it will delete, then asks
faceless reset --only library       # just the clip library
faceless reset --yes                # no prompt, for scripts
```

It prints the file count and size per directory before asking, and treats an
unanswered prompt as a no — so piping it into a script without `--yes` deletes
nothing. If a path cannot be removed it says which and exits non-zero rather
than reporting a success it did not achieve. A database viewer attached to
`library/library.db` will block that deletion on Windows; close it and re-run.

### Captions the *footage* came with, and what to do about them

Not to be confused with `--captions` above, which draws our own narration. This
is text baked into the borrowed clips before we ever saw them.

Clips harvested from YouTube are cut from *finished* Shorts, so they inherit
whatever those creators burned into the picture. A reused clip arrives with a
word like `HIPPOPOTAMUS` or `BANNED` still on screen — text that belonged to the
source's narration and now contradicts ours. It is the most visible flaw in a
YouTube-sourced remix, and it is inherent to that footage, not a bug in the
cutting.

**`--source pexels` avoids it entirely.** Stock footage carries no on-screen
text, so the pictures stay clean. Side by side on the same target video, the
YouTube remix had a caption stamped across most shots; the Pexels remix had none.
It also pairs best with `--captions`: on YouTube-sourced footage the two sets of
words stack on top of each other.

Removing text from footage that already has it is the harder road — it needs
per-frame text detection plus video inpainting, is slow, and leaves smears on
moving backgrounds. Avoiding the damage beats repairing it. The middle options,
if you want YouTube's visual style without the text, are cropping the caption
band (position varies by channel, and you lose picture) or screening clips for
on-screen text at harvest time and keeping only the clean ones. Neither is
implemented.

### Known: narration runs about a shot behind

Captions are attributed roughly one cue later than they were spoken, because the
VTT parser treats YouTube's whitespace-only separator line as the end of a cue.
Nothing is lost, but everything is late, which is the most likely reason a shot
sometimes matches the *previous* sentence. See CLAUDE.md → Known limitations for
the one-line fix and why it has not been applied yet.

This affects **matching** only. Burned-in captions read the per-word stamps
directly rather than the cue boundaries, so they are unaffected and stay on the
frame where the word is spoken.

### Local model

Descriptions, queries, and matching all run on a local Ollama model, so there is
no API key and no per-call cost. Defaults to `gemma4:12b`; override with
`--model` or `FACELESS_OLLAMA_MODEL`, and point at a different host with
`FACELESS_OLLAMA_HOST`.

The first call pages a ~8GB model in from disk and can take minutes; every call
after answers in about a second, and the model is held resident for 30 minutes
between calls. Both commands warm it up front so that wait happens once, visibly,
rather than looking like a hang mid-run.

### Shared options

`--cookies-from-browser chrome` or `--cookies FILE` authenticate the request.
Reach for these when YouTube starts answering with `403`/`429` or asking you to
confirm you are not a bot - which it will, if you make many requests in a row.
