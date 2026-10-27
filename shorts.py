"""
Cut vertical Shorts or Reels out of a long-form video.

Works from either source:

  IMAGES   a folder of timestamped PNGs from generatescene.py, plus the
           voiceover and the SRT. Same assets slideshow.py uses.
  VIDEO    a finished .mp4 (or .mov/.mkv/.webm). Its own audio is used
           unless you pass --audio. Captions still come from --srt.

Either way you get a 1080x1920 clip with audio and burned-in captions,
ready to upload.

Finding the moment is the whole game, so start with --list:

    python shorts.py <source> --srt input/vo.srt --list

That prints every caption line with its clock time and flags the ones
that open like a hook. Pick a window, then cut it:

    python shorts.py <source> --srt input/vo.srt --seg 12-18
    python shorts.py <source> --srt input/vo.srt --start 4:33 --end 5:12
    python shorts.py <folder> --srt input/vo.srt --beats 23-27

Cut several at once:

    python shorts.py <source> --srt input/vo.srt --clips 1:10-1:45,4:33-5:12

Window flags:
    --seg 12-18     SRT segment range, from --list. Works on both sources.
    --beats 23-27   plan beat range. Image folders only.
    --start/--end   clock times.
    --clips a-b,c-d several windows in one run.
    --pad 1.5       widen every window by this many seconds on each side.

Caption flags:
    --caption-style plain | pop | karaoke | hype   (default pop)
        plain    static line per chunk
        pop      each chunk snaps up to size as it lands
        karaoke  one event per word, the spoken word lit up
        hype     karaoke plus keyword colour and a fade per chunk
    --words FILE          Whisper JSON from --word_timestamps True. Without
                          it, per-word timing is estimated from word length
                          and karaoke/hype drift a little inside long lines.
    --keywords a,b,c      words to colour differently
    --keywords-file FILE  one per line, # starts a comment
    --auto-keywords       guess them instead
    --title "TEXT"        burn a headline over the opening seconds
    --font, --font-size, --caption-margin, --caption-chars
    --base-color, --active-color, --keyword-color   (#RRGGBB)

For exact word timing:
    whisper AUDIO --word_timestamps True --output_format json

Requires ffmpeg on your PATH.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WIDTH = 1080
HEIGHT = 1920
FPS = 30

# Caption styling. Captions are written as ASS with the real frame size
# declared, so these are honest pixels. Styling an SRT instead puts you in
# libass's default 288-high script canvas, where a sane-looking MarginV
# silently pushes the text off screen.
FONT_SIZE = 76
FONT_NAME = "DejaVu Sans"
MARGIN_V = 420  # px up from the bottom, clears the Shorts UI
MARGIN_H = 70
OUTLINE = 6
CAPTION_MAX_CHARS = 20

# Caption animation. Colours are #RRGGBB here and converted to ASS's
# &HAABBGGRR on the way out, so you can paste hex straight from a picker.
BASE_COLOR = "#FFFFFF"  # the resting word
ACTIVE_COLOR = "#2BFF00"  # the word being spoken right now
KEYWORD_COLOR = "#FF3B30"  # words you flagged as the point of the clip
POP_MS = 130  # how long a chunk takes to snap to full size
POP_FROM = 85  # start scale, percent
ACTIVE_SCALE = 118  # how much the spoken word grows
KEYWORD_SCALE = 110

CAPTION_STYLES = ("plain", "pop", "karaoke", "hype")

# Title card. Its own font and placement, independent of the captions.
TITLE_FONT = "Comic Sans MS"
TITLE_ALIGNMENTS = {"top": 8, "middle": 5, "bottom": 2}
TITLE_ALIGN = "middle"
TITLE_MARGIN_V = 150  # used by top and bottom; middle ignores it

FIT_MODES = ("crop", "blur", "bars", "top", "stretch")
PAD_COLOR = "black"

# How much of the width --fit blur and bars throw away to make the
# footage bigger. 0.0 keeps the whole frame small in the middle, 1.0 is
# the same as --fit crop. Anything between is a partial crop with the
# background still showing.
ZOOM = 0.0

# Words that are never worth highlighting, so auto keyword picking has
# something to throw away.
STOPWORDS = {
    "a",
    "about",
    "actually",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "back",
    "be",
    "because",
    "been",
    "being",
    "but",
    "by",
    "can",
    "come",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "dont",
    "down",
    "even",
    "every",
    "for",
    "from",
    "get",
    "getting",
    "go",
    "going",
    "gonna",
    "good",
    "got",
    "gotta",
    "had",
    "has",
    "have",
    "he",
    "her",
    "here",
    "hes",
    "him",
    "his",
    "how",
    "i",
    "if",
    "im",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "like",
    "little",
    "lot",
    "make",
    "man",
    "may",
    "me",
    "mean",
    "more",
    "most",
    "much",
    "my",
    "never",
    "no",
    "not",
    "now",
    "of",
    "off",
    "oh",
    "on",
    "one",
    "only",
    "or",
    "other",
    "our",
    "out",
    "over",
    "really",
    "right",
    "said",
    "same",
    "say",
    "saying",
    "says",
    "see",
    "she",
    "shes",
    "should",
    "so",
    "some",
    "something",
    "still",
    "such",
    "take",
    "than",
    "that",
    "thats",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "theyre",
    "thing",
    "things",
    "think",
    "this",
    "those",
    "though",
    "thought",
    "through",
    "time",
    "to",
    "too",
    "up",
    "us",
    "very",
    "want",
    "was",
    "way",
    "we",
    "well",
    "went",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
    "yeah",
    "you",
    "your",
    "youre",
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# Openers that tend to stop a scroll. Used only to star candidates in
# --list. It is a hint for you, not a decision the script makes.
HOOK_WORDS = (
    "here is",
    "here's",
    "this is why",
    "the reason",
    "nobody",
    "everybody",
    "most guys",
    "the truth",
    "listen",
    "look",
    "imagine",
    "what if",
    "stop",
    "never",
    "always",
    "biggest",
    "worst",
    "best",
    "secret",
    "mistake",
    "truth is",
    "real talk",
    "let me tell you",
    "i am going to",
    "i'm going to",
    "you need to",
)

NAME_RE = re.compile(r"^(\d+)_(\d+)m(\d+)s\.png$", re.IGNORECASE)
TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*" r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


def require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise SystemExit(
            f"{tool} not found on your PATH.\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Mac:      brew install ffmpeg\n"
            "Then close and reopen your terminal."
        )


def parse_clock(value: str) -> float:
    """Accept 4:33, 04:33, 1:04:33, or plain seconds."""
    value = value.strip()
    if not value:
        raise SystemExit("Empty time value")
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"Could not read the time {value!r}") from exc

    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    raise SystemExit(f"Could not read the time {value!r}")


def clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def ass_stamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def media_duration(path: Path) -> float:
    require("ffprobe")
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"Could not read the length of {path}\n{out.stderr}")
    return float(out.stdout.strip())


# ---------------------------------------------------------------------------
# INPUTS
# ---------------------------------------------------------------------------


def parse_srt(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig")
    segments: list[dict] = []

    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        match = None
        text_start = 0
        for i, line in enumerate(lines[:2]):
            match = TIME_RE.search(line)
            if match:
                text_start = i + 1
                break
        if not match:
            continue

        text = " ".join(lines[text_start:]).strip()
        if not text:
            continue

        g = match.groups()

        def to_s(h, m, s, ms):
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

        segments.append({"start": to_s(*g[0:4]), "end": to_s(*g[4:8]), "text": text})

    if not segments:
        raise SystemExit(f"No subtitle segments parsed from {path}")
    return segments


def collect_images(folder: Path) -> list[tuple[Path, float]]:
    """Return [(path, start_seconds)] sorted by start time."""
    found = []
    for path in folder.iterdir():
        match = NAME_RE.match(path.name)
        if match:
            _, mins, secs = match.groups()
            found.append((path, int(mins) * 60 + int(secs)))

    if not found:
        raise SystemExit(
            f"No timestamped images in {folder}\n" "Expected names like 014_02m17s.png."
        )
    found.sort(key=lambda pair: pair[1])
    return found


def parse_window_spec(spec: str) -> tuple[str, str]:
    """Split '1:10-1:45' into its two halves, minding negative-free input."""
    match = re.fullmatch(r"\s*([\d:.]+)\s*-\s*([\d:.]+)\s*", spec)
    if not match:
        raise SystemExit(f"Could not read the window {spec!r}. Try 1:10-1:45.")
    return match.group(1), match.group(2)


def window_from_segments(segments, spec: str) -> tuple[float, float]:
    """--seg 12-18 means from segment 12's start to segment 18's end."""
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", spec)
    if not match:
        raise SystemExit("--seg wants a range like 12-18")

    first, last = int(match.group(1)), int(match.group(2))
    if first < 1 or last < first or last > len(segments):
        raise SystemExit(f"--seg out of range. This SRT has {len(segments)} segments.")
    return segments[first - 1]["start"], segments[last - 1]["end"]


def looks_like_hook(text: str) -> bool:
    low = text.lower().lstrip("\"'- ")
    if low.startswith(("so ", "and ", "but ", "um ", "uh ", "like ")):
        return False
    return any(low.startswith(w) or f" {w}" in low[:40] for w in HOOK_WORDS)


def print_segment_list(segments) -> None:
    print(f"{len(segments)} caption segments\n")
    for i, seg in enumerate(segments, start=1):
        mark = " *" if looks_like_hook(seg["text"]) else "  "
        span = seg["end"] - seg["start"]
        text = seg["text"][:72]
        print(f"{mark}{i:>4}  {clock(seg['start'])}  ({span:4.1f}s)  {text}")
    print(
        "\n* = opens like a hook. Aim for 20-60s: --seg 12-18, "
        "or --start 4:33 --end 5:12"
    )


def window_from_beats(images, spec: str, total: float) -> tuple[float, float]:
    """--beats 23-27 means from beat 23's start to beat 28's start."""
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", spec)
    if not match:
        raise SystemExit("--beats wants a range like 23-27")

    first, last = int(match.group(1)), int(match.group(2))
    if first < 1 or last < first or last > len(images):
        raise SystemExit(f"--beats out of range. This folder has {len(images)}.")

    start = images[first - 1][1]
    end = images[last][1] if last < len(images) else total
    return float(start), float(end)


def clip_images(images, start: float, end: float) -> list[tuple[Path, float]]:
    """
    Images that are on screen during the window, with durations trimmed
    to the window edges.
    """
    timed: list[tuple[Path, float]] = []

    for i, (path, img_start) in enumerate(images):
        img_end = images[i + 1][1] if i + 1 < len(images) else end
        visible_from = max(img_start, start)
        visible_to = min(img_end, end)
        if visible_to - visible_from > 0.05:
            timed.append((path, visible_to - visible_from))

    if not timed:
        raise SystemExit(f"No images fall between {clock(start)} and {clock(end)}.")
    return timed


def write_concat(timed, path: Path) -> None:
    """
    Write the concat demuxer list.

    The demuxer works out each entry's length from where the next one
    starts, so the final duration line has nothing to measure against and
    gets cut short. Repeating the last file with no duration gives it
    something to measure against; -t at render time trims the overhang.
    """
    lines = []
    for image, hold in timed:
        safe = str(image.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
        lines.append(f"duration {hold:.3f}")

    last = str(timed[-1][0].resolve()).replace("\\", "/").replace("'", r"'\''")
    lines.append(f"file '{last}'")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ass_color(hex_color: str) -> str:
    """#RRGGBB -> &HAABBGGRR. ASS stores colour backwards and alpha first."""
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        raise SystemExit(f"Colour must look like #FF3B30, got {hex_color!r}")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError as exc:
        raise SystemExit(f"Bad colour {hex_color!r}") from exc
    return f"&H00{b:02X}{g:02X}{r:02X}"


def parse_words_json(path: Path) -> list[dict]:
    """
    Read a Whisper JSON written with --word_timestamps True.

    Returns the same shape parse_srt does, plus a 'words' list on every
    segment: [{'text', 'start', 'end'}, ...] with real spoken times.
    """
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path.name} is not valid JSON\n{exc}") from exc

    raw_segments = data.get("segments")
    if not raw_segments:
        raise SystemExit(
            f"{path.name} has no segments.\n"
            "Transcribe with:  whisper AUDIO --word_timestamps True "
            "--output_format json"
        )

    segments: list[dict] = []
    missing_words = 0

    for seg in raw_segments:
        words = []
        for w in seg.get("words") or []:
            text = str(w.get("word", "")).strip()
            if not text:
                continue
            try:
                w_start = float(w["start"])
                w_end = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            words.append({"text": text, "start": w_start, "end": w_end})

        if not words:
            missing_words += 1

        segments.append(
            {
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": str(seg.get("text", "")).strip(),
                "words": words,
            }
        )

    if not segments:
        raise SystemExit(f"No segments parsed from {path}")

    if missing_words == len(segments):
        raise SystemExit(
            f"{path.name} has segments but no word timings.\n"
            "Re-run Whisper with --word_timestamps True."
        )
    if missing_words:
        print(f"        note: {missing_words} segments had no word timings")

    return segments


def words_for_segment(seg: dict) -> list[dict]:
    """
    Real word timings when the transcript has them, estimated when it
    does not.

    The estimate splits the segment by word length rather than word
    count, since 'unattainable' takes longer to say than 'a'. Good enough
    for colour, not good enough for per-word highlighting, which is why
    --words exists.
    """
    if seg.get("words"):
        return seg["words"]

    pieces = seg["text"].split()
    if not pieces:
        return []

    weights = [len(w) + 1 for w in pieces]
    total_weight = sum(weights)
    span = max(seg["end"] - seg["start"], 0.001)

    words = []
    cursor = seg["start"]
    for piece, weight in zip(pieces, weights):
        hold = span * (weight / total_weight)
        words.append({"text": piece, "start": cursor, "end": cursor + hold})
        cursor += hold
    return words


def chunk_words(words: list[dict], max_chars: int) -> list[list[dict]]:
    """Group words into lines short enough to read on a phone."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    length = 0

    for word in words:
        add = len(word["text"]) + (1 if current else 0)
        if current and length + add > max_chars:
            chunks.append(current)
            current, length = [word], len(word["text"])
        else:
            current.append(word)
            length += add

    if current:
        chunks.append(current)
    return chunks


def normalize_word(text: str) -> str:
    return re.sub(r"[^a-z0-9']", "", text.lower())


def load_keywords(explicit: str | None, file_arg: str | None) -> set[str]:
    words: set[str] = set()

    if explicit:
        for item in explicit.split(","):
            cleaned = normalize_word(item)
            if cleaned:
                words.add(cleaned)

    if file_arg:
        path = Path(file_arg)
        if not path.exists():
            raise SystemExit(f"Keyword file not found: {path}")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#", 1)[0]
            for item in re.split(r"[,\s]+", line):
                cleaned = normalize_word(item)
                if cleaned:
                    words.add(cleaned)

    return words


def auto_keywords(all_words: list[dict], share: float = 0.18) -> set[str]:
    """
    Guess which words carry the clip.

    Longer, rarer, non-stopword words win. It is a blunt heuristic and it
    will pick a dud now and then, which is why --keywords-file exists for
    when you care.
    """
    counts: dict[str, int] = {}
    for word in all_words:
        cleaned = normalize_word(word["text"])
        if cleaned and cleaned not in STOPWORDS and len(cleaned) > 3:
            counts[cleaned] = counts.get(cleaned, 0) + 1

    if not counts:
        return set()

    ranked = sorted(counts, key=lambda w: (-len(w), -counts[w], w))
    keep = max(1, int(len(ranked) * share))
    return set(ranked[:keep])


def styled_word(
    text: str,
    base: str,
    colour: str | None,
    scale: int | None,
) -> str:
    """Wrap one word in inline overrides and put the style back after."""
    safe = text.upper().replace("{", "(").replace("}", ")").replace("\\", "/")
    if colour is None and scale is None:
        return safe

    opening = "{"
    if colour:
        opening += f"\\c{colour}"
    if scale:
        opening += f"\\fscx{scale}\\fscy{scale}"
    opening += "}"

    closing = "{"
    if colour:
        closing += f"\\c{base}"
    if scale:
        closing += "\\fscx100\\fscy100"
    closing += "}"

    return f"{opening}{safe}{closing}"


def chunk_line(
    chunk: list[dict],
    active_index: int | None,
    keywords: set[str],
    colours: dict,
) -> str:
    """Render one caption line, optionally with one word lit up."""
    out = []
    for i, word in enumerate(chunk):
        cleaned = normalize_word(word["text"])
        is_active = active_index is not None and i == active_index
        is_key = cleaned in keywords

        if is_active:
            out.append(
                styled_word(
                    word["text"],
                    colours["base"],
                    colours["active"],
                    ACTIVE_SCALE,
                )
            )
        elif is_key:
            out.append(
                styled_word(
                    word["text"],
                    colours["base"],
                    colours["keyword"],
                    KEYWORD_SCALE,
                )
            )
        else:
            out.append(styled_word(word["text"], colours["base"], None, None))

    return " ".join(out)


def write_window_ass(
    segments,
    start: float,
    end: float,
    path: Path,
    style: str = "pop",
    keywords: set[str] | None = None,
    font_name: str = FONT_NAME,
    font_size: int = FONT_SIZE,
    margin_v: int = MARGIN_V,
    max_chars: int = CAPTION_MAX_CHARS,
    colours: dict | None = None,
    title: str | None = None,
    title_seconds: float = 2.5,
    title_font: str = TITLE_FONT,
    title_size: int | None = None,
    title_align: str = TITLE_ALIGN,
) -> int:
    """
    Re-time the captions to the clip and write them as ASS.

    Whisper segments run 8-15 words, which is a wall of text on a phone,
    so every segment is broken into short chunks first.

    style:
      plain    one static line per chunk
      pop      same, but each chunk snaps up to size as it appears
      karaoke  one event per word, the spoken word lit up
      hype     karaoke plus keyword colouring and a fade on each chunk

    karaoke and hype are only as tight as the word timings behind them.
    With --words they land on the syllable. Without, they are estimated
    from word length and will drift a little inside a long segment.
    """
    keywords = keywords or set()
    colours = colours or {
        "base": ass_color(BASE_COLOR),
        "active": ass_color(ACTIVE_COLOR),
        "keyword": ass_color(KEYWORD_COLOR),
    }

    title_px = title_size or int(font_size * 1.05)
    title_alignment = TITLE_ALIGNMENTS.get(title_align, 5)
    title_margin = 0 if title_align == "middle" else TITLE_MARGIN_V

    chunks: list[list[dict]] = []
    for seg in segments:
        if seg["end"] <= start or seg["start"] >= end:
            continue

        for word in chunk_words(words_for_segment(seg), max_chars):
            clipped = [w for w in word if w["end"] > start and w["start"] < end]
            if clipped:
                chunks.append(clipped)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font_name},{font_size},{colours["base"]},&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,{OUTLINE},2,2,{MARGIN_H},{MARGIN_H},{margin_v},1
Style: Title,{title_font},{title_px},{colours["base"]},&H000000FF,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,{OUTLINE},3,{title_alignment},{MARGIN_H},{MARGIN_H},{title_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    events = 0

    if title:
        safe_title = (
            title.replace("{", "(").replace("}", ")").upper().replace("|", "\\N")
        )
        lines.append(
            f"Dialogue: 0,{ass_stamp(0)},{ass_stamp(title_seconds)},"
            f"Title,,0,0,0,,{{\\fad(120,200)}}{safe_title}"
        )
        events += 1

    per_word = style in ("karaoke", "hype")
    pop_prefix = (
        f"{{\\fad(50,0)\\fscx{POP_FROM}\\fscy{POP_FROM}"
        f"\\t(0,{POP_MS},\\fscx100\\fscy100)}}"
    )
    fade_prefix = "{\\fad(40,0)}"

    for chunk in chunks:
        chunk_from = max(chunk[0]["start"], start)
        chunk_to = min(chunk[-1]["end"], end)
        if chunk_to - chunk_from <= 0.04:
            continue

        if not per_word:
            prefix = pop_prefix if style == "pop" else ""
            body = chunk_line(chunk, None, keywords, colours)
            lines.append(
                f"Dialogue: 0,{ass_stamp(chunk_from - start)},"
                f"{ass_stamp(chunk_to - start)},Main,,0,0,0,,{prefix}{body}"
            )
            events += 1
            continue

        for i, word in enumerate(chunk):
            word_from = max(word["start"], chunk_from)
            word_to = min(word["end"], chunk_to)
            if i == len(chunk) - 1:
                word_to = chunk_to
            if word_to - word_from <= 0.01:
                continue

            prefix = fade_prefix if (style == "hype" and i == 0) else ""
            body = chunk_line(chunk, i, keywords, colours)
            lines.append(
                f"Dialogue: 0,{ass_stamp(word_from - start)},"
                f"{ass_stamp(word_to - start)},Main,,0,0,0,,{prefix}{body}"
            )
            events += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events


# ---------------------------------------------------------------------------
# RENDER
# ---------------------------------------------------------------------------


def foreground_chain(zoom: float, crop_align: str) -> tuple[str, int]:
    """
    Build the scale/crop for the footage itself and return its height.

    A 16:9 source laid full width into a 1080 wide frame is only 608 tall,
    which is the small band you get at zoom 0. Zoom 1 makes it 1920 tall,
    which fills the frame and is identical to --fit crop. In between we
    crop the sides just enough to hit the height we want, so the footage
    grows and the background still shows above and below.
    """
    zoom = min(max(zoom, 0.0), 1.0)
    flat = WIDTH * 9 // 16  # 608 for a 16:9 source laid full width
    height = int(round(flat + zoom * (HEIGHT - flat)))
    height -= height % 2  # x264 wants even dimensions

    if height <= flat:
        return f"scale={WIDTH}:-2", flat

    # keep a slice of the source whose aspect matches the band we want
    if crop_align == "left":
        x = "0"
    elif crop_align == "right":
        x = "iw-ow"
    else:
        x = "(iw-ow)/2"

    chain = f"crop=min(iw\\,ih*{WIDTH}/{height}):ih:{x}:0," f"scale={WIDTH}:{height}"
    return chain, height


def video_filter(
    fit: str,
    captions: Path | None,
    fps: int,
    crop_align: str = "center",
    pad_color: str = PAD_COLOR,
    zoom: float = ZOOM,
) -> str:
    """
    Fit the source into a 1080x1920 frame.

    16:9 is 1.78 wide, 9:16 is 0.56. Nothing shows the whole frame AND
    fills the canvas, so pick the compromise:

    crop     fills the canvas, throws away most of the width
    blur     footage over a blurred zoom of itself. --zoom 0 keeps the
             whole frame; higher values crop the sides so the footage
             gets bigger, with the blur still behind it
    bars     same as blur but on flat colour instead of blur
    top      whole frame, full width, parked near the top
    stretch  fills the canvas by squashing. Everything visible, all wrong
    """
    if fit == "crop":
        if crop_align == "left":
            x = "0"
        elif crop_align == "right":
            x = "iw-ow"
        else:
            x = "(iw-ow)/2"
        base = (
            f"fps={fps},"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT}:{x}:(ih-oh)/2,setsar=1"
        )
    elif fit == "stretch":
        base = f"fps={fps},scale={WIDTH}:{HEIGHT},setsar=1"
    elif fit == "top":
        base = (
            f"fps={fps},scale={WIDTH}:-2,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)*0.28:color={pad_color},"
            f"setsar=1"
        )
    elif fit == "bars":
        fg, _ = foreground_chain(zoom, crop_align)
        base = (
            f"fps={fps},{fg},"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={pad_color},"
            f"setsar=1"
        )
    else:
        fg, _ = foreground_chain(zoom, crop_align)
        base = (
            f"fps={fps},split[bg][fg];"
            f"[bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},boxblur=40:2[blurred];"
            f"[fg]{fg}[sharp];"
            f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )

    if captions is None:
        return base

    # ffmpeg's filter parser needs the path escaped, Windows drive colon included
    safe = str(captions.resolve()).replace("\\", "/").replace(":", r"\:")
    return f"{base},ass='{safe}'"


def build_cmd(
    args,
    source_kind: str,
    source,
    audio: Path | None,
    start: float,
    length: float,
    captions_path: Path | None,
    out_path: Path,
    tmp: Path,
) -> list[str]:
    """
    One ffmpeg invocation, built differently per source.

    images  concat demuxer of stills, audio from --audio
    video   seek into the file, audio from the file unless --audio given
    """
    cmd = ["ffmpeg", "-y"]

    if source_kind == "images":
        concat_path = tmp / "concat.txt"
        write_concat(source, concat_path)
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_path)]
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio)]
        cmd += ["-map", "0:v", "-map", "1:a"]
    else:
        # -ss before -i seeks fast and stays frame accurate on modern ffmpeg
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(source)]
        if audio:
            cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio)]
            cmd += ["-map", "0:v", "-map", "1:a"]
        else:
            cmd += ["-map", "0:v", "-map", "0:a?"]

    cmd += [
        "-vf",
        video_filter(
            args.fit,
            captions_path,
            args.fps,
            args.crop_align,
            args.pad_color,
            args.zoom,
        ),
        "-t",
        f"{length:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "20",
        "-preset",
        "medium",
    ]
    if source_kind == "images":
        cmd += ["-tune", "stillimage"]
    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    return cmd


def render_one(
    args,
    source_kind: str,
    images,
    video_path: Path | None,
    audio: Path | None,
    segments,
    start: float,
    end: float,
    out_dir: Path,
    index: int | None = None,
    keywords: set[str] | None = None,
    colours: dict | None = None,
) -> None:
    length = end - start
    if length <= 0:
        raise SystemExit("The window has no length.")
    if length > 180:
        print(f"WARNING: {length:.0f}s is over the 3 minute Shorts limit")

    print(f"\nWindow  {clock(start)} - {clock(end)}  ({length:.1f}s)")

    if source_kind == "images":
        source = clip_images(images, start, end)
        print(f"Images  {len(source)}")
    else:
        source = video_path
        print(f"Source  {video_path.name}")

    print(f"Fit     {args.fit}")

    use_captions = bool(segments) and not args.no_captions
    if args.no_captions:
        print("Caption off")
    elif not segments:
        print("Caption off, pass --srt to burn captions")

    if args.dry_run:
        if source_kind == "images":
            for image, hold in source:
                print(f"  {image.name}  {hold:5.2f}s")
        print("Dry run. Nothing rendered.")
        return

    require("ffmpeg")

    if args.out and index is None:
        out_path = Path(args.out)
    else:
        tag = clock(start).replace(":", "m")
        out_path = out_dir / f"short_{tag}s.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="shorts_"))
    try:
        captions_path = None
        if use_captions:
            captions_path = tmp / "captions.ass"
            n = write_window_ass(
                segments,
                start,
                end,
                captions_path,
                style=args.caption_style,
                keywords=keywords or set(),
                font_name=args.font,
                font_size=args.font_size,
                margin_v=args.caption_margin,
                max_chars=args.caption_chars,
                colours=colours,
                title=args.title,
                title_seconds=args.title_seconds,
                title_font=args.title_font,
                title_size=args.title_size,
                title_align=args.title_align,
            )
            print(f"Caption {n} events ({args.caption_style})")

        cmd = build_cmd(
            args,
            source_kind,
            source,
            audio,
            start,
            length,
            captions_path,
            out_path,
            tmp,
        )

        print(f"Rendering {out_path.name} ...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.strip().splitlines()[-20:])
            raise SystemExit(f"\nffmpeg failed:\n{tail}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    size_mb = out_path.stat().st_size / 1_000_000
    print(f"Done. {out_path}  ({size_mb:.0f} MB, {length:.0f}s)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", help="image folder, or a finished video file")
    p.add_argument("--audio", help="voiceover (required for an image folder)")
    p.add_argument("--srt", help="the SRT, for captions and for --list / --seg")
    p.add_argument("--start", help="clip start, like 4:33")
    p.add_argument("--end", help="clip end, like 5:12")
    p.add_argument("--seg", help="SRT segment range from --list, like 12-18")
    p.add_argument("--beats", help="plan beat range, image folders only")
    p.add_argument("--clips", help="several windows: 1:10-1:45,4:33-5:12")
    p.add_argument(
        "--pad",
        type=float,
        default=0.0,
        help="widen every window by this many seconds each side",
    )
    p.add_argument("--out", help="output file (single clip only)")
    p.add_argument("--out-dir", help="folder for the clips")
    p.add_argument(
        "--fit",
        choices=list(FIT_MODES),
        default="crop",
        help="crop (default) fills the frame, top keeps the whole frame "
        "near the top, bars centres it, blur pads with a blurred copy, "
        "stretch squashes it to fit",
    )
    p.add_argument(
        "--crop-align",
        choices=["center", "left", "right"],
        default="center",
        help="which part of the width to keep when --fit crop",
    )
    p.add_argument(
        "--zoom",
        type=float,
        default=ZOOM,
        help="0.0 to 1.0 for --fit blur and bars. 0 keeps the whole frame, "
        "1 fills the canvas like crop. Try 0.4",
    )
    p.add_argument(
        "--pad-color",
        default=PAD_COLOR,
        help="fill colour for --fit top and bars (black, white, #101820)",
    )
    p.add_argument("--fps", type=int, default=FPS)
    p.add_argument("--no-captions", action="store_true")
    p.add_argument(
        "--words",
        help="Whisper JSON with --word_timestamps True, for exact per-word timing",
    )
    p.add_argument(
        "--caption-style",
        choices=CAPTION_STYLES,
        default="pop",
        help="plain, pop (default), karaoke, or hype",
    )
    p.add_argument(
        "--keywords",
        help="comma separated words to highlight, e.g. status,options,lonely",
    )
    p.add_argument("--keywords-file", help="one keyword per line, # for comments")
    p.add_argument(
        "--auto-keywords",
        action="store_true",
        help="guess the keywords instead of naming them",
    )
    p.add_argument("--font", default=FONT_NAME)
    p.add_argument("--font-size", type=int, default=FONT_SIZE)
    p.add_argument(
        "--caption-margin", type=int, default=MARGIN_V, help="px up from the bottom"
    )
    p.add_argument(
        "--caption-chars",
        type=int,
        default=CAPTION_MAX_CHARS,
        help="max characters per caption line",
    )
    p.add_argument("--base-color", default=BASE_COLOR)
    p.add_argument("--active-color", default=ACTIVE_COLOR)
    p.add_argument("--keyword-color", default=KEYWORD_COLOR)
    p.add_argument("--title", help="burn a headline over the first seconds")
    p.add_argument("--title-seconds", type=float, default=2.5)
    p.add_argument("--title-font", default=TITLE_FONT)
    p.add_argument(
        "--title-size", type=int, help="defaults to just over the caption size"
    )
    p.add_argument(
        "--title-align",
        choices=list(TITLE_ALIGNMENTS),
        default=TITLE_ALIGN,
        help="middle (default), top, or bottom",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="show the caption lines with hook markers, then stop",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    source_path = Path(args.source)
    if source_path.is_dir():
        source_kind = "images"
    elif source_path.is_file() and source_path.suffix.lower() in VIDEO_EXTS:
        source_kind = "video"
    elif source_path.is_file():
        raise SystemExit(
            f"{source_path.name} is not a video this script reads.\n"
            f"Known: {', '.join(sorted(VIDEO_EXTS))}"
        )
    else:
        raise SystemExit(f"Not found: {source_path}")

    segments = None
    word_timed = False
    if args.words:
        words_path = Path(args.words)
        if not words_path.exists():
            raise SystemExit(f"Word JSON not found: {words_path}")
        segments = parse_words_json(words_path)
        word_timed = True
    elif args.srt:
        srt_path = Path(args.srt)
        if not srt_path.exists():
            raise SystemExit(f"SRT not found: {srt_path}")
        segments = parse_srt(srt_path)

    if args.list:
        if segments:
            print_segment_list(segments)
            return
        if source_kind == "images":
            require("ffprobe")
            if not args.audio:
                raise SystemExit("--list on an image folder needs --srt or --audio")
            images = collect_images(source_path)
            total = media_duration(Path(args.audio))
            print(f"{len(images)} beats in {source_path.name}\n")
            for i, (path, start) in enumerate(images, start=1):
                end = images[i][1] if i < len(images) else total
                print(f"  {i:>3}  {clock(start)} - {clock(end)}  {path.name}")
            print("\nPick a window with --beats 12-16 or --start 2:10 --end 2:55")
            return
        raise SystemExit("--list needs --srt")

    images = None
    video_path = None
    audio = None

    if source_kind == "images":
        if not args.audio:
            raise SystemExit("An image folder needs --audio for the voiceover.")
        audio = Path(args.audio)
        if not audio.exists():
            raise SystemExit(f"Audio not found: {audio}")
        images = collect_images(source_path)
        require("ffprobe")
        total = media_duration(audio)
    else:
        video_path = source_path
        require("ffprobe")
        total = media_duration(video_path)
        if args.audio:
            audio = Path(args.audio)
            if not audio.exists():
                raise SystemExit(f"Audio not found: {audio}")
        if args.beats:
            raise SystemExit("--beats only works on an image folder. Use --seg.")

    windows: list[tuple[float, float]] = []

    if args.clips:
        for chunk in args.clips.split(","):
            a, b = parse_window_spec(chunk)
            windows.append((parse_clock(a), parse_clock(b)))
    elif args.seg:
        if not segments:
            raise SystemExit("--seg needs --srt")
        windows.append(window_from_segments(segments, args.seg))
    elif args.beats:
        windows.append(window_from_beats(images, args.beats, total))
    elif args.start and args.end:
        windows.append((parse_clock(args.start), parse_clock(args.end)))
    else:
        raise SystemExit(
            "Pick a window: --seg 12-18, --start 4:33 --end 5:12, "
            "--beats 23-27, or --clips 1:10-1:45,4:33-5:12\n"
            "Run with --srt ... --list to see the options."
        )

    if args.pad:
        windows = [(max(0.0, a - args.pad), b + args.pad) for a, b in windows]

    windows = [(a, min(b, total)) for a, b in windows]
    for a, b in windows:
        if b <= a:
            raise SystemExit(f"Window {clock(a)}-{clock(b)} has no length.")

    colours = {
        "base": ass_color(args.base_color),
        "active": ass_color(args.active_color),
        "keyword": ass_color(args.keyword_color),
    }

    keywords = load_keywords(args.keywords, args.keywords_file)
    if args.auto_keywords and segments:
        pool: list[dict] = []
        for seg in segments:
            pool.extend(words_for_segment(seg))
        keywords |= auto_keywords(pool)

    if segments and not args.no_captions:
        if args.caption_style in ("karaoke", "hype") and not word_timed:
            print(
                "Note    per-word timing is estimated. For exact timing:\n"
                "        whisper AUDIO --word_timestamps True "
                "--output_format json\n"
                "        then pass it with --words"
            )

    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif source_kind == "images":
        out_dir = source_path / "shorts"
    else:
        out_dir = source_path.parent / "shorts"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Source  {source_path.name}  ({source_kind})")
    print(f"Clips   {len(windows)}")
    if segments and not args.no_captions:
        timing = "word level" if word_timed else "estimated"
        print(f"Style   {args.caption_style}, timing {timing}")
        if keywords:
            shown = ", ".join(sorted(keywords)[:8])
            more = f" +{len(keywords) - 8}" if len(keywords) > 8 else ""
            print(f"Keys    {shown}{more}")

    for i, (start, end) in enumerate(windows):
        render_one(
            args,
            source_kind,
            images,
            video_path,
            audio,
            segments,
            start,
            end,
            out_dir,
            index=i if len(windows) > 1 else None,
        )

    if not args.dry_run:
        print(f"\nAll clips in {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
