"""
Turn a folder of generated images into one video, timed to the voiceover.

The image filenames carry their own start time (014_02m17s.png), so this
reads the folder, works out how long each image should hold, and renders a
single video file. Drop that one file into Clipchamp above your voiceover
track and every image is already in the right place.

Usage:
    python slideshow.py output/my_video__style_c_ghibli
    python slideshow.py <folder> --audio input/voiceover.mp3
    python slideshow.py <folder> --fit pad --out slideshow.mp4

The last image needs an end time, since nothing follows it. Pass --audio
and it uses the length of your voiceover, which is what you want. Without
it the last image holds for --tail seconds.

Requires ffmpeg on your PATH:
    winget install Gyan.FFmpeg          (Windows)
    brew install ffmpeg                 (Mac)
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

WIDTH = 1920
HEIGHT = 1080
FPS = 30
TAIL_SECONDS = 8.0

NAME_RE = re.compile(r"^(\d+)_(\d+)m(\d+)s\.png$", re.IGNORECASE)


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg not found on your PATH.\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Mac:      brew install ffmpeg\n"
            "Then close and reopen your terminal."
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


def collect_images(folder: Path) -> list[tuple[Path, float]]:
    """Return [(path, start_seconds)] sorted by start time."""
    found: list[tuple[Path, float]] = []

    for path in folder.iterdir():
        match = NAME_RE.match(path.name)
        if match:
            _, mins, secs = match.groups()
            found.append((path, int(mins) * 60 + int(secs)))

    if not found:
        raise SystemExit(
            f"No timestamped images in {folder}\n"
            "Expected names like 014_02m17s.png, which is what "
            "generate_images.py produces."
        )

    found.sort(key=lambda pair: pair[1])
    return found


def build_durations(
    images: list[tuple[Path, float]],
    end_seconds: float | None,
    tail: float,
) -> list[tuple[Path, float]]:
    """Each image holds until the next one starts."""
    timed: list[tuple[Path, float]] = []

    for i, (path, start) in enumerate(images):
        if i + 1 < len(images):
            hold = images[i + 1][1] - start
        elif end_seconds is not None:
            hold = end_seconds - start
        else:
            hold = tail

        if hold <= 0:
            print(f"  warning: {path.name} had a non-positive duration, " f"using 1s")
            hold = 1.0

        timed.append((path, hold))

    return timed


def write_concat_file(timed: list[tuple[Path, float]], path: Path) -> None:
    """
    Write a concat demuxer list.

    Do not append a repeated final entry here. That trick is for lists
    with no duration on the last file; with durations on every entry it
    tacks a whole extra segment onto the end. Exact total length is
    enforced with -t at render time instead.
    """
    lines: list[str] = []
    for image, hold in timed:
        # ffmpeg's concat demuxer wants forward slashes and escaped quotes
        safe = str(image.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
        lines.append(f"duration {hold:.3f}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scale_filter(fit: str) -> str:
    """
    Generated images are 1536x1024, which is 3:2. Video is 16:9.

    crop fills the frame and trims a little off the top and bottom.
    pad keeps the whole image and adds black bars at the sides.
    """
    if fit == "pad":
        return (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1"
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
    p.add_argument(
        "--tail",
        type=float,
        default=TAIL_SECONDS,
        help="how long the last image holds without --audio",
    )
    p.add_argument("--fit", choices=["crop", "pad"], default="crop")
    p.add_argument("--fps", type=int, default=FPS)
    p.add_argument(
        "--dry-run", action="store_true", help="print the timing, render nothing"
    )
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

    timed = build_durations(images, end_seconds, args.tail)
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

    # last = str(timed[-1][0].resolve()).replace("\\", "/").replace("'", r"'\''")
    # lines.append(f"file '{last}'")

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
    print("\nDrop it into Clipchamp above your voiceover track, " "starting at 0:00.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
