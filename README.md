# Content_Pipeline

Faceless YouTube pipeline: transcribe → scene-plan → generate images → stitch into a long-form video, and cut vertical Shorts out of either images or finished video.

---

## Scripts

| Script | What it does |
|---|---|
| `generatescene.py` | Builds a scene plan from an SRT, optionally writes the scenes, then generates one image per beat with GPT-Image-2 |
| `slideshow.py` | Stitches the generated images into a long-form video, timed to your voiceover |
| `shorts.py` | Cuts vertical 9:16 Shorts from either an image folder or a finished video, with animated captions |
| `add_cta.py` | Appends a CTA clip to the end of a finished video |

---

## 1. Scene planning + image generation — `generatescene.py`

Reads a Whisper SRT, merges segments into beats, writes a TSV plan, and generates one image per beat. You can hand-write the scenes or let `gpt-4o-mini` draft them.

```bash
# Dry run — writes plans/<name>.tsv, no images, no cost
python generatescene.py --srt "input/your.srt" --prompt-file "prompts/style_d_doodle.txt" --no-character --min-seconds 7 --dry-run

# Let the model draft scenes into the plan
python generatescene.py --srt "input/your.srt" --prompt-file "prompts/style_d_doodle.txt" --no-character --min-seconds 7 --write-scenes

# Test three images before committing
python generatescene.py --srt "input/your.srt" --prompt-file "prompts/style_d_doodle.txt" --no-character --min-seconds 7 --limit 3

# Full run — resumes, skips images already on disk
python generatescene.py --srt "input/your.srt" --prompt-file "prompts/style_d_doodle.txt" --no-character --min-seconds 7
```

**Key flags**
- `--prompt-file` — the style block (`style_a` MS Paint, `style_c` Ghibli, `style_d` doodle, `style_e` stick, etc.)
- `--no-character` — don't lock a recurring character; use different stick figures as scenes need
- `--min-seconds 7` — minimum beat length. Lower = more images, faster cuts
- `--write-scenes` — model drafts the scene column; `--rewrite-scenes` overwrites existing
- `--limit N` — generate the first N images only, for testing
- `--force` — overwrite images already on disk
- `--allow-missing-scenes` — fall back to the raw narration if scenes aren't written

**Output**: `output/<srt stem>__<prompt stem>/001_00m00s.png`, named by beat index + start time.

---

## 2. Long-form video — `slideshow.py`

Stitches the beats into a timed slideshow matched to the voiceover.

```bash
python slideshow.py "output/your_srt__style_d_doodle" --audio "input/your.mp4"
```

**Flags**
- `--audio` — the voiceover (mp4, mp3, wav)
- `--fit crop|blur` — how to fit landscape images into 16:9
- `--fps 30` — frame rate

Lands as `slideshow.mp4` next to the images.

---

## 3. Vertical Shorts — `shorts.py`

Cuts 1080×1920 Shorts from either the image folder or a finished mp4, with styled burned-in captions.

```bash
# List candidate windows from the SRT with hook markers
python shorts.py "input/video.mp4" --srt "input/vo.srt" --list

# Cut one window
python shorts.py "input/video.mp4" --srt "input/vo.srt" --seg 12-18

# Several in one run
python shorts.py "input/video.mp4" --srt "input/vo.srt" --clips 1:10-1:45,4:33-5:12
```

### Window flags
- `--seg 12-18` — SRT segment range from `--list`
- `--beats 23-27` — plan beat range (image folder only)
- `--start 4:33 --end 5:12` — clock times
- `--clips a-b,c-d` — several windows in one call
- `--pad 0.5` — widen every window by this many seconds each side

### Caption styles
- `--caption-style plain` — static line per chunk
- `--caption-style pop` *(default)* — each chunk snaps to size as it lands
- `--caption-style karaoke` — per-word highlight on the spoken word
- `--caption-style hype` — karaoke + keyword colour + fade

Karaoke and hype are only tight with real word-level timing. Pass it with:
- `--words transcript.json` — Whisper JSON written with `--word_timestamps True`

### Keyword highlighting
- `--keywords status,options,drake` — explicit list
- `--keywords-file words.txt` — one per line, `#` for comments
- `--auto-keywords` — guess them

### Appearance
- `--font "Impact"` and `--font-size 76`
- `--caption-margin 420` — pixels up from the bottom
- `--caption-chars 20` — max characters per caption line
- `--base-color #FFFFFF --active-color #FF2222 --keyword-color #FFE100`

### Title (overlay headline)
- `--title "YOUR HOOK"`
- `--title-font "Georgia"` *(or Impact, Arial Black, Comic Sans MS)*
- `--title-align middle|top|bottom`
- `--title-size 90` — defaults to just over caption size
- `--title-seconds 2.5` — how long to show
- Put a `|` in the title to force a line break: `"HARDEST BATTLES|STRONGEST SOLDIERS"`

### Fit (how 16:9 lands in 9:16)
- `--fit crop` *(default)* — fills the frame, cuts the sides. `--crop-align center|left|right` picks which third survives
- `--fit blur` — footage over a blurred copy of itself. `--zoom 0.0`–`1.0` makes the footage bigger with blur still showing. `0.25` is the sweet spot
- `--fit bars` — same as blur but on flat color. `--pad-color "#101820"` sets the fill
- `--fit top` — whole frame parked near the top, color below. Good for layouts where captions sit in the empty space
- `--fit stretch` — squashes to fill. Everything visible, everything wrong

### Other
- `--no-captions` — no caption burn
- `--out-dir output/batch_shorts` — where clips land
- `--dry-run` — preview windows without rendering

**Filenames**: `short_<MmSSs>.mp4` based on the clip's start time, so re-running the same `--seg` overwrites instead of duplicating.

---

## 4. CTA — `add_cta.py`

Appends `cta_subscribe.webm` (or any clip) to the end of a finished video.

```bash
python add_cta.py "output/myvideo.mp4"
```

---

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
OPENAI_API_KEY=sk-proj-...
```

Also needs `ffmpeg` on PATH:
- Windows: `winget install Gyan.FFmpeg`
- Mac: `brew install ffmpeg`

For transcription:

```bash
pip install openai-whisper
whisper "input/your.wav" --model large --word_timestamps True --output_format all \
  --initial_prompt "proper nouns and jargon the model will mis-hear"
```

Use `--model large` for proper nouns and slang. The `--initial_prompt` trick biases the decoder toward your vocabulary.

---

## Folder layout

```
Content_Pipeline/
├── input/                 # SRTs, JSONs, source videos, voiceovers
├── output/                # generated images and finished clips
├── plans/                 # scene-plan TSVs, one per SRT
├── prompts/               # style blocks and character sheet
├── Completed videos/      # long-form outputs
├── generatescene.py
├── slideshow.py
├── shorts.py
├── add_cta.py
├── cta_subscribe.webm
└── .env                   # never committed
```

Media is gitignored — only scripts, prompts, and small TSVs go to the repo.