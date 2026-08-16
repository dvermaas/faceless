# faceless

An AI-in-the-loop pipeline for producing YouTube Shorts. It finds Shorts, pulls
down their media and transcripts, detects every cut, files each shot as a
reusable clip, and rebuilds a target Short from that library over its original
audio.

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
  descriptions and drives matching. Only `harvest` and `remix` need it; `find`
  and `grab` work without it.

```sh
ollama pull gemma4:12b
```

Output lands in `downloads/` (sources), `library/` (clips), and `remixes/`
(finished videos). All three are gitignored - they are large and reproducible.

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

Finds Shorts, downloads them, splits each one along its own detected cuts, and
files every scene as a reusable clip.

```sh
faceless harvest "https://www.youtube.com/channel/UC..." -n 10
faceless harvest "kitchen tips" -n 5 --json
```

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

**On yield:** YouTube's Shorts *search* filter returns very few results for some
topics — "animal facts" gave 2. A **channel URL** is the far better harvest
source; `https://www.youtube.com/channel/UC.../` returned 15. Note that some
channels publish Shorts without exposing a shorts tab, and yt-dlp then reports
no tab at all — use the `channel_url` from a `grab`'s metadata rather than
guessing a handle.

### `faceless remix <url|id>`

Rebuilds a video shot for shot: keeps its audio, replaces every shot with a
library clip matched to what is being said at that moment.

```sh
faceless remix "https://youtu.be/VIDEO_ID" --dry-run   # print the plan, render nothing
faceless remix "https://youtu.be/VIDEO_ID" -o remixes
```

Matching runs in two stages. A lexical pass scores every clip by word overlap
and keeps a shortlist; the local model then picks from the shortlist. The split
is what keeps it affordable as the library grows — running hundreds of clips
through a model per segment would dominate the runtime, while word overlap alone
picks wrong whenever the words differ and the meaning does not.

Clips from the video being rebuilt are never reused. Start with `--dry-run`: it
prints each segment, the query derived from its narration, the chosen clip and
why, and renders nothing — it is the cheap loop for judging match quality.

### Known limitation: harvested clips carry their original captions

Harvested footage is cut from *finished* Shorts, not from clean stock. Most of
these videos burn their own captions into the picture, so a reused clip arrives
with a word like `HIPPOPOTAMUS` or `BANNED` still on screen — text that belonged
to the source's narration and now contradicts ours. It is the most visible flaw
in a rendered remix, and it is inherent to sourcing footage this way rather than
a bug in the cutting.

Three ways out, none implemented yet: crop the caption band (position varies by
channel, and cropping loses picture), screen clips for on-screen text at harvest
time and keep only clean ones, or prefer sources that do not burn in captions.

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
