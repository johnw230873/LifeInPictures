"""Memorial Slideshow Creator.

A GUI app that turns a folder of photos plus a list of songs into a gentle,
dignified MP4 slideshow suitable for a funeral or memorial service.

Run:  python memorial_slideshow.py
"""

from __future__ import annotations

import html
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # pragma: no cover - fall back to ffmpeg on PATH
    FFMPEG = "ffmpeg"

try:
    import pillow_heif  # optional HEIC (iPhone) support

    pillow_heif.register_heif_opener()
    HEIC = True
except Exception:
    HEIC = False

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
if HEIC:
    IMAGE_EXTS |= {".heic", ".heif"}

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma", ".opus"}

MAX_TRANSITION = 10.0      # hard limit requested: no transition longer than 10 s
MIN_SLIDE_SECONDS = 5.0    # below this, photos get grouped onto shared slides
MAX_PER_SLIDE = 6
SONG_CROSSFADE = 3.0       # seconds of audio crossfade between songs
AUDIO_FADE_IN = 2.0
AUDIO_FADE_OUT = 10.0      # the last song fades out over this many seconds
MAX_SPEEDUP = 1.1          # music may be played up to 10% faster if photos are few
COMFORT_SLIDE_SECONDS = 12.0  # above this per slide, the music is sped up
MIN_MANUAL_SPEED = 0.80    # range of the manual speed slider
MAX_MANUAL_SPEED = 1.20
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    photos_dir: str
    songs: list[str]
    output: str
    width: int = 1920
    height: int = 1080
    fps: int = 25
    transition: float = 2.5
    style: str = "Mixed (crossfade + dip to black)"
    order: str = "Filename"
    title: str = ""
    subtitle: str = ""
    closing: str = ""
    allow_speedup: bool = True
    manual_speed: float | None = None  # when set, overrides the automatic speed-up
    opening_photo: str = ""            # shown first (with the title text, if any)
    closing_photo: str = ""            # shown last (with the closing text, if any)
    write_titles: bool = True          # write photo titles as pen-style ink on the slides


@dataclass
class Slide:
    photos: list[Path] = field(default_factory=list)
    text: tuple[str, str] | None = None   # (main, sub) for title/closing cards
    duration: float = 0.0
    start: float = 0.0
    dip_to_black: bool = False            # transition INTO this slide
    layout: str = ""
    seed: int = 0
    captions: list[str] = field(default_factory=list)
    bookend: bool = False                 # opening/closing photo shown on its own


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, "-hide_banner", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW)


def media_duration(path: str | Path) -> float:
    out = run_ffmpeg(["-i", str(path)]).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise RuntimeError(f"Could not read duration of {path}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def find_font(size: int, italic: bool = False) -> ImageFont.FreeTypeFont:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    names = (["georgiai.ttf", "timesi.ttf"] if italic else ["georgia.ttf", "times.ttf"]) + [
        "DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "arial.ttf"]
    for name in names:
        for p in (Path(windir) / "Fonts" / name, Path(name)):
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:          # Pillow < 10.1 has no size argument
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# Music
# --------------------------------------------------------------------------- #
def obtain_song(entry: str, dest: Path, log) -> Path:
    """Return a local audio file for a song title, URL or local path."""
    entry = entry.strip().strip('"')
    if os.path.isfile(entry):
        log(f"Using local file: {entry}")
        return Path(entry)

    import yt_dlp

    dest.mkdir(parents=True, exist_ok=True)
    # '%' would be expanded by yt-dlp's output template, so the file couldn't be found
    safe = re.sub(r'[\\/:*?"<>|%]+', "_", entry)[:80]
    for ext in AUDIO_EXTS:
        cached = dest / f"{safe}{ext}"
        if cached.exists():
            log(f"Already downloaded: {cached.name}")
            return cached

    is_url = re.match(r"https?://", entry) is not None
    queries = [entry] if is_url else [f"ytsearch1:{entry} audio", f"scsearch1:{entry}"]
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / f"{safe}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": FFMPEG,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "192"}],
    }
    last_err = None
    for q in queries:
        try:
            log(f"Searching online: {q}")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(q, download=True)
                if info and "entries" in info:
                    entries = [e for e in info["entries"] if e]
                    if not entries:
                        raise RuntimeError("no results")
                    info = entries[0]
                log(f"  Found: {info.get('title')} ({info.get('uploader') or info.get('channel') or '?'})")
            out = dest / f"{safe}.mp3"
            if out.exists():
                return out
            for ext in AUDIO_EXTS:
                if (dest / f"{safe}{ext}").exists():
                    return dest / f"{safe}{ext}"
            raise RuntimeError("download finished but file not found")
        except Exception as exc:  # try next source
            last_err = exc
            log(f"  Failed: {exc}")
    raise RuntimeError(f"Could not download '{entry}': {last_err}")


def soundtrack_length(durs: list[float], speed: float = 1.0) -> tuple[float, float]:
    """Return (total length, crossfade) for tracks joined at the given speed."""
    durs = [d / speed for d in durs]
    cf = min(SONG_CROSSFADE, *(d / 3 for d in durs)) if len(durs) > 1 else 0
    return sum(durs) - cf * (len(durs) - 1), cf


def build_soundtrack(tracks: list[Path], out: Path, speed: float = 1.0) -> float:
    """Join tracks with gentle crossfades, optionally sped up (pitch preserved),
    and fade the last song out at the end. Returns final duration in seconds."""
    durs = [media_duration(t) for t in tracks]
    total, cf = soundtrack_length(durs, speed)

    args = []
    for t in tracks:
        args += ["-i", str(t)]
    tempo = f",atempo={speed:.4f}" if abs(speed - 1.0) > 1e-3 else ""
    parts = [f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo{tempo}[a{i}]"
             for i in range(len(tracks))]
    cur = "a0"
    for i in range(1, len(tracks)):
        nxt = f"x{i}"
        parts.append(f"[{cur}][a{i}]acrossfade=d={cf:.3f}:c1=tri:c2=tri[{nxt}]")
        cur = nxt
    fade_out = min(AUDIO_FADE_OUT, durs[-1] / speed / 3, total / 4)
    parts.append(f"[{cur}]afade=t=in:d={min(AUDIO_FADE_IN, total / 4):.3f},"
                 f"afade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f}[out]")
    res = run_ffmpeg(["-y", *args, "-filter_complex", ";".join(parts), "-map", "[out]",
                      "-c:a", "aac", "-b:a", "192k", str(out)])
    if res.returncode != 0:
        raise RuntimeError("Audio mixing failed:\n" + res.stderr[-2000:])
    return media_duration(out)


# --------------------------------------------------------------------------- #
# Photos and slide planning
# --------------------------------------------------------------------------- #
def date_taken(p: Path) -> str:
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            val = exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if val:
                return str(val)
    except Exception:
        pass
    return "9999"  # undated photos go last


def list_photos(folder: str, order: str) -> list[Path]:
    files = [p for p in Path(folder).iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    natural = lambda p: [int(t) if t.isdigit() else t.lower()
                         for t in re.split(r"(\d+)", p.name)]
    files.sort(key=natural)
    if order.startswith("Date"):
        files.sort(key=date_taken)  # stable: filename order breaks ties
    return files


def aspect_of(p: Path) -> float:
    try:
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            return im.width / im.height
    except Exception:
        return 1.5


CAPTION_FILES = ("captions.txt", "titles.txt")
# Descriptions cameras write automatically, which are not real titles
JUNK_TITLE = re.compile(r"^(olympus digital camera|sony dsc|default|exif_jpeg_picture|dcim.*|"
                        r"image|picture|photo|untitled|digital camera|\W*)$", re.I)


def _clean_title(text) -> str:
    if isinstance(text, (bytes, tuple, list)):
        text = bytes(text).decode("utf-16-le" if b"\x00" in bytes(text) else "utf-8", "ignore")
    text = " ".join(str(text).replace("\x00", "").split())
    return "" if JUNK_TITLE.match(text) else text[:150]


def photo_title(p: Path) -> str:
    """Title stored in the photo itself (Windows 'Title', XMP dc:title or description)."""
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            t = _clean_title(exif.get(0x9C9B, ""))          # XPTitle (Windows Explorer)
            if t:
                return t
            xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp")
            if xmp:
                if isinstance(xmp, bytes):
                    xmp = xmp.decode("utf-8", "ignore")
                m = re.search(r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", xmp, re.S)
                t = _clean_title(html.unescape(m.group(1))) if m else ""
                if t:
                    return t
            return _clean_title(exif.get(270, ""))          # ImageDescription
    except Exception:
        return ""


def read_captions(folder: str, photos: list[Path]) -> dict[Path, str]:
    """Titles from captions.txt ('filename: title' per line) or the photos' own metadata."""
    caps: dict[Path, str] = {}
    by_name = {p.name.lower(): p for p in photos} | {p.stem.lower(): p for p in photos}
    for fname in CAPTION_FILES:
        f = Path(folder) / fname
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.strip().startswith("#"):
                continue
            m = re.match(r"\s*(.+?)\s*[|:=\t]\s*(.+?)\s*$", line)
            if m and m.group(1).lower() in by_name:
                title = " ".join(m.group(2).split())[:150]
                if title:  # an empty title must not hide the photo's own Title
                    caps.setdefault(by_name[m.group(1).lower()], title)
    for p in photos:
        if p not in caps:
            t = photo_title(p)
            if t:
                caps[p] = t
    return caps


def group_photos(photos: list[Path], aspects: dict, per_slide: int) -> list[list[Path]]:
    if per_slide == 1:
        # One photo per slide, but pair neighbouring portrait photos side by side
        groups, i = [], 0
        while i < len(photos):
            if (i + 1 < len(photos) and aspects[photos[i]] < 0.9
                    and aspects[photos[i + 1]] < 0.9):
                groups.append(photos[i:i + 2])
                i += 2
            else:
                groups.append([photos[i]])
                i += 1
        return groups
    n_groups = math.ceil(len(photos) / per_slide)
    base, extra = divmod(len(photos), n_groups)
    groups, i = [], 0
    for g in range(n_groups):
        size = base + (1 if g < extra else 0)
        groups.append(photos[i:i + size])
        i += size
    return groups


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def split_bookends(photos: list[Path], opening: str, closing: str
                   ) -> tuple[Path | None, list[Path], Path | None]:
    """Separate the chosen opening/closing photos from the rest so none is shown twice."""
    first = Path(opening) if opening and Path(opening).is_file() else None
    last = Path(closing) if closing and Path(closing).is_file() else None
    middle = [p for p in photos if not any(b and _same_file(p, b) for b in (first, last))]
    return first, middle, last


def n_bookend_slides(title: bool, closing_text: bool, first, last) -> int:
    return int(bool(title or first)) + int(bool(closing_text or last))


def plan_slides(cfg: Config, photos: list[Path], total: float, log) -> tuple[list[Slide], float]:
    first, photos, last = split_bookends(photos, cfg.opening_photo, cfg.closing_photo)
    has_title = bool(cfg.title or cfg.subtitle)
    aspects = {p: aspect_of(p) for p in photos + [b for b in (first, last) if b]}
    cards = n_bookend_slides(has_title, bool(cfg.closing), first, last)
    d = max(0.5, min(cfg.transition, MAX_TRANSITION))

    groups = []
    for per in range(1, MAX_PER_SLIDE + 1):
        groups = group_photos(photos, aspects, per)
        n = len(groups) + cards
        if not photos or total / n >= MIN_SLIDE_SECONDS or per == MAX_PER_SLIDE:
            break
    if per > 1:
        log(f"Music is short for {len(photos)} photos - showing up to {per} photos per slide.")

    slides: list[Slide] = []
    if has_title:
        slides.append(Slide(text=(cfg.title, cfg.subtitle), photos=[first] if first else []))
    elif first:
        slides.append(Slide(photos=[first], bookend=True))
    slides += [Slide(photos=g) for g in groups]
    if cfg.closing:
        slides.append(Slide(text=(cfg.closing, ""), photos=[last] if last else []))
    elif last:
        slides.append(Slide(photos=[last], bookend=True))
    if not slides:
        raise RuntimeError("There are no photos to show.")
    if first or last:
        log("Opening/closing photo(s) added" + (" with the title/closing text." if
                                                 (first and has_title) or (last and cfg.closing) else "."))

    # Multi-photo slides get proportionally more time so each photo can be seen;
    # the opening and closing photos linger a little longer.
    def weight(s: Slide) -> float:
        if s.text:
            return 1.6 if s.photos else 0.8
        if s.bookend:
            return 1.5
        return 1.0 + 0.4 * (len(s.photos) - 1)
    weights = [weight(s) for s in slides]
    n = len(slides)

    def durations(trans):
        # sum(D) - (n-1)*trans == total  ->  every slide shows `trans` + its share
        return [trans + (total - trans) * w / sum(weights) for w in weights]

    durs = durations(d)
    # Cap transitions at 40% of the shortest slide; iterate since shortening d also shortens slides.
    requested_d = d
    for _ in range(20):
        if n < 2 or d <= 0.5 or d <= 0.4 * min(durs):
            break
        d = max(0.5, 0.4 * min(durs))
        durs = durations(d)
    if n > 1 and d > 0.5 * total:          # extremely short soundtrack
        d = max(0.1, 0.5 * total)
        durs = durations(d)
    if d < requested_d - 1e-9:
        log(f"Transition shortened to {d:.1f}s to fit all photos.")
    t = 0.0
    captions = read_captions(cfg.photos_dir, photos + [b for b in (first, last) if b]) \
        if cfg.write_titles else {}
    if captions:
        log(f"{len(captions)} photo(s) have a title - it will be handwritten under the photo.")
    elif not cfg.write_titles:
        log("Photo titles are turned off - photos will be shown without writing.")
    rng = random.Random(len(photos) * 7919 + 17)
    prev = ""
    for i, (s, dur) in enumerate(zip(slides, durs)):
        if s.photos and not s.text:
            s.seed = rng.randrange(1 << 30)
            s.captions = [captions.get(p, "") for p in s.photos]
            if s.bookend:
                # A calm, simple presentation for the first and last photo
                s.layout = "polaroid" if s.captions[0] else "centre"
            else:
                s.layout = choose_layout([aspects[p] for p in s.photos], prev, rng, any(s.captions))
            prev = s.layout
        s.duration, s.start = dur, t
        t += dur - d
        if cfg.style.startswith("Dip"):
            s.dip_to_black = True
        elif cfg.style.startswith("Mixed"):
            # Gentle pause to black every 6 slides and around title/closing cards
            s.dip_to_black = (i % 6 == 0) or s.text is not None or (
                i > 0 and slides[i - 1].text is not None)
    return slides, d


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
KB = 1.08  # canvas over-size used for slow Ken Burns zoom/pan


def load_photo(p: Path, max_w: int, max_h: int) -> Image.Image:
    with Image.open(p) as src:
        im = ImageOps.exif_transpose(src).convert("RGB")
    im.thumbnail((max_w, max_h), Image.LANCZOS)
    return im


def fit(im: Image.Image, w: int, h: int) -> Image.Image:
    scale = min(w / im.width, h / im.height)
    return im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                     Image.LANCZOS)


PRINT_BORDER = (235, 230, 220)
POLAROID_BORDER = (244, 241, 234)


def background_from(im: Image.Image, w: int, h: int) -> Image.Image:
    bg = ImageOps.fit(im, (w // 8, h // 8), Image.BILINEAR)
    bg = bg.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.BILINEAR)
    return Image.blend(bg, Image.new("RGB", (w, h), (12, 12, 14)), 0.62)


def grid_for(n: int) -> list[int]:
    return {1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [3, 2], 6: [3, 3]}.get(n, [n])


# A "box" places one photo: centre x/y, maximum width/height, tilt in degrees, style.
Box = tuple[float, float, float, float, float, str]


def layout_options(aspects: list[float], captioned: bool = False) -> list[str]:
    """Layouts that suit these photos. Every layout gives each photo its own space, so
    photos never overlap. Captioned slides avoid full-screen (nowhere to write) and the
    tighter layouts that leave little room for writing."""
    n = len(aspects)
    if n == 1:
        opts = ["centre", "polaroid", "offset"]
        if aspects[0] >= 1.45 and not captioned:  # wide photos can fill the screen
            opts.append("full")
        return opts
    if n == 2:
        opts = ["side", "feature"] + ([] if captioned else ["polaroids"])
        if all(a > 1.1 for a in aspects) and not captioned:
            opts.append("diagonal")
        return opts
    opts = ["grid", "feature"] if n <= 4 else ["grid"]
    return opts if captioned else opts + ["scatter"]


def choose_layout(aspects: list[float], prev: str, rng, captioned: bool = False) -> str:
    opts = layout_options(aspects, captioned)
    fresh = [o for o in opts if o != prev]
    return rng.choice(fresh or opts)


def layout_boxes(layout: str, aspects: list[float], W: int, H: int, rng) -> list[Box]:
    n = len(aspects)
    m = H * 0.085          # outer margin (kept clear of the slow zoom crop)
    gap = H * 0.035
    tilt = lambda lo, hi: rng.choice([-1, 1]) * rng.uniform(lo, hi)

    if layout == "polaroid":
        return [(W / 2 + rng.uniform(-0.06, 0.06) * W, H / 2, W * 0.62, H - 2 * m, tilt(1.5, 3.5), "polaroid")]
    if layout == "offset":
        side = rng.choice([-1, 1])
        return [(W / 2 + side * W * 0.14, H / 2 + rng.uniform(-0.03, 0.03) * H,
                 W * 0.6, H - 2.4 * m, tilt(1, 3), "print")]
    if layout == "full":
        return [(W / 2, H / 2, W, H, 0, "full")]
    if layout == "side":
        cw = (W - 2 * m - gap) / 2
        lift = H * rng.uniform(0.02, 0.05) * rng.choice([-1, 1])
        return [(m + cw / 2, H / 2 - lift, cw, H - 2 * m - abs(lift) * 2, tilt(0, 1.5), "print"),
                (W - m - cw / 2, H / 2 + lift, cw, H - 2 * m - abs(lift) * 2, tilt(0, 1.5), "print")]
    if layout == "polaroids":
        # Two tilted polaroids laid next to each other, each in its own half
        cw = (W - 2 * m - gap * 2) / 2
        a = rng.uniform(2, 4)
        lift = H * rng.uniform(0.02, 0.04)
        return [(m + cw / 2, H / 2 - lift, cw, H - 2 * m - 2 * lift, -a, "polaroid"),
                (W - m - cw / 2, H / 2 + lift, cw, H - 2 * m - 2 * lift, a * rng.uniform(0.6, 1.0), "polaroid")]
    if layout == "diagonal":
        # Upper and lower halves, offset sideways (the halves never share space)
        bw, bh = W * 0.56, (H - 2 * m - gap) / 2
        flip = rng.choice([-1, 1])
        return [(W / 2 - flip * W * 0.17, m + bh / 2, bw, bh, tilt(0.5, 2), "print"),
                (W / 2 + flip * W * 0.17, H - m - bh / 2, bw, bh, tilt(0.5, 2), "print")]
    if layout == "feature":
        # One large photo with the rest stacked in a column beside it
        big_w = (W - 2 * m - gap) * 0.62
        col_w = W - 2 * m - gap - big_w
        left = rng.random() < 0.5
        bx = m + big_w / 2 if left else W - m - big_w / 2
        cx = W - m - col_w / 2 if left else m + col_w / 2
        k = n - 1
        ch = (H - 2 * m - gap * (k - 1)) / k
        if k == 1:
            ch = H * 0.6
        boxes = [(bx, H / 2, big_w, H - 2 * m, tilt(0, 1.2), "print")]
        top = H / 2 - (ch * k + gap * (k - 1)) / 2
        for j in range(k):
            boxes.append((cx, top + ch / 2 + j * (ch + gap), col_w, ch, tilt(0.5, 3),
                          "polaroid" if k == 1 else "print"))
        return boxes
    if layout == "scatter":
        # Loosely scattered polaroids, each nudged around inside its own cell
        rows = grid_for(n)
        cell_h = (H - 2 * m) / len(rows)
        boxes = []
        for r, cols in enumerate(rows):
            cell_w = (W - 2 * m) / cols
            bw, bh = cell_w - gap * 1.2, cell_h - gap * 1.2
            for c in range(cols):
                cx = m + cell_w * (c + 0.5) + rng.uniform(-0.5, 0.5) * (cell_w - gap - bw)
                cy = m + cell_h * (r + 0.5) + rng.uniform(-0.5, 0.5) * (cell_h - gap - bh)
                boxes.append((cx, cy, bw, bh, rng.uniform(-6, 6), "polaroid"))
        return boxes
    if layout == "grid":
        rows = grid_for(n)
        cell_h = (H - 2 * m - gap * (len(rows) - 1)) / len(rows)
        boxes = []
        for r, cols in enumerate(rows):
            cell_w = (W - 2 * m - gap * (cols - 1)) / cols
            for c in range(cols):
                boxes.append((m + cell_w * (c + 0.5) + gap * c, m + cell_h * (r + 0.5) + gap * r,
                              cell_w, cell_h, 0, "print"))
        return boxes
    return [(W / 2, H / 2, W - 2 * m, H - 2 * m, 0, "print")]  # centre


HAND_FONTS = ["Inkfree.ttf", "segoepr.ttf", "segoesc.ttf", "BRADHITC.TTF", "LHANDW.TTF"]
INK_ON_PAPER = (26, 36, 84)     # blue-black ballpoint on the polaroid frame
INK_ON_DARK = (240, 235, 220)   # white gel pen on the dark album page


@lru_cache(maxsize=64)
def hand_font(size: int) -> ImageFont.FreeTypeFont:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in HAND_FONTS:
        try:
            return ImageFont.truetype(str(Path(windir) / "Fonts" / name), size)
        except OSError:
            continue
    return find_font(size, italic=True)


def wrap_words(text: str, font, max_w: float) -> list[str]:
    lines, line = [], ""
    for w in text.split():
        trial = f"{line} {w}".strip()
        if font.getlength(trial) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    return lines + [line] if line else lines


def caption_fit(text: str, max_w: float, size: float, min_size: float):
    """Font and wrapped lines (ideally ≤ 2), shrinking the writing to fit max_w."""
    min_size = max(10, int(min_size))
    size = max(min_size, int(size))
    while True:
        font = hand_font(size)
        lines = wrap_words(text, font, max_w) or [""]
        fits = max(font.getlength(l) for l in lines) <= max_w
        if (len(lines) <= 2 and fits) or size <= min_size:
            break
        size = max(min_size, int(size * 0.9))
    asc, desc = font.getmetrics()
    return font, lines[:3], asc, desc


@dataclass
class Frame:
    b: int                 # side/top border
    bottom: int            # border + caption area below the photo
    caption: tuple | None  # (font, lines, ascent, descent)
    cap_top: int = 0       # y of caption area, relative to the photo bottom


def frame_for(style: str, unit: int, pw: float, caption: str, room: float = 0) -> Frame:
    """Border sizes for a photo pw wide; `room` is the width available in its layout box."""
    if style == "polaroid":
        b = max(4, int(unit * 0.022))
        bottom = 3 * b
    else:
        b = bottom = max(2, unit // 360)
    if not caption:
        return Frame(b, bottom, None)
    if style == "polaroid":
        cap = caption_fit(caption, pw * 0.94, min(unit * 0.045, pw * 0.11), unit * 0.02)
        line_h = cap[2] + cap[3]
        text_h = line_h * len(cap[1])
        return Frame(b, max(bottom, int(text_h + b * 0.9)), cap, 0)
    # Below the photo the writing may use the whole width of the photo's space
    max_w = max(pw, min(room or pw, unit * 0.6) * 0.96)
    cap = caption_fit(caption, max_w, min(unit * 0.042, max_w * 0.09), unit * 0.026)
    line_h = cap[2] + cap[3]
    gap = int(line_h * 0.25)
    return Frame(b, b + gap + int(line_h * len(cap[1])), cap, b + gap)


def ink_text(cap, color, seed: int) -> Image.Image:
    """Handwritten caption that looks like real pen ink (RGBA)."""
    font, lines, asc, desc = cap
    rng = random.Random(seed)
    line_h = asc + desc
    pad = int(line_h * 0.3)
    w = int(max(font.getlength(l) for l in lines)) + 2 * pad
    h = int(line_h * len(lines)) + 2 * pad
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    space = font.getlength(" ")
    stroke = 1 if font.size >= 30 else 0                # a little pen pressure
    for li, line in enumerate(lines):
        x = (w - font.getlength(line)) / 2
        base = pad + asc + li * line_h
        slope = rng.uniform(-0.015, 0.015)              # hand-written lines are never level
        for word in line.split():
            y = base + rng.uniform(-0.035, 0.035) * line_h + slope * (x - w / 2)
            shade = rng.randint(225, 255)
            d.text((x, y), word, font=font, fill=shade, anchor="ls",
                   stroke_width=stroke, stroke_fill=shade)
            x += font.getlength(word) + space
    # ink soaking slightly into the paper
    mask = mask.filter(ImageFilter.GaussianBlur(max(0.4, font.size / 75)))
    # uneven ink flow along the strokes
    sw, sh = max(1, w // 7), max(1, h // 7)
    flow = Image.frombytes("L", (sw, sh), rng.randbytes(sw * sh)).resize((w, h), Image.BICUBIC)
    mask = ImageChops.multiply(mask, flow.point(lambda v: 175 + v * 80 // 255))
    ink = Image.new("RGBA", (w, h), (*color, 0))
    ink.putalpha(mask)
    return ink


def make_print(im: Image.Image, style: str, unit: int, caption: str = "", seed: int = 0,
               room: float = 0) -> Image.Image:
    """Photo with a printed-photo or polaroid border (and handwritten caption), as RGBA."""
    fr = frame_for(style, unit, im.width, caption, room)
    b = fr.b
    border = POLAROID_BORDER if style == "polaroid" else PRINT_BORDER
    # Captions below a print may be wider than the photo itself
    ink = ink_text(fr.caption, INK_ON_PAPER if style == "polaroid" else INK_ON_DARK, seed) if fr.caption else None
    extra = max(0, (ink.width - im.width - 2 * b + 1) // 2) if ink and style != "polaroid" else 0
    tile = Image.new("RGBA", (im.width + 2 * b + 2 * extra, im.height + b + fr.bottom), (0, 0, 0, 0))
    paper_h = im.height + b + (fr.bottom if style == "polaroid" else b)
    tile.paste((*border, 255), (extra, 0, extra + im.width + 2 * b, paper_h))
    tile.paste(im, (extra + b, b))
    if ink:
        top = b + im.height + fr.cap_top
        area = fr.bottom - fr.cap_top
        layer = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        layer.paste(ink, ((tile.width - ink.width) // 2, top + (area - ink.height) // 2))
        tile = Image.alpha_composite(tile, layer)
    return tile


def draw_box(canvas: Image.Image, im: Image.Image, box: Box, unit: int,
             caption: str = "", seed: int = 0):
    cx, cy, bw, bh, angle, style = box
    if style == "full":
        canvas.paste(ImageOps.fit(im, canvas.size, Image.LANCZOS, centering=(0.5, 0.4)), (0, 0))
        return
    rad = math.radians(abs(angle))
    cos, sin = math.cos(rad), math.sin(rad)
    # Largest scale where the rotated, framed (and captioned) photo still fits in the box
    s = min(bw / im.width, bh / im.height)
    for _ in range(10):
        fr = frame_for(style, unit, im.width * s, caption, bw)
        tw, th = im.width * s + 2 * fr.b, im.height * s + fr.b + fr.bottom
        if fr.caption and style != "polaroid":  # writing below may be wider than the photo
            font, lines, asc, desc = fr.caption
            tw = max(tw, max(font.getlength(l) for l in lines) + 0.6 * (asc + desc))
        rw, rh = tw * cos + th * sin, tw * sin + th * cos
        f = min(bw / rw, bh / rh)
        if f >= 0.999:
            break
        s *= f
    photo = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    tile = make_print(photo, style, unit, caption, seed, bw)
    if angle:
        tile = ImageOps.expand(tile, 2, fill=(0, 0, 0, 0))  # soft edges after rotation
        tile = tile.rotate(angle, resample=Image.BICUBIC, expand=True)
    x, y = int(cx - tile.width / 2), int(cy - tile.height / 2)
    pad = max(6, unit // 90)
    alpha = tile.getchannel("A")
    shadow = Image.new("L", (tile.width + pad * 4, tile.height + pad * 4), 0)
    shadow.paste(alpha.point(lambda a: int(a * 0.7)), (pad * 2, pad * 2))
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad))
    canvas.paste((0, 0, 0), (x - pad * 2 + pad // 2, y - pad * 2 + pad), shadow)
    canvas.paste(tile, (x, y), tile)


def render_photo_slide(paths: list[Path], W: int, H: int, layout: str = "", seed: int = 0,
                       captions: list[str] | None = None) -> Image.Image:
    imgs = []
    for p in paths:
        try:
            imgs.append(load_photo(p, W, W))
        except Exception as exc:
            raise RuntimeError(f"Could not read the photo “{p.name}”: {exc}") from exc
    captions = list(captions or []) + [""] * (len(imgs) - len(captions or []))
    rng = random.Random(seed)
    aspects = [im.width / im.height for im in imgs]
    opts = layout_options(aspects, any(captions))
    if layout not in opts:
        layout = opts[0]
    canvas = background_from(imgs[0], W, H)
    boxes = layout_boxes(layout, aspects, W, H, rng)
    for i in range(len(imgs)):
        draw_box(canvas, imgs[i], boxes[i], min(W, H), captions[i], seed + i)
    return canvas


def render_text_slide(main: str, sub: str, W: int, H: int, photo: Path | None = None) -> Image.Image:
    """Title / closing card. With a photo: portrait photos sit beside the text,
    wide photos above it."""
    region = (0, 0, W, H)
    if photo:
        im = load_photo(photo, W, W)
        canvas = Image.blend(background_from(im, W, H), Image.new("RGB", (W, H), (10, 10, 12)), 0.35)
        unit = min(W, H)
        if im.width / im.height < 1.25:
            box = (W * 0.31, H / 2, W * 0.46, H * 0.84, 0, "print")
            region = (W * 0.56, 0, W * 0.96, H)
        else:
            box = (W / 2, H * 0.38, W * 0.8, H * 0.62, 0, "print")
            region = (W * 0.08, H * 0.70, W * 0.92, H * 0.97)
        draw_box(canvas, im, box, unit)
    else:
        canvas = Image.new("RGB", (W, H), (14, 14, 18))
        # subtle radial vignette glow
        glow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(glow).ellipse([W * 0.15, H * 0.1, W * 0.85, H * 0.9], fill=60)
        glow = glow.filter(ImageFilter.GaussianBlur(W // 10))
        canvas.paste((70, 62, 50), (0, 0), glow)
    d = ImageDraw.Draw(canvas)
    gold = (226, 212, 180)
    rx0, ry0, rx1, ry1 = region
    rw, rh = rx1 - rx0, ry1 - ry0
    scale = 0.8 if photo and rh < H * 0.5 else 1.0
    f_main = find_font(int(H * 0.075 * scale))
    f_sub = find_font(int(H * 0.045 * scale), italic=True)
    max_w = rw * (0.95 if photo else 0.8)

    def wrap(text, font, max_w):
        lines = []
        for para in text.split("\n"):
            words, line = para.split(), ""
            for w in words:
                trial = f"{line} {w}".strip()
                if d.textlength(trial, font=font) <= max_w or not line:
                    line = trial
                else:
                    lines.append(line)
                    line = w
            lines.append(line)
        return lines

    blocks = [(l, f_main, gold) for l in wrap(main, f_main, max_w)] if main else []
    if sub:
        blocks += [(l, f_sub, (200, 195, 185)) for l in wrap(sub, f_sub, max_w)]
    heights = [f.getbbox("Hg")[3] * 1.35 for _, f, _ in blocks]
    sep = H * 0.04 * scale if main and sub else 0
    y = ry0 + (rh - sum(heights) - sep) / 2
    cx = rx0 + rw / 2
    main_count = len(wrap(main, f_main, max_w)) if main else 0
    for i, ((line, font, col), lh) in enumerate(zip(blocks, heights)):
        if i == main_count and sep:
            lw = W * 0.12
            d.line([cx - lw / 2, y + sep / 2, cx + lw / 2, y + sep / 2], fill=(150, 135, 105), width=2)
            y += sep
        d.text((cx, y), line, font=font, fill=col, anchor="ma")
        y += lh
    return canvas


def smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


class SlideRenderer:
    """Builds slide canvases in a background thread ahead of when they're needed.
    canvas/prefetch/release are called from the render loop's thread only."""

    def __init__(self, slides: list[Slide], W: int, H: int):
        self.slides, self.W, self.H = slides, W, H
        self.cw, self.ch = int(W * KB), int(H * KB)
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="slide")
        self.futs: dict[int, Future] = {}

    def _build(self, i: int) -> Image.Image:
        s = self.slides[i]
        if s.text:
            return render_text_slide(*s.text, self.cw, self.ch, s.photos[0] if s.photos else None)
        return render_photo_slide(s.photos, self.cw, self.ch, s.layout, s.seed, s.captions)

    def prefetch(self, i: int):
        if 0 <= i < len(self.slides) and i not in self.futs:
            self.futs[i] = self.pool.submit(self._build, i)

    def canvas(self, i: int) -> Image.Image:
        self.prefetch(i)
        return self.futs[i].result()

    def release(self, before: int):
        for k in [k for k in self.futs if k < before]:
            del self.futs[k]

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def frame(self, i: int, t: float, canvas: Image.Image | None = None) -> Image.Image:
        """Slide i at time t, with a slow Ken Burns zoom/pan."""
        s = self.slides[i]
        p = smooth((t - s.start) / s.duration) if s.duration else 0
        if s.text:
            amount = 0.025 if s.photos else 0.0     # barely-there drift on title cards with a photo
        else:
            amount = (KB - 1.0) * (0.5 if len(s.photos) > 1 else 1.0)
        # Alternate slow zoom-in / zoom-out; k goes 0..1 as the zoom deepens.
        k = p if i % 2 == 0 else 1 - p
        z = 1.0 + amount * k
        cw, ch = self.cw / z, self.ch / z
        # Gentle drift towards a different point on each slide
        fx, fy = [(0, 0), (-0.6, -0.3), (0.6, 0.3), (-0.3, 0.6), (0.3, -0.6)][i % 5]
        cx = self.cw / 2 + fx * (self.cw - cw) / 2 * k
        cy = self.ch / 2 + fy * (self.ch - ch) / 2 * k
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return (canvas or self.canvas(i)).resize((self.W, self.H), Image.BILINEAR, box=box)


def _remove_quietly(path: Path):
    try:
        path.unlink()
    except OSError:
        pass


def render_video(slides: list[Slide], trans: float, audio: Path, total: float,
                 cfg: Config, progress, cancel: threading.Event):
    W, H, fps = cfg.width, cfg.height, cfg.fps
    r = SlideRenderer(slides, W, H)
    black = Image.new("RGB", (W, H), (0, 0, 0))
    n_frames = int(round(total * fps))
    out_path = Path(cfg.output)
    # Render to a temp file so a cancel/failure never destroys an existing video.
    part_path = out_path.with_name(f"{out_path.stem}.part{out_path.suffix or '.mp4'}")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-i", str(audio), "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(part_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    err_chunks: list[bytes] = []
    err_thread = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    err_thread.start()

    def ffmpeg_error() -> str:
        proc.wait()
        err_thread.join(timeout=5)
        text = b"".join(err_chunks).decode(errors="replace").strip()
        return text[-2000:] or "ffmpeg stopped unexpectedly."
    # Picture fades to black together with the music fading out at the end
    fade_in, fade_out = min(2.0, total / 6), min(AUDIO_FADE_OUT, total / 6)

    def compose(i: int, t: float, cv: Image.Image, cv2: Image.Image | None) -> bytes:
        img = r.frame(i, t, cv)
        if cv2 is not None:
            nxt = slides[i + 1]
            a = (t - nxt.start) / trans
            if nxt.dip_to_black:
                img = (Image.blend(img, black, smooth(a * 2)) if a < 0.5 else
                       Image.blend(black, r.frame(i + 1, t, cv2), smooth(a * 2 - 1)))
            else:
                img = Image.blend(img, r.frame(i + 1, t, cv2), smooth(a))
        if t < fade_in:
            img = Image.blend(black, img, smooth(t / fade_in))
        elif t > total - fade_out:
            img = Image.blend(black, img, smooth((total - t) / fade_out))
        return img.tobytes()

    # Frames are drawn in parallel (Pillow releases the GIL while resizing/blending)
    # and written to ffmpeg in order; the next slides are built in the background.
    workers = max(2, min(8, (os.cpu_count() or 4) - 2))
    pool = ThreadPoolExecutor(workers, thread_name_prefix="frame")
    pending: deque[Future] = deque()
    written = 0

    def write_oldest():
        nonlocal written
        proc.stdin.write(pending.popleft().result())
        if written % fps == 0:
            t = written / fps
            progress(written / n_frames, f"Rendering {fmt_time(t)} / {fmt_time(total)}")
        written += 1

    cur = 0
    try:
        for f in range(n_frames):
            if cancel.is_set():
                raise Cancelled()
            t = f / fps
            while cur + 1 < len(slides) and t >= slides[cur].start + slides[cur].duration:
                cur += 1
                r.release(cur)
            in_trans = cur + 1 < len(slides) and t >= slides[cur + 1].start
            cv = r.canvas(cur)
            cv2 = r.canvas(cur + 1) if in_trans else None
            r.prefetch(cur + 1)
            r.prefetch(cur + 2)
            pending.append(pool.submit(compose, cur, t, cv, cv2))
            if len(pending) >= workers * 2:
                write_oldest()
        while pending:
            if cancel.is_set():
                raise Cancelled()
            write_oldest()
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg failed: " + ffmpeg_error())
        os.replace(part_path, out_path)
    except BrokenPipeError:
        # ffmpeg exited early (bad output path, unsupported frame, out of disk space…)
        proc.kill()
        message = ffmpeg_error()
        _remove_quietly(part_path)
        raise RuntimeError("ffmpeg stopped unexpectedly: " + message)
    except BaseException:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        _remove_quietly(part_path)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        r.close()


def work_dir(output: str) -> Path:
    return Path(output).parent / "slideshow_work"


def clean_songs(songs: list[str]) -> list[str]:
    return [s for s in (x.strip() for x in songs) if s]


def fetch_tracks(songs: list[str], work: Path, log, progress,
                 cancel: threading.Event) -> list[tuple[Path, float]]:
    """Download / locate every song and return (file, duration) pairs."""
    songs = clean_songs(songs)
    if not songs:
        raise RuntimeError("Please enter at least one song.")
    work.mkdir(parents=True, exist_ok=True)
    tracks = []
    for i, s in enumerate(songs, 1):
        if cancel.is_set():
            raise Cancelled()
        progress((i - 1) / len(songs), f"Getting song {i} of {len(songs)}…")
        log(f"Song {i}: {s}")
        path = obtain_song(s, work / "music", log)
        dur = media_duration(path)
        log(f"  Length {fmt_time(dur)}  ({path.name})")
        tracks.append((path, dur))
    return tracks


def count_photo_slides(photos: list[Path], aspects: dict | None = None) -> int:
    """Slides needed with one photo per slide (paired portraits share a slide)."""
    aspects = aspects or {p: aspect_of(p) for p in photos}
    return len(group_photos(photos, aspects, 1))


def auto_speed(natural: float, n_slides: int) -> float:
    """Speed-up (1.0 .. MAX_SPEEDUP) used when there are few photos for the music."""
    if n_slides and natural / n_slides > COMFORT_SLIDE_SECONDS:
        return min(MAX_SPEEDUP, natural / (n_slides * COMFORT_SLIDE_SECONDS))
    return 1.0


def create_video(cfg: Config, log, progress, cancel: threading.Event):
    photos = list_photos(cfg.photos_dir, cfg.order)
    for label, b in (("Opening", cfg.opening_photo), ("Closing", cfg.closing_photo)):
        if b and not Path(b).is_file():
            raise RuntimeError(f"{label} photo not found: {b}")
    if not photos and not (cfg.opening_photo or cfg.closing_photo):
        raise RuntimeError(f"No photos found in {cfg.photos_dir}")
    out_dir = Path(cfg.output).parent
    if not out_dir.is_dir():
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Could not create the folder for the video: {out_dir}") from exc
    log(f"Found {len(photos)} photos.")

    work = work_dir(cfg.output)
    tracks = fetch_tracks(cfg.songs, work, log, progress, cancel)
    natural, _ = soundtrack_length([d for _, d in tracks])
    log(f"Music length at normal speed: {fmt_time(natural)}")

    speed = 1.0
    if cfg.manual_speed:
        speed = max(MIN_MANUAL_SPEED, min(MAX_MANUAL_SPEED, cfg.manual_speed))
        log(f"Manual speed: {speed:.2f}x (pitch unchanged).")
    elif cfg.allow_speedup:
        first, middle, last = split_bookends(photos, cfg.opening_photo, cfg.closing_photo)
        cards = n_bookend_slides(bool(cfg.title or cfg.subtitle), bool(cfg.closing), first, last)
        speed = auto_speed(natural, count_photo_slides(middle) + cards)
        if speed > 1.0:
            log(f"Not many photos for {fmt_time(natural)} of music - "
                f"playing music at {speed:.2f}x speed (pitch unchanged).")
    tracks = [p for p, _ in tracks]

    progress(0.0, "Mixing music…")
    audio = work / "soundtrack.m4a"
    total = build_soundtrack(tracks, audio, speed)
    log(f"Total music length: {fmt_time(total)}")

    slides, trans = plan_slides(cfg, photos, total, log)
    n_photo_slides = sum(1 for s in slides if s.photos)
    shown = [p for s in slides for p in s.photos]

    def path_key(p: Path) -> str:
        try:
            return os.path.normcase(str(p.resolve()))
        except OSError:
            return os.path.normcase(str(p))

    shown_keys = {path_key(p) for p in shown}
    missing = [p for p in photos if path_key(p) not in shown_keys]
    if missing:
        raise RuntimeError("These photos would be missing from the video: "
                           + ", ".join(p.name for p in missing[:5]))
    avg = sum(s.duration for s in slides if s.photos) / max(1, n_photo_slides)
    log(f"{len(slides)} slides ({n_photo_slides} with photos, all {len(shown)} photos), "
        f"~{avg:.1f}s each, {trans:.1f}s transitions.")

    render_video(slides, trans, audio, total, cfg, progress, cancel)
    progress(1.0, "Done")
    log(f"Finished: {cfg.output}")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Memorial Slideshow Creator")
    root.geometry("860x980")
    root.minsize(760, 760)
    pad = {"padx": 8, "pady": 4}
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    photos_var = tk.StringVar()
    out_var = tk.StringVar()
    title_var, sub_var, closing_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
    open_photo_var, close_photo_var = tk.StringVar(), tk.StringVar()
    res_var = tk.StringVar(value="1920x1080 (Full HD)")
    trans_var = tk.DoubleVar(value=2.5)
    style_var = tk.StringVar(value="Mixed (crossfade + dip to black)")
    order_var = tk.StringVar(value="Filename")
    speed_var = tk.BooleanVar(value=True)
    manual_var = tk.BooleanVar(value=False)
    manual_speed_var = tk.DoubleVar(value=1.0)
    write_titles_var = tk.BooleanVar(value=True)
    # Result of the last "Analyse Music" run
    analysis = {"songs": None, "durs": [], "natural": None, "slides": None,
                "photos": [], "aspects": {}, "dir": ""}

    row = 0
    ttk.Label(frm, text="Photos folder:").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=photos_var).grid(row=row, column=1, sticky="ew", **pad)

    def pick_photos():
        d = filedialog.askdirectory(title="Select the folder containing the photos")
        if not d:
            return
        try:
            photos = list_photos(d, "Filename")
        except OSError as exc:
            messagebox.showerror("Photos", f"Could not read that folder:\n{exc}")
            return
        photos_var.set(d)
        n_caps = len(read_captions(d, photos)) if write_titles_var.get() else 0
        status.set(f"{len(photos)} photos found in folder"
                   + (f", {n_caps} with a title." if n_caps else "."))
        if not out_var.get():
            out_var.set(str(Path(d) / "Memorial Slideshow.mp4"))
    ttk.Button(frm, text="Browse…", command=pick_photos).grid(row=row, column=2, **pad)

    row += 1
    ttk.Label(frm, text="Photo order:").grid(row=row, column=0, sticky="w", **pad)
    ttk.Combobox(frm, textvariable=order_var, state="readonly", width=20,
                 values=["Filename", "Date taken (EXIF)"]).grid(row=row, column=1, sticky="w", **pad)

    row += 1
    ttk.Label(frm, text="Photo titles:").grid(row=row, column=0, sticky="w", **pad)
    ttk.Checkbutton(frm, variable=write_titles_var, text=(
        "Write photo titles on the slides in pen-style ink (from captions.txt or the photo's own Title)"
        )).grid(row=row, column=1, columnspan=2, sticky="w", **pad)

    row += 1
    ttk.Label(frm, text="Songs (in play order,\none per line):").grid(row=row, column=0, sticky="nw", **pad)
    songs_txt = tk.Text(frm, height=6, wrap="none")
    songs_txt.grid(row=row, column=1, sticky="nsew", **pad)
    songs_txt.insert("1.0", "")

    def add_file():
        files = filedialog.askopenfilenames(title="Choose audio files",
                                            filetypes=[("Audio", " ".join(f"*{e}" for e in AUDIO_EXTS))])
        for f in files:
            existing = songs_txt.get("1.0", "end").rstrip("\n")
            songs_txt.insert("end", ("\n" if existing else "") + f)
    ttk.Button(frm, text="Add local file…", command=add_file).grid(row=row, column=2, sticky="n", **pad)
    row += 1
    ttk.Label(frm, foreground="#555", wraplength=440, text=(
        "Type song titles, e.g. “Amazing Grace - Il Divo” or “Time to Say Goodbye - Andrea Bocelli”. "
        "Titles are searched online and downloaded. You can also paste a URL or add a local audio file. "
        "Please make sure you have the right to use the music."
    )).grid(row=row, column=1, sticky="w", padx=8)

    row += 1
    ttk.Separator(frm).grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    row += 1
    ttk.Label(frm, text="Title card (optional):").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=title_var).grid(row=row, column=1, sticky="ew", **pad)
    ttk.Label(frm, foreground="#555", text="e.g. In Loving Memory of Jane Smith").grid(row=row, column=2, sticky="w")
    row += 1
    ttk.Label(frm, text="Title subtitle:").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=sub_var).grid(row=row, column=1, sticky="ew", **pad)
    ttk.Label(frm, foreground="#555", text="e.g. 1942 – 2026").grid(row=row, column=2, sticky="w")

    def photo_picker(row: int, label: str, var, hint: str):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", **pad)
        cell = ttk.Frame(frm)
        cell.grid(row=row, column=1, sticky="ew", **pad)
        cell.columnconfigure(0, weight=1)
        ttk.Entry(cell, textvariable=var).grid(row=0, column=0, sticky="ew")

        def browse():
            f = filedialog.askopenfilename(
                title=f"Choose the {label.split()[0].lower()} photo",
                initialdir=photos_var.get() or None,
                filetypes=[("Photos", " ".join(f"*{e}" for e in sorted(IMAGE_EXTS)))])
            if f:
                var.set(f)
        ttk.Button(cell, text="Browse…", command=browse).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(cell, text="Clear", width=6, command=lambda: var.set("")).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(frm, foreground="#555", text=hint).grid(row=row, column=2, sticky="w")

    row += 1
    photo_picker(row, "Opening photo (optional):", open_photo_var, "shown first, with the title")
    row += 1
    ttk.Label(frm, text="Closing card (optional):").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=closing_var).grid(row=row, column=1, sticky="ew", **pad)
    ttk.Label(frm, foreground="#555", text="e.g. Forever in our hearts").grid(row=row, column=2, sticky="w")
    row += 1
    photo_picker(row, "Closing photo (optional):", close_photo_var, "shown last, with the closing text")

    row += 1
    opts = ttk.Frame(frm)
    opts.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
    ttk.Label(opts, text="Resolution:").pack(side="left")
    ttk.Combobox(opts, textvariable=res_var, state="readonly", width=18,
                 values=["1920x1080 (Full HD)", "1280x720 (HD)", "3840x2160 (4K)"]).pack(side="left", padx=(4, 16))
    ttk.Label(opts, text="Transition (s):").pack(side="left")
    ttk.Spinbox(opts, from_=0.5, to=MAX_TRANSITION, increment=0.5, textvariable=trans_var,
                width=5).pack(side="left", padx=(4, 16))
    ttk.Label(opts, text="Style:").pack(side="left")
    ttk.Combobox(opts, textvariable=style_var, state="readonly", width=30,
                 values=["Mixed (crossfade + dip to black)", "Crossfade", "Dip to black"]
                 ).pack(side="left", padx=4)

    row += 1
    music = ttk.LabelFrame(frm, text="Music length and speed", padding=6)
    music.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
    music.columnconfigure(1, weight=1)
    analysis_var = tk.StringVar(value="Click “Analyse Music” to download the songs and see the total length.")
    analyse_btn = ttk.Button(music, text="Analyse Music")
    analyse_btn.grid(row=0, column=0, sticky="nw", padx=4, pady=2)
    ttk.Label(music, textvariable=analysis_var, justify="left", wraplength=600
              ).grid(row=0, column=1, columnspan=3, sticky="w", padx=4, pady=2)

    auto_chk = ttk.Checkbutton(music, variable=speed_var, text=(
        "Auto: if there aren't many photos, play the music slightly faster (up to 1.1x)"))
    auto_chk.grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=2)

    ttk.Checkbutton(music, variable=manual_var, text="Manual speed:",
                    command=lambda: on_manual_toggle()).grid(row=2, column=0, sticky="w", padx=4)
    speed_scale = ttk.Scale(music, from_=MIN_MANUAL_SPEED, to=MAX_MANUAL_SPEED,
                            variable=manual_speed_var, command=lambda _v: on_speed_change())
    speed_scale.grid(row=2, column=1, sticky="ew", padx=4)
    ttk.Button(music, text="Reset", width=6,
               command=lambda: (manual_speed_var.set(1.0), on_speed_change())
               ).grid(row=2, column=2, padx=4)
    speed_lbl = ttk.Label(music, text="1.00x", width=6)
    speed_lbl.grid(row=2, column=3, padx=4)
    ttk.Label(music, text=f"{MIN_MANUAL_SPEED:.2f}x slower", foreground="#555").grid(row=3, column=1, sticky="w", padx=4)
    ttk.Label(music, text=f"faster {MAX_MANUAL_SPEED:.2f}x", foreground="#555").grid(row=3, column=1, sticky="e", padx=4)
    result_var = tk.StringVar(value="")
    ttk.Label(music, textvariable=result_var, font=("Segoe UI", 10, "bold")
              ).grid(row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(4, 2))

    def total_slides() -> int | None:
        """Slides the video will have (photos analysed + title/closing/bookend slides)."""
        if analysis["slides"] is None:
            return None
        first, middle, last = split_bookends(analysis["photos"], open_photo_var.get().strip().strip('"'),
                                             close_photo_var.get().strip().strip('"'))
        has_title = bool(title_var.get().strip() or sub_var.get().strip())
        return (count_photo_slides(middle, analysis["aspects"]) if middle else 0) + \
            n_bookend_slides(has_title, bool(closing_var.get().strip()), first, last)

    def chosen_speed() -> tuple[float, str]:
        if manual_var.get():
            return round(manual_speed_var.get(), 2), "manual"
        n = total_slides()
        if speed_var.get() and analysis["natural"] is not None and n:
            return auto_speed(analysis["natural"], n), "auto"
        return 1.0, "normal"

    def on_speed_change():
        manual_speed_var.set(round(manual_speed_var.get() / 0.01) * 0.01)
        speed_lbl.configure(text=f"{manual_speed_var.get():.2f}x")
        update_result()

    def on_manual_toggle():
        manual = manual_var.get()
        speed_scale.state(["!disabled"] if manual else ["disabled"])
        auto_chk.state(["disabled"] if manual else ["!disabled"])
        update_result()

    def update_result(*_):
        nat = analysis["natural"]
        if nat is None or analysis["songs"] != clean_songs(songs_txt.get("1.0", "end").splitlines()):
            result_var.set("Music not analysed yet." if nat is None
                           else "Songs changed – click “Analyse Music” again.")
            return
        if analysis["photos"] and analysis["dir"] != photos_var.get().strip().strip('"'):
            result_var.set("Photos folder changed – click “Analyse Music” again.")
            return
        speed, mode = chosen_speed()
        total = soundtrack_length(analysis["durs"], speed)[0]
        text = f"Video length: {fmt_time(total)} at {speed:.2f}x ({mode})"
        if abs(speed - 1.0) > 1e-3:
            text += f"   –   normal speed {fmt_time(nat)}, difference {'+' if total > nat else '-'}{fmt_time(abs(total - nat))}"
        n = total_slides()
        if n:
            try:
                trans = min(MAX_TRANSITION, max(0.5, float(trans_var.get())))
            except (tk.TclError, ValueError):
                trans = 2.5
            text += f"   –   about {(total + (n - 1) * trans) / n:.1f}s per slide"
        result_var.set(text)

    for v in (speed_var, title_var, sub_var, closing_var, trans_var, open_photo_var,
              close_photo_var, photos_var):
        v.trace_add("write", update_result)
    songs_txt.bind("<<Modified>>", lambda e: (songs_txt.edit_modified(False), update_result()))
    on_manual_toggle()

    row += 1
    ttk.Label(frm, text="Save video as:").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=out_var).grid(row=row, column=1, sticky="ew", **pad)

    def pick_out():
        f = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")],
                                         initialfile="Memorial Slideshow.mp4")
        if f:
            out_var.set(f)
    ttk.Button(frm, text="Save As…", command=pick_out).grid(row=row, column=2, **pad)

    row += 1
    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    start_btn = ttk.Button(btns, text="Create Video")
    start_btn.pack(side="left", padx=8)
    cancel_btn = ttk.Button(btns, text="Cancel", state="disabled")
    cancel_btn.pack(side="left")
    open_btn = ttk.Button(btns, text="Open Video", state="disabled")
    open_btn.pack(side="left", padx=8)

    row += 1
    bar = ttk.Progressbar(frm, maximum=1000)
    bar.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
    row += 1
    status = tk.StringVar(value="Choose a photos folder and enter the songs.")
    ttk.Label(frm, textvariable=status).grid(row=row, column=0, columnspan=3, sticky="w", **pad)
    row += 1
    log_txt = tk.Text(frm, height=8, state="disabled", bg="#f7f7f7")
    log_txt.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
    frm.rowconfigure(row, weight=1)

    msgs: queue.Queue = queue.Queue()
    cancel = threading.Event()

    def poll():
        try:
            while True:
                kind, *val = msgs.get_nowait()
                if kind == "log":
                    log_txt.configure(state="normal")
                    log_txt.insert("end", val[0] + "\n")
                    log_txt.see("end")
                    log_txt.configure(state="disabled")
                elif kind == "progress":
                    bar["value"] = val[0] * 1000
                    status.set(val[1])
                elif kind == "analysed":
                    songs, durs, slides, photos, aspects, photo_dir = val
                    analysis.update(songs=songs, durs=durs, slides=slides, photos=photos,
                                    aspects=aspects, natural=soundtrack_length(durs)[0],
                                    dir=photo_dir)
                    lines = [f"{i}. {fmt_time(d)}  {s}" for i, (s, d) in enumerate(zip(songs, durs), 1)]
                    lines.append(f"Total at normal speed: {fmt_time(analysis['natural'])}"
                                 + (f"  –  {slides} photo slides" if slides else ""))
                    analysis_var.set("\n".join(lines))
                    update_result()
                elif kind == "done":
                    start_btn.configure(state="normal")
                    analyse_btn.configure(state="normal")
                    cancel_btn.configure(state="disabled")
                    ok, text = val
                    status.set(text)
                    if ok == "analysed":
                        bar["value"] = 0        # analysed, but no video rendered yet
                    elif ok:
                        bar["value"] = 1000
                        open_btn.configure(state="normal")
                        messagebox.showinfo("Finished", text)
                    else:
                        bar["value"] = 0
                        if text != "Cancelled.":
                            messagebox.showerror("Error", text)
        except queue.Empty:
            pass
        except Exception:               # a bad message must not stop the UI updates
            traceback.print_exc()
        finally:
            root.after(100, poll)

    def start():
        songs = songs_txt.get("1.0", "end").splitlines()
        photos_dir = photos_var.get().strip().strip('"')
        if not photos_dir or not Path(photos_dir).is_dir():
            messagebox.showwarning("Photos", "Please choose the folder containing the photos.")
            return
        if not any(s.strip() for s in songs):
            messagebox.showwarning("Songs", "Please enter at least one song title.")
            return
        if not out_var.get():
            pick_out()
            if not out_var.get():
                return
        out = out_var.get().strip().strip('"')
        if not Path(out).suffix:
            out += ".mp4"
        out_var.set(out)
        w, h = map(int, res_var.get().split()[0].split("x"))
        try:
            trans = float(trans_var.get())
        except (tk.TclError, ValueError):
            trans = 2.5
        cfg = Config(photos_dir=photos_dir, songs=songs, output=out,
                     width=w, height=h, transition=min(MAX_TRANSITION, max(0.5, trans)),
                     style=style_var.get(), order=order_var.get(), title=title_var.get().strip(),
                     subtitle=sub_var.get().strip(), closing=closing_var.get().strip(),
                     allow_speedup=speed_var.get() and not manual_var.get(),
                     manual_speed=round(manual_speed_var.get(), 2) if manual_var.get() else None,
                     opening_photo=open_photo_var.get().strip().strip('"'),
                     closing_photo=close_photo_var.get().strip().strip('"'),
                     write_titles=write_titles_var.get())
        cancel.clear()
        start_btn.configure(state="disabled")
        analyse_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        open_btn.configure(state="disabled")

        def worker():
            try:
                create_video(cfg, lambda m: msgs.put(("log", m)),
                             lambda p, s: msgs.put(("progress", p, s)), cancel)
                msgs.put(("done", True, f"Video saved to {cfg.output}"))
            except Cancelled:
                msgs.put(("log", "Cancelled."))
                msgs.put(("done", False, "Cancelled."))
            except Exception as exc:
                msgs.put(("log", traceback.format_exc()))
                msgs.put(("done", False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def analyse():
        songs = clean_songs(songs_txt.get("1.0", "end").splitlines())
        if not songs:
            messagebox.showwarning("Songs", "Please enter at least one song title.")
            return
        photos_dir = photos_var.get().strip().strip('"')
        if not out_var.get() and photos_dir and Path(photos_dir).is_dir():
            out_var.set(str(Path(photos_dir) / "Memorial Slideshow.mp4"))
        if not out_var.get():
            messagebox.showwarning("Photos", "Please choose the photos folder (or where to save the video) "
                                             "first – the music is downloaded next to the video.")
            return
        output = out_var.get().strip().strip('"')
        order = order_var.get()
        opening = open_photo_var.get().strip().strip('"')
        closing_photo = close_photo_var.get().strip().strip('"')
        cancel.clear()
        start_btn.configure(state="disabled")
        analyse_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")

        def worker():
            log = lambda m: msgs.put(("log", m))
            try:
                tracks = fetch_tracks(songs, work_dir(output), log,
                                      lambda p, s: msgs.put(("progress", p, s)), cancel)
                slides, photos, aspects = None, [], {}
                if photos_dir and Path(photos_dir).is_dir():
                    msgs.put(("progress", 1.0, "Checking photos…"))
                    photos = list_photos(photos_dir, order)
                    aspects = {p: aspect_of(p) for p in photos}
                    # The opening/closing photo gets its own card, so don't count it twice
                    _, middle, _ = split_bookends(photos, opening, closing_photo)
                    slides = count_photo_slides(middle, aspects) if middle else 0
                msgs.put(("analysed", songs, [d for _, d in tracks], slides, photos, aspects,
                          photos_dir if photos else ""))
                msgs.put(("done", "analysed", "Music analysed – adjust the speed if needed, then click Create Video."))
            except Cancelled:
                msgs.put(("log", "Cancelled."))
                msgs.put(("done", False, "Cancelled."))
            except Exception as exc:
                msgs.put(("log", traceback.format_exc()))
                msgs.put(("done", False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    analyse_btn.configure(command=analyse)
    start_btn.configure(command=start)
    cancel_btn.configure(command=cancel.set)
    open_btn.configure(command=lambda: os.startfile(out_var.get()) if sys.platform == "win32"
                       else subprocess.Popen(["xdg-open", out_var.get()]))
    poll()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
