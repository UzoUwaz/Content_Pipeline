"""
Batch image generator for timestamped YouTube scripts.

Reads a Whisper .srt file, merges segments into beats, and generates one
image per beat with the OpenAI Images API.

Images are driven by a beat plan, not by raw narration. The plan is a TSV
you edit by hand between two runs:

    1.  python generatescene.py --dry-run
        Writes plans/<srt name>.tsv and generates nothing.

    2.  Open the plan. Fill in the `scene` column for every beat: a
        concrete, literal description of what the viewer should SEE while
        that line is spoken. Fill in `on_screen_text` for the beats you
        want words on. Leave the rest blank.

    3.  python generatescene.py

Short on time? Step 2 can be done for you:

    python generatescene.py --write-scenes

That fills every blank scene cell in the plan with a text model and stops.
It never touches a cell you typed yourself. Read them over, fix the ones
that miss, then generate.

Narration is an argument. A scene is a picture. "Lean is the floor, not
the ceiling" has nothing drawable in it, and an image model handed that
line will fall back on something scenic and generic. "A man in a bathroom
mirror lifting his shirt, checking his abs, face unimpressed" is a shot.

The plan lives under plans/ keyed on the SRT, not the style, so scenes
survive switching prompt files.

Usage:
    python generatescene.py --dry-run
    python generatescene.py --limit 3
    python generatescene.py
    python generatescene.py --srt input/other.srt --quality medium
    python generatescene.py --prompt-file prompts/style_a.txt --no-character
    python generatescene.py --ref refs/character.png
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from openai import APIConnectionError, APITimeoutError, RateLimitError

# ---------------------------------------------------------------------------
# DEFAULTS  (override any of these from the command line)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
PLAN_DIR = ROOT / "plans"
PROMPT_FILE = ROOT / "prompts" / "style_d_doodle.txt"
CHARACTER_FILE = ROOT / "prompts" / "character.txt"
# Switch styles with:  --prompt-file prompts/style_b_noir.txt

MODEL = "gpt-image-2"
SCENE_MODEL = "gpt-4o-mini"  # writes the scene column; pennies per video
SIZE = "1536x1024"
QUALITY = "low"

MIN_BEAT_SECONDS = 10.0
MAX_BEAT_CHARS = 400

RETRIES = 3

TEXT_PLACEHOLDER = "{TEXT}"
PLAN_FIELDS = ["n", "timestamp", "file", "scene", "on_screen_text", "narration"]

# ---------------------------------------------------------------------------
# SRT PARSING
# ---------------------------------------------------------------------------

TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*" r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[dict]:
    """Return a list of {'start', 'end', 'text'} dicts."""
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
        segments.append(
            {
                "start": _to_seconds(*g[0:4]),
                "end": _to_seconds(*g[4:8]),
                "text": text,
            }
        )

    return segments


def merge_into_beats(
    segments: list[dict],
    min_seconds: float,
    max_chars: int,
) -> list[dict]:
    """
    Whisper splits on pauses, which yields far more segments than you want
    images for. Merge short adjacent segments into meaningful beats.
    """
    beats: list[dict] = []
    current: dict | None = None

    for seg in segments:
        if current is None:
            current = dict(seg)
            continue

        too_short = (current["end"] - current["start"]) < min_seconds
        room_left = len(current["text"]) + len(seg["text"]) + 1 <= max_chars

        if too_short and room_left:
            current["end"] = seg["end"]
            current["text"] = f"{current['text']} {seg['text']}"
        else:
            beats.append(current)
            current = dict(seg)

    if current is not None:
        beats.append(current)

    return beats


def label(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def slug(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}m{total % 60:02d}s"


# ---------------------------------------------------------------------------
# STYLE BLOCKS
# ---------------------------------------------------------------------------


def load_style_blocks(prompt_path: Path, text_path_arg: str | None):
    """
    Load the plain style block, and the text variant if there is one.

    The text variant is found automatically by appending _text to the
    prompt filename, so style_c_ghibli.txt pairs with
    style_c_ghibli_text.txt. Override with --text-prompt-file. Styles
    with no text variant simply never render on-screen words.
    """
    if not prompt_path.exists():
        raise SystemExit(f"Prompt file not found: {prompt_path}")
    plain = prompt_path.read_text(encoding="utf-8-sig").rstrip() + "\n\n"

    if text_path_arg:
        text_path = Path(text_path_arg)
        if not text_path.exists():
            raise SystemExit(f"Text prompt file not found: {text_path}")
    else:
        text_path = prompt_path.with_name(prompt_path.stem + "_text.txt")
        if not text_path.exists():
            return plain, None, None

    text_block = text_path.read_text(encoding="utf-8-sig").rstrip() + "\n\n"
    if TEXT_PLACEHOLDER not in text_block:
        raise SystemExit(
            f"{text_path.name} has no {TEXT_PLACEHOLDER} placeholder.\n"
            "The on-screen words have nowhere to go."
        )
    return plain, text_block, text_path


def load_character(path: Path) -> str:
    """
    Read the character description, dropping # comment lines.

    Everything here is pasted into every image prompt, so notes to
    yourself have to be stripped or the model tries to draw them.
    """
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8-sig").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    return "\n".join(lines).strip()


RENDER_RULE = (
    "RENDER RULE, overrides everything above: every person is a plain stick "
    "figure. Empty white circle head, single-line body and limbs, two dot "
    "eyes, one curved mouth line. No hair, no skin tone, no clothing, no "
    "shading, no filled-in head."
)


def build_prompt(
    row: dict,
    plain: str,
    text_block: str | None,
    character: str = "",
) -> tuple[str, bool]:
    """
    Assemble the full prompt for one beat.

    Order matters: style block, then the scene, then the drawing convention,
    then the render rule last. Whatever sits at the end wins ties.
    """
    scene = (row.get("scene") or "").strip()
    narration = (row.get("narration") or "").strip()
    on_screen = (row.get("on_screen_text") or "").strip()
    subject = scene or narration

    if character:
        subject = f"{subject}\n\nDrawing convention:\n{character}"

    subject = f"{subject}\n\n{RENDER_RULE}"

    if on_screen and text_block:
        safe = on_screen.replace('"', "").replace("{", "").replace("}", "")
        return text_block.replace(TEXT_PLACEHOLDER, safe) + subject, True

    return plain + subject, False


# ---------------------------------------------------------------------------
# BEAT PLAN
# ---------------------------------------------------------------------------


def plan_path_for(srt_path: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return PLAN_DIR / f"{srt_path.stem}.tsv"


def write_plan(beats: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PLAN_FIELDS, delimiter="\t")
        writer.writeheader()
        for i, beat in enumerate(beats, start=1):
            writer.writerow(
                {
                    "n": i,
                    "timestamp": label(beat["start"]),
                    "file": f"{i:03d}_{slug(beat['start'])}.png",
                    "scene": "",
                    "on_screen_text": "",
                    "narration": beat["text"],
                }
            )


def read_plan(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))

    missing = [f for f in PLAN_FIELDS if rows and f not in rows[0]]
    if missing:
        raise SystemExit(
            f"{path.name} is missing columns: {', '.join(missing)}\n"
            "It was probably saved as CSV. Save as tab-separated."
        )
    return rows


def load_or_create_plan(beats: list[dict], path: Path) -> list[dict]:
    """
    Reuse the existing plan so hand-written scenes survive re-runs.
    Never overwrites. Delete the file to rebuild from the SRT.
    """
    if not path.exists():
        write_plan(beats, path)
        print(f"Plan    wrote {path} ({len(beats)} rows)")
        print("        Fill in the scene column, then run again.")
        return read_plan(path)

    rows = read_plan(path)
    print(f"Plan    {path} ({len(rows)} rows)")

    if len(rows) != len(beats):
        print(
            f"\nWARNING: the plan has {len(rows)} rows but this SRT produces "
            f"{len(beats)} beats.\n"
            f"         The transcript or the beat settings changed. Delete\n"
            f"         {path} to rebuild — you will lose your scenes."
        )
    return rows


# ---------------------------------------------------------------------------
# SCENE WRITING
# ---------------------------------------------------------------------------

DEFAULT_CHARACTER = (
    "Any person in this image is drawn as a plain stick figure: an empty "
    "white circle for the head, single lines for body, arms and legs, two "
    "dots for eyes, one short curved line for the mouth. No hair, no beard, "
    "no skin tone, no clothing, no shoes, no hands, no torso, no shading. "
    "The head circle is never filled with any color. This applies even if "
    "the scene text mentions a man, a woman, an age, a body, or clothes — "
    "ignore those words and draw the plain stick figure."
)

SCENE_SYSTEM = """\
You turn lines of a YouTube voiceover into scene descriptions for an image \
generator that draws crude stick-figure doodles. The channel is men's \
self-improvement and dating.
 
Your job is to draw the IDEA, not a person doing something.
 
THE CORE RULE
Each line makes a point. Find the object, comparison, or diagram that makes \
that point visible. A person performing an action is almost never the answer.
 
BAD  -> A man sits on a couch looking at his phone, frowning.
GOOD -> A grid of nine blank grey squares inside a phone outline, every \
square empty, no speech bubbles anywhere.
 
BAD  -> He walks into a gym feeling nervous.
GOOD -> A door with a large red question mark floating in front of it.
 
BAD  -> The man realizes he swiped on many people too.
GOOD -> A tall stack of cards all labeled RIGHT SWIPE with a big 60 beside it.
 
BAD  -> He feels the weight of past rejection.
GOOD -> A cardboard box overflowing with blank sheets of paper, labeled.
 
VISUAL VOCABULARY — reach for these first
- Objects: phone outline, envelope, calendar page, stopwatch, receipt, jar, \
box, door, sack, price tag, lottery ticket, counter, trash can
- Comparisons: balance scale, split frame with a divider, two piles of \
different sizes, before and after panels
- Diagrams: bar charts, loops of arrows, stacks, grids, a circled item in a list
- Marks: red arrows, red circles, red X, check marks, question marks
 
STICK FIGURES
You may use stick figures, but only as generic symbols — a figure buried in \
a pile, two figures on a bench, a figure walking through a door. Write \
"a stick figure", never "the man", "he", "she", or "the same man". Never \
give a figure an age, a body, hair, clothing, or a described face. There is \
no recurring character.
 
VARIETY
Consecutive scenes must not repeat the same object or layout. If the last \
scene used a phone, this one does not. Across a full script, no object \
should appear more than three times.
 
ALSO WRITE THE ON-SCREEN TEXT
For each line, give a short handwritten label that goes in the image, or an \
empty string if the image is clearer without one. Rules for labels: one to \
three words, ALL CAPS, spelled correctly, no punctuation beyond a question \
mark or a dollar sign. Roughly two thirds of beats should have one.
 
LENGTH
25 to 40 words per scene. Concrete and literal. Name the shapes.
 
Return ONLY a JSON array, in order, no markdown fences, no commentary:
[{"n": 1, "scene": "...", "on_screen_text": "..."}]"""


BANNED = re.compile(
    r"\b(the man|the same man|he |him |his |she |her |the guy|the character|"
    r"young man|a man\b|a woman\b|hoodie|jeans|sneakers|t-shirt|beard|"
    r"hair|skin|shirt)\b",
    re.I,
)


def _parse_scene_json(raw: str) -> dict[int, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Could not parse the scene response as JSON:\n{text[:400]}\n{exc}"
        ) from exc
    return {int(item["n"]): str(item["scene"]).strip() for item in data}


def _parse_scene_json(raw: str) -> dict[int, dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    import json

    data = json.loads(text)
    return {
        int(it["n"]): {
            "scene": str(it.get("scene", "")).strip(),
            "on_screen_text": str(it.get("on_screen_text", "")).strip(),
        }
        for it in data
    }


def write_scenes(
    client,
    rows: list[dict],
    plan_path,
    character: str,
    model: str,
    rewrite: bool,
    chunk_size: int = 20,
) -> list[dict]:
    """Fill scene and on_screen_text, validating against person-words."""
    targets = [r for r in rows if rewrite or not (r.get("scene") or "").strip()]
    if not targets:
        print("Scenes  already written, nothing to do")
        return rows

    print(f"Scenes  writing {len(targets)} with {model}")

    got_all: dict[int, dict] = {}
    tail: list[str] = []

    def ask(chunk, extra=""):
        lines = "\n".join(f'{r["n"]}. {r["narration"]}' for r in chunk)
        context = ""
        if tail:
            recent = "\n".join(f"- {t}" for t in tail[-4:])
            context = (
                f"\nScenes immediately before these. Do not reuse their "
                f"objects or layout:\n{recent}\n"
            )
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SCENE_SYSTEM},
                {
                    "role": "user",
                    "content": f"{context}{extra}\nWrite one scene for each "
                    f"numbered line:\n\n{lines}",
                },
            ],
            temperature=0.85,
        )
        return _parse_scene_json(resp.choices[0].message.content or "")

    for start in range(0, len(targets), chunk_size):
        chunk = targets[start : start + chunk_size]

        try:
            got = ask(chunk)
        except Exception as exc:
            raise SystemExit(f"Scene generation failed: {exc}") from exc

        bad = [n for n, v in got.items() if BANNED.search(v["scene"])]
        if bad:
            print(f"        {len(bad)} scenes used person-words, retrying")
            retry_rows = [r for r in chunk if int(r["n"]) in bad]
            try:
                fixed = ask(
                    retry_rows,
                    extra=(
                        "Your previous attempt at these described a person. "
                        "Do NOT write the man, he, his, she, her, hair, "
                        "clothing, or a body. Describe objects, comparisons, "
                        "or diagrams instead."
                    ),
                )
                got.update(fixed)
            except Exception:
                pass

        still_bad = [n for n, v in got.items() if BANNED.search(v["scene"])]
        if still_bad:
            print(f"        REVIEW BY HAND: rows {still_bad}")

        got_all.update(got)
        tail.extend(got[int(r["n"])]["scene"] for r in chunk if int(r["n"]) in got)
        print(f"        {min(start + chunk_size, len(targets))}/{len(targets)}")

    filled = 0
    for row in rows:
        hit = got_all.get(int(row["n"]))
        if hit and (rewrite or not (row.get("scene") or "").strip()):
            row["scene"] = hit["scene"]
            if not (row.get("on_screen_text") or "").strip():
                row["on_screen_text"] = hit["on_screen_text"]
            filled += 1

    with plan_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PLAN_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in PLAN_FIELDS} for r in rows)

    texted = sum(1 for r in rows if (r.get("on_screen_text") or "").strip())
    print(f"        wrote {filled} scenes, {texted} labels into {plan_path.name}")
    return rows


# ---------------------------------------------------------------------------
# GENERATION
# ---------------------------------------------------------------------------

RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError)


def generate_one(
    client: OpenAI,
    prompt: str,
    out_path: Path,
    model: str,
    size: str,
    quality: str,
    ref: Path | None = None,
    retries: int = RETRIES,
) -> bool:
    """
    Generate a single image. Returns True on success.

    Fails fast on anything that will not resolve by trying again — no
    credit balance, bad path, bad argument. Only retries transient errors.

    When ref is set the call routes through images.edit so every frame is
    generated against the same character reference.
    """
    for attempt in range(1, retries + 1):
        try:
            if ref:
                with ref.open("rb") as fh:
                    result = client.images.edit(
                        model=model,
                        image=[fh],
                        prompt=prompt,
                        size=size,
                        quality=quality,
                        n=1,
                    )
            else:
                result = client.images.generate(
                    model=model,
                    prompt=prompt,
                    size=size,
                    quality=quality,
                    n=1,
                )

            payload = getattr(result.data[0], "b64_json", None)
            if payload is None:
                raise RuntimeError("response contained no b64_json")

            out_path.write_bytes(base64.b64decode(payload))
            return True

        except OSError as exc:
            # Cannot read the ref or write the file. Retrying burns money.
            raise SystemExit(f"\nFile error on {out_path}\n  {exc}") from exc

        except RETRYABLE as exc:
            text = str(exc)
            if "insufficient_quota" in text or "credit_balance" in text:
                raise SystemExit(
                    "\nOut of API credits. Top up at "
                    "https://platform.openai.com/settings/organization/billing"
                ) from exc

            print(f"    attempt {attempt} failed: {exc}")
            if attempt < retries:
                wait = 2**attempt
                print(f"    retrying in {wait}s")
                time.sleep(wait)

        except Exception as exc:  # bad model name, bad size, refusal, etc.
            print(f"    failed: {exc}")
            return False

    return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


def find_srt(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise SystemExit(f"SRT not found: {path}")
        return path

    candidates = sorted(INPUT_DIR.glob("*.srt"))
    if not candidates:
        raise SystemExit(
            f"No .srt file in {INPUT_DIR}\n"
            "Drop your Whisper output there, or pass --srt PATH"
        )
    if len(candidates) > 1:
        names = "\n  ".join(c.name for c in candidates)
        raise SystemExit(
            f"Multiple .srt files in {INPUT_DIR}:\n  {names}\n"
            "Pass --srt PATH to pick one."
        )
    return candidates[0]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--srt", help="path to .srt (default: the one in input/)")
    p.add_argument("--out", help="output folder (default: output/<srt>__<style>/)")
    p.add_argument("--plan", help="beat plan TSV (default: plans/<srt>.tsv)")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--size", default=SIZE)
    p.add_argument("--quality", default=QUALITY, choices=["low", "medium", "high"])
    p.add_argument("--min-seconds", type=float, default=MIN_BEAT_SECONDS)
    p.add_argument("--max-chars", type=int, default=MAX_BEAT_CHARS)
    p.add_argument("--limit", type=int, help="only generate the first N beats")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="write the plan, show the breakdown, generate nothing",
    )
    p.add_argument(
        "--prompt-file",
        default=str(PROMPT_FILE),
        help="style block to prepend to every scene",
    )
    p.add_argument(
        "--text-prompt-file",
        help="text-variant style block (default: <prompt-file>_text.txt)",
    )
    p.add_argument(
        "--ref",
        help="character reference PNG, for consistency across frames",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="regenerate images even if the file already exists",
    )
    p.add_argument(
        "--write-scenes",
        action="store_true",
        help="fill blank scene cells with a text model, then stop",
    )
    p.add_argument(
        "--rewrite-scenes",
        action="store_true",
        help="with --write-scenes, overwrite existing scenes too",
    )
    p.add_argument("--scene-model", default=SCENE_MODEL)
    p.add_argument(
        "--character",
        help=f"file describing the recurring character "
        f"(default: {CHARACTER_FILE.name} if present)",
    )
    p.add_argument(
        "--no-character",
        action="store_true",
        help="do not append the character description to image prompts",
    )
    p.add_argument(
        "--allow-missing-scenes",
        action="store_true",
        help="generate from raw narration where the scene column is blank",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / ".env")

    srt_path = find_srt(args.srt)
    prompt_path = Path(args.prompt_file)
    plain, text_block, text_path = load_style_blocks(prompt_path, args.text_prompt_file)

    char_path = Path(args.character) if args.character else CHARACTER_FILE
    if args.no_character:
        character = ""
    elif char_path.exists():
        character = load_character(char_path)
    else:
        character = DEFAULT_CHARACTER

    ref = None
    if args.ref:
        ref = Path(args.ref)
        if not ref.exists():
            raise SystemExit(f"Reference image not found: {ref}")

    segments = parse_srt(srt_path)
    if not segments:
        raise SystemExit(f"No subtitle segments parsed from {srt_path}")

    beats = merge_into_beats(segments, args.min_seconds, args.max_chars)

    print(f"SRT     {srt_path.name}")
    print(f"Parsed  {len(segments)} segments -> {len(beats)} beats")

    rows = load_or_create_plan(beats, plan_path_for(srt_path, args.plan))

    if args.limit:
        rows = rows[: args.limit]
        print(f"Limit   first {len(rows)}")

    if args.out:
        out_dir = Path(args.out)
    else:
        # Folder name includes the style, so switching prompts never
        # collides with a previous run's images.
        out_dir = OUTPUT_DIR / f"{srt_path.stem}__{prompt_path.stem}"

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Prompt  {prompt_path.name}")
    if text_path:
        print(f"Text    {text_path.name}")
    else:
        print("Text    no text variant found, on-screen text will be ignored")
    if ref:
        print(f"Ref     {ref.name}")
    if not character:
        print("Char    off")
    elif char_path.exists():
        print(f"Char    {char_path.name} ({len(character)} chars)")
    else:
        print(f"Char    built-in default -- no {char_path} found")
    print(f"Output  {out_dir}")
    if args.force:
        print("Force   on, existing images will be overwritten")

    scened = sum(1 for r in rows if (r.get("scene") or "").strip())
    texted = sum(1 for r in rows if (r.get("on_screen_text") or "").strip())
    print(f"Scenes  {scened}/{len(rows)} written")
    print(
        f"Text    {texted}/{len(rows)} beats ({100 * texted / max(len(rows), 1):.0f}%)"
    )
    print()

    if args.write_scenes:
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit(
                "No OPENAI_API_KEY found.\n"
                "Copy .env.example to .env and paste your key into it.\n"
                f"Looked for: {ROOT / '.env'}"
            )
        print()
        write_scenes(
            OpenAI(),
            rows,
            plan_path_for(srt_path, args.plan),
            character or DEFAULT_CHARACTER,
            args.scene_model,
            args.rewrite_scenes,
        )
        print(
            "\nRead them over, edit anything that misses, then run again "
            "to generate."
        )
        return

    if args.dry_run:
        for row in rows:
            scene = (row.get("scene") or "").strip()
            mark = "  <-- no scene" if not scene else ""
            body = scene or row["narration"]
            print(f"[{row['n']:>3}] {row['timestamp']}  {body[:66]}{mark}")
            if (row.get("on_screen_text") or "").strip():
                print(f"      TEXT: \"{row['on_screen_text'].strip()}\"")
        print(f"\nDry run. {len(rows)} images would be generated.")
        if scened < len(rows):
            print(f"Fill in {len(rows) - scened} more scenes before generating.")
        return

    missing = len(rows) - scened
    if missing and not args.allow_missing_scenes:
        raise SystemExit(
            f"\n{missing} of {len(rows)} beats have no scene description.\n"
            "Those would be drawn from raw narration and will not match what\n"
            "is being said. Fill them in, or pass --allow-missing-scenes."
        )

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit(
            "No OPENAI_API_KEY found.\n"
            "Copy .env.example to .env and paste your key into it.\n"
            f"Looked for: {ROOT / '.env'}"
        )

    client = OpenAI()  # reads OPENAI_API_KEY from the environment

    manifest: list[str] = ["timestamp\tfile\tscene\ton_screen_text\tnarration"]
    failed: list[str] = []
    text_frames: list[str] = []
    made = 0

    for row in rows:
        stamp = row["timestamp"]
        out_path = out_dir / row["file"]
        scene = (row.get("scene") or "").strip()
        on_screen = (row.get("on_screen_text") or "").strip()
        prompt, has_text = build_prompt(row, plain, text_block, character)

        manifest.append(
            f"{stamp}\t{row['file']}\t{scene}\t{on_screen}\t{row['narration']}"
        )

        if out_path.exists() and not args.force:
            print(f"[{row['n']}/{len(rows)}] {stamp}  exists, skipping")
            continue

        preview = (scene or row["narration"])[:64].replace("\n", " ")
        marker = f'   TEXT: "{on_screen}"' if has_text else ""
        print(f"[{row['n']}/{len(rows)}] {stamp}  {preview}...{marker}")

        if generate_one(
            client,
            prompt,
            out_path,
            args.model,
            args.size,
            args.quality,
            ref=ref,
        ):
            made += 1
            if has_text:
                text_frames.append(f'{row["file"]}  "{on_screen}"')
        else:
            print(f"    gave up on {stamp}")
            failed.append(stamp)

    (out_dir / "manifest.tsv").write_text("\n".join(manifest), encoding="utf-8")

    print(f"\nDone. {made} generated, {len(failed)} failed.")
    if failed:
        print("Failed:", ", ".join(failed))

    if text_frames:
        print(f"\nCheck the spelling on these {len(text_frames)}:")
        for line in text_frames:
            print(f"  {line}")

    print(f"Files in {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume — finished images are skipped.")
        sys.exit(130)
