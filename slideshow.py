"""
Turn a folder of generated images into one video, timed to the voiceover.

The image filenames carry their own start time (014_02m17s.png). With just
a folder, each image holds until the next one starts. With --plan and
--words, each image's start time is pulled from a cue word in the TSV,
looked up in Whisper's per-word JSON, so the image lands exactly on the
word it illustrates.

Usage:
    python slideshow.py output/my_video__style_c_ghibli
    python slideshow.py <folder> --audio input/voiceover.mp3
    python slideshow.py <folder> --audio input/vo.mp3 \
        --plan plans/my_video.tsv --words input/my_video.json

Requires ffmpeg on your PATH.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

WIDTH = 1920
HEIGHT = 1080
FPS = 30
TAIL_SECONDS = 8.0
CUE_SEARCH_WINDOW = 4.0

NAME_RE = re.compile(r"^(\d+)_(\d+)m(\d+)s\.png$", re.IGNORECASE)


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg not found on your PATH.\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Mac:      brew install ffmpeg"
        )


def audio_duration(path: Path) -> float:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe not found. It ships with ffmpeg.")
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


def collect_images(folder: Path) -> list[tuple[Path, float, int]]:
    """Return [(path, filename_start_seconds, beat_number)]."""
    found: list[tuple[Path, float, int]] = []
    for path in folder.iterdir():
        m = NAME_RE.match(path.name)
        if m:
            n, mins, secs = m.groups()
            found.append((path, int(mins) * 60 + int(secs), int(n)))
    if not found:
        raise SystemExit(f"No timestamped images in {folder}")
    found.sort(key=lambda x: x[1])
    return found


def load_word_times(path: Path) -> list[tuple[str, float]]:
    """[(cleaned_word_lowercase, start_seconds)] in order, from Whisper JSON."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out: list[tuple[str, float]] = []
    for seg in data.get("segments", []):
        for w in seg.get("words") or []:
            token = (w.get("word") or "").strip().lower()
            token = re.sub(r"[^a-z0-9']+", "", token)
            if token:
                out.append((token, float(w["start"])))
    return out


def load_plan(path: Path) -> dict[int, str]:
    """{beat_number: cue_word_lowercase} from the TSV."""
    plan: dict[int, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if "cue_word" not in (reader.fieldnames or []):
            raise SystemExit(
                f"{path} has no cue_word column. Add one with the word each "
                f"beat should land on."
            )
        for row in reader:
            n = int(row["n"])
            cue = (row.get("cue_word") or "").strip().lower()
            plan[n] = cue
    return plan


def build_durations(
    images: list[tuple[Path, float, int]],
    end_seconds: float | None,
    tail: float,
    plan: dict[int, str] | None,
    word_times: list[tuple[str, float]] | None,
) -> list[tuple[Path, float]]:
    starts: list[float] = []

    for path, file_start, beat_n in images:
        cue_start = file_start
        if plan is not None and word_times is not None:
            cue = plan.get(beat_n, "")
            if cue:
                found = False
                for word, t in word_times:
                    if t < file_start - 0.25:
                        continue
                    if t > file_start + CUE_SEARCH_WINDOW:
                        break
                    if word == cue:
                        cue_start = t
                        found = True
                        break
                if not found:
                    print(
                        f"  beat {beat_n}: cue '{cue}' not found near "
                        f"{file_start:.1f}s, using filename time"
                    )
        starts.append(cue_start)

    for i in range(1, len(starts)):
        if starts[i] < starts[i - 1] + 0.3:
            starts[i] = starts[i - 1] + 0.3

    timed: list[tuple[Path, float]] = []
    for i, (path, _, _) in enumerate(images):
        if i + 1 < len(images):
            hold = starts[i + 1] - starts[i]
        elif end_seconds is not None:
            hold = end_seconds - starts[i]
        else:
            hold = tail
        if hold <= 0:
            print(f"  warning: {path.name} had non-positive duration")
            hold = 1.0
        timed.append((path, hold))

    return timed


def write_concat_file(timed: list[tuple[Path, float]], path: Path) -> None:
    lines: list[str] = []
    for image, hold in timed:
        safe = str(image.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
        lines.append(f"duration {hold:.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scale_filter(fit: str) -> str:
    if fit == "pad":
        return (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )
    return (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", help="folder of generated images")
    p.add_argument("--out", help="output file (default: <folder>/slideshow.mp4)")
    p.add_argument("--audio", help="voiceover file, sets the last image's end")
    p.add_argument("--plan", help="TSV with a cue_word column per beat")
    p.add_argument("--words", help="Whisper JSON written with --word_timestamps True")
    p.add_argument("--tail", type=float, default=TAIL_SECONDS)
    p.add_argument("--fit", choices=["crop", "pad"], default="crop")
    p.add_argument("--fps", type=int, default=FPS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        raise SystemExit(f"Not a folder: {folder}")

    images = collect_images(folder)
    print(f"Images  {len(images)} in {folder}")

    end_seconds = None
    if args.audio:
        audio = Path(args.audio)
        if not audio.exists():
            raise SystemExit(f"Audio not found: {audio}")
        require_ffmpeg()
        end_seconds = audio_duration(audio)
        print(f"Audio   {audio.name}, {end_seconds:.1f}s")

    plan = load_plan(Path(args.plan)) if args.plan else None
    word_times = load_word_times(Path(args.words)) if args.words else None
    if (plan is None) != (word_times is None):
        raise SystemExit("--plan and --words must be used together")
    if plan is not None:
        print(f"Plan    {len(plan)} beats | Words   {len(word_times)} tokens")

    timed = build_durations(images, end_seconds, args.tail, plan, word_times)
    total = sum(hold for _, hold in timed)
    print(f"Length  {int(total // 60)}m{int(total % 60):02d}s")

    if end_seconds and abs(total - end_seconds) > 1.0:
        print(f"        note: {abs(total - end_seconds):.1f}s off the audio")

    if args.dry_run:
        print()
        for image, hold in timed:
            print(f"  {image.name}  {hold:5.1f}s")
        print("\nDry run. Nothing rendered.")
        return

    require_ffmpeg()
    out_path = Path(args.out) if args.out else folder / "slideshow.mp4"
    concat_path = folder / "_concat.txt"
    write_concat_file(timed, concat_path)

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-vf",
        scale_filter(args.fit),
        "-r",
        str(args.fps),
        "-t",
        f"{total:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "18",
        "-preset",
        "medium",
        str(out_path),
    ]

    print(f"\nRendering {out_path.name} ...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    concat_path.unlink(missing_ok=True)

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise SystemExit(f"\nffmpeg failed:\n{tail}")

    size_mb = out_path.stat().st_size / 1_000_000
    print(f"Done. {out_path}  ({size_mb:.0f} MB)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
