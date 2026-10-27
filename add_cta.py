"""
Burn a YouTube subscribe prompt into your videos.

Two jobs in one script:

    python add_cta.py --make-overlay
        Draws the sequence -- cursor travels in, clicks the white
        Subscribe button, button flips to a red Subscribed with a
        ringing notification bell -- and saves cta_subscribe.webm with a
        transparent background. Run this once.

    python add_cta.py --video output/clip.mp4 --at 0:45
        Burns that overlay into one video at 45 seconds.

    python add_cta.py --folder output/shorts --at 0:12
        Does every .mp4 in the folder. Originals are never touched --
        results land in a _cta subfolder.

Why not a MOGRT: those only open in Premiere and After Effects. A
transparent WebM drops into anything, and this script skips the editor
entirely.

Variants:
    --dark          start from the black button instead of the white one
    --seconds 5     stretch the whole sequence

Requires ffmpeg on your PATH and pillow:
    pip install pillow
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# DEFAULTS
# ---------------------------------------------------------------------------

OVERLAY_FILE = "cta_subscribe.webm"
FPS = 30
SECONDS = 4.0
W, H = 900, 420  # overlay canvas; scaled to the video at burn time

WHITE_BG = (255, 255, 255, 255)  # the unsubscribed button
WHITE_TEXT = (15, 15, 15, 255)
HOVER_BG = (222, 222, 222, 255)  # pressed
SUBBED_BG = (204, 0, 0, 255)  # after the click
SUBBED_TEXT = (255, 255, 255, 255)
DARK_BG = (15, 15, 15, 255)  # --dark starts black instead of white
DARK_TEXT = (255, 255, 255, 255)
CURSOR_FILL = (255, 255, 255, 255)
CURSOR_LINE = (0, 0, 0, 255)
SPARK = (170, 170, 170, 255)

# Overlay width as a fraction of the video width, and where its top-left
# corner sits. Defaults keep it clear of the caption zone on Shorts.
SCALE = 0.66
POS_X = 0.20
POS_Y = 0.58

# First match wins. The bundled Roboto is what YouTube actually uses;
# the rest are close-enough system fallbacks.
FONT_CANDIDATES = [
    "fonts/Roboto-Medium.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise SystemExit(
            f"{tool} not found on your PATH.\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  Mac:      brew install ffmpeg\n"
            "Then close and reopen your terminal."
        )


def load_font(size: int, extra: str | None = None):
    tried = ([extra] if extra else []) + FONT_CANDIDATES
    for path in tried:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    print("  note: no TrueType font found, falling back to a bitmap font.")
    print("        Drop Roboto-Medium.ttf in a fonts/ folder for the real look.")
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# DRAWING
# ---------------------------------------------------------------------------


def draw_cursor(d, x, y, s=1.0, clicking=False):
    """Standard arrow pointer. x, y is the tip."""
    pts = [
        (0, 0),
        (0, 17.5),
        (4.2, 13.7),
        (7.0, 19.8),
        (10.1, 18.4),
        (7.4, 12.4),
        (12.6, 12.0),
    ]
    sc = 2.4 * s
    poly = [(x + px * sc, y + py * sc) for px, py in pts]

    # soft shadow so the cursor reads on any footage
    d.polygon([(px + 2, py + 2) for px, py in poly], fill=(0, 0, 0, 70))
    d.polygon(poly, fill=CURSOR_FILL)
    d.line(poly + [poly[0]], fill=CURSOR_LINE, width=max(2, int(2 * s)))

    if clicking:
        for ang in (-82, -48, -14):
            r = math.radians(ang)
            x0, y0 = x + 3 * sc, y - 2 * sc
            d.line(
                [
                    (x0 + math.cos(r) * 12 * s, y0 + math.sin(r) * 12 * s),
                    (x0 + math.cos(r) * 24 * s, y0 + math.sin(r) * 24 * s),
                ],
                fill=SPARK,
                width=max(2, int(2.8 * s)),
            )


def draw_bell(d, cx, cy, size, color, wiggle=0.0):
    """Notification bell, rocking on its hanger."""
    s = size / 24.0
    a = math.radians(wiggle * 14)
    pivot_y = cy - 10 * s

    def rot(px, py):
        dx, dy = px - cx, py - pivot_y
        return (
            cx + dx * math.cos(a) - dy * math.sin(a),
            pivot_y + dx * math.sin(a) + dy * math.cos(a),
        )

    body = [
        (-9, 6),
        (-9, -2),
        (-7, -7),
        (-3, -10),
        (3, -10),
        (7, -7),
        (9, -2),
        (9, 6),
        (12, 10),
        (-12, 10),
    ]
    d.polygon([rot(cx + px * s, cy + py * s) for px, py in body], fill=color)

    kx, ky = rot(cx, cy - 11.5 * s)
    d.ellipse([kx - 2.4 * s, ky - 2.4 * s, kx + 2.4 * s, ky + 2.4 * s], fill=color)
    clx, cly = rot(cx, cy + 13.5 * s)
    d.ellipse([clx - 3.2 * s, cly - 3.2 * s, clx + 3.2 * s, cly + 3.2 * s], fill=color)


def draw_chevron(d, cx, cy, size, color):
    s = size / 24.0
    d.line(
        [(cx - 5 * s, cy - 2.5 * s), (cx, cy + 2.5 * s), (cx + 5 * s, cy - 2.5 * s)],
        fill=color,
        width=max(2, int(2.6 * s)),
        joint="curve",
    )


def frame(i: int, total: int, dark: bool, font) -> Image.Image:
    """
    One frame of the sequence:
      cursor travels in -> presses -> button flips to red Subscribed
      -> bell rings.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    t = i / total

    travel_end, click_end = 0.46, 0.56
    if t < travel_end:
        e = t / travel_end
        travel, press, subbed = 1 - (1 - e) ** 3, 0.0, 0.0
    elif t < click_end:
        travel = 0.0
        press = math.sin((t - travel_end) / (click_end - travel_end) * math.pi)
        subbed = 0.0
    else:
        travel, press = 0.0, 0.0
        subbed = min(1.0, (t - click_end) / 0.14)

    done = subbed > 0.5
    label = "Subscribed" if done else "Subscribe"
    tb = d.textbbox((0, 0), label, font=font)
    text_w, text_h = tb[2] - tb[0], tb[3] - tb[1]

    bh = 100
    bw = text_w + 72 + (56 if done else 0)  # room for bell + chevron
    cx, cy = W * 0.42, H * 0.34
    box = [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2]

    if done:
        bg, fg = SUBBED_BG, SUBBED_TEXT
    elif press > 0.3:
        bg, fg = (HOVER_BG, WHITE_TEXT) if not dark else ((49, 49, 49, 255), DARK_TEXT)
    else:
        bg, fg = (DARK_BG, DARK_TEXT) if dark else (WHITE_BG, WHITE_TEXT)

    d.rounded_rectangle(box, radius=bh // 2, fill=bg)

    if done:
        # bell rings on arrival, then settles
        ring = math.sin((t - click_end) * 34) * math.exp(-(t - click_end) * 9)
        bell_x = cx - bw / 2 + 36
        draw_bell(d, bell_x, cy, 30, fg, wiggle=ring)
        d.text((bell_x + 26, cy - text_h / 2 - tb[1]), label, font=font, fill=fg)
        draw_chevron(d, cx + bw / 2 - 28, cy + 2, 26, fg)
    else:
        d.text(
            (cx - text_w / 2 - tb[0], cy - text_h / 2 - tb[1]),
            label,
            font=font,
            fill=fg,
        )

    # long diagonal travel in from the bottom-right corner
    tx, ty = cx + bw / 2 - 30, cy + bh * 0.12
    sx, sy = W - 40, H - 30
    px = tx + (sx - tx) * travel
    py = ty + (sy - ty) * travel + press * 6
    draw_cursor(d, px, py, 1.0, press > 0.45)

    return img


def make_overlay(
    path: Path, dark: bool, seconds: float, fps: int, font_path: str | None
) -> None:
    require("ffmpeg")
    font = load_font(42, font_path)
    total = int(seconds * fps)
    print(f"Drawing {total} frames...")

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(total):
            frame(i, total, dark, font).save(Path(tmp) / f"{i:04d}.png")

        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-framerate",
            str(fps),
            "-i",
            str(Path(tmp) / "%04d.png"),
            "-c:v",
            "libvpx-vp9",
            "-pix_fmt",
            "yuva420p",  # the 'a' is what keeps transparency
            "-b:v",
            "2M",
            "-auto-alt-ref",
            "0",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"ffmpeg failed:\n{result.stderr[-800:]}")

    print(
        f"Wrote {path} ({path.stat().st_size / 1000:.0f} KB, "
        f"{seconds}s, transparent)"
    )
    print("\nThat file also drops straight into CapCut as an overlay if you")
    print("would rather place it by hand.")


# ---------------------------------------------------------------------------
# BURNING
# ---------------------------------------------------------------------------


def parse_timecode(text: str) -> float:
    text = text.strip()
    if ":" in text:
        mins, _, secs = text.partition(":")
        return int(mins) * 60 + float(secs)
    return float(text)


def probe(path: Path, field: str) -> str:
    stream = field in ("width", "height")
    out = subprocess.run(
        ["ffprobe", "-v", "error"]
        + (["-select_streams", "v:0"] if stream else [])
        + [
            "-show_entries",
            f"{'stream' if stream else 'format'}={field}",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"Could not read {path}\n{out.stderr}")
    return out.stdout.strip().splitlines()[0]


def burn(
    video: Path,
    overlay: Path,
    at: float,
    out_path: Path,
    scale: float,
    pos: tuple[float, float],
    seconds: float,
) -> bool:
    """Overlay the CTA onto one video, visible from `at` for `seconds`."""
    width = int(int(probe(video, "width")) * scale)
    px, py = pos
    end = at + seconds

    # setpts shifts the overlay's own clock so frame 0 of the animation
    # lands at `at`. Without it the overlay plays from the video's t=0
    # and has already finished by the time the enable window opens --
    # you would only ever see its last frame.
    filters = (
        f"[1:v]scale={width}:-1,setpts=PTS-STARTPTS+{at:.3f}/TB[cta];"
        f"[0:v][cta]overlay="
        f"x=main_w*{px}:y=main_h*{py}:"
        f"enable='between(t,{at:.2f},{end:.2f})'"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(video),
        "-c:v",
        "libvpx-vp9",
        "-i",
        str(overlay),
        "-filter_complex",
        filters,
        "-map",
        "0:a?",
        "-c:a",
        "copy",
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()
        print(f"  failed: {tail[-1] if tail else 'unknown error'}")
        return False
    return True


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--make-overlay", action="store_true", help="draw the overlay file, then stop"
    )
    p.add_argument(
        "--dark",
        action="store_true",
        help="start from the black button instead of the white one",
    )
    p.add_argument("--font", help="path to a .ttf to use for the label")
    p.add_argument("--overlay", default=OVERLAY_FILE)
    p.add_argument("--seconds", type=float, default=SECONDS)
    p.add_argument("--fps", type=int, default=FPS)

    p.add_argument("--video", help="one video to burn into")
    p.add_argument("--folder", help="every .mp4 in this folder")
    p.add_argument(
        "--at",
        default="0:10",
        help="when it appears, e.g. 0:45. "
        "'end' places it just before the video ends.",
    )
    p.add_argument(
        "--scale",
        type=float,
        default=SCALE,
        help="overlay width as a fraction of the video width",
    )
    p.add_argument("--x", type=float, default=POS_X)
    p.add_argument("--y", type=float, default=POS_Y)
    args = p.parse_args()

    overlay = Path(args.overlay)

    if args.make_overlay:
        make_overlay(overlay, args.dark, args.seconds, args.fps, args.font)
        return

    if not args.video and not args.folder:
        raise SystemExit(
            "Nothing to do. Make the overlay first:\n"
            "  python add_cta.py --make-overlay\n"
            "then point at a video with --video or --folder. See --help."
        )

    require("ffmpeg")
    require("ffprobe")

    if not overlay.exists():
        raise SystemExit(
            f"Overlay not found: {overlay}\n"
            "Make it first:  python add_cta.py --make-overlay"
        )

    if args.video:
        videos = [Path(args.video)]
        out_dir = videos[0].parent / "_cta"
    else:
        folder = Path(args.folder)
        if not folder.is_dir():
            raise SystemExit(f"Not a folder: {folder}")
        videos = sorted(v for v in folder.glob("*.mp4"))
        if not videos:
            raise SystemExit(f"No .mp4 files in {folder}")
        out_dir = folder / "_cta"

    out_dir.mkdir(exist_ok=True)
    print(f"Overlay {overlay.name}")
    print(f"Videos  {len(videos)}")
    print(f"Output  {out_dir}  (originals untouched)")
    print()

    done = failed = 0
    for video in videos:
        if not video.exists():
            print(f"  missing: {video}")
            failed += 1
            continue

        duration = float(probe(video, "duration"))
        if args.at == "end":
            at = max(0.0, duration - args.seconds - 1.0)
        else:
            at = parse_timecode(args.at)

        if at + args.seconds > duration:
            at = max(0.0, duration - args.seconds)
            print(f"  {video.name}: shifted to {at:.1f}s to fit")

        print(f"  {video.name}  @ {at:.1f}s")
        if burn(
            video,
            overlay,
            at,
            out_dir / video.name,
            args.scale,
            (args.x, args.y),
            args.seconds,
        ):
            done += 1
        else:
            failed += 1

    print(f"\nDone. {done} written, {failed} failed.")
    print(f"Files in {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
