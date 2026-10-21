"""Illustrations of the site: named slots, generation prompts, import command.

The site works without any image (gradients and icons). Drop images into
``static/img/`` with a slot name and they are used at once:

    python -m app.images list                     # slots, prompt, present or missing
    python -m app.images prompts > prompts.md     # every prompt, ready to paste in an image generator
    python -m app.images import ~/Downloads/gen   # resize + convert files named after the slots (needs Pillow)

Several variants of one slot are allowed (``ev-launch.webp``, ``ev-launch-2.webp``…): each
event always gets the same variant, chosen from its id, so lists do not repeat one picture.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from app.config import settings

IMG_DIR = settings.static_dir / "img"
EXTS = (".webp", ".jpg", ".jpeg", ".png", ".avif")
MAX_VARIANTS = 4

STYLE = ("photorealistic, cinematic, deep midnight-blue and navy palette with subtle cyan highlights, "
         "calm and sober mood, high detail, no text, no logos, no flags, no insignia, no recognizable people")

# slot: (width, height, aspect for the generator, subject)
SLOTS: dict[str, tuple[int, int, str, str]] = {
    "hero": (2400, 1100, "21:9", "Earth's night side seen from low orbit, thin glowing blue atmosphere along a curved horizon, "
             "faint city lights, a soft sunrise glow on the right edge, dark starry space, wide empty dark area on the left"),
    "eco": (1600, 640, "5:2", "abstract constellation of small glowing nodes linked by thin lines, floating above the dark curve of the Earth"),
    "mod-actualites": (800, 600, "4:3", "large satellite ground-station dish at night under a starry sky, faint blue glow"),
    "mod-comprendre": (800, 600, "4:3", "a satellite being assembled in an empty clean room, blue lighting, reflective floor"),
    "mod-acteurs": (800, 600, "4:3", "aerospace factory hall at night, aircraft fuselage sections on assembly jigs, blue tones"),
    "mod-geopolitique": (800, 600, "4:3", "Europe seen from orbit at night with city lights, subtle glowing arcs linking capitals"),
    "mod-tendances": (800, 600, "4:3", "long-exposure orbital trajectories drawn as glowing lines around the night Earth"),
    "ev-launch": (640, 480, "4:3", "a generic rocket lifting off at dusk, bright plume, launch tower, no markings"),
    "ev-contract": (640, 480, "4:3", "defense industry assembly line at night, missile canisters or radar panels under blue light"),
    "ev-test": (640, 480, "4:3", "rocket engine hot-fire test on a test stand at night, bright flame and steam cloud"),
    "ev-delivery": (640, 480, "4:3", "military transport aircraft on a tarmac at blue hour, rear cargo ramp open, unmarked"),
    "ev-partner": (640, 480, "4:3", "two satellites flying in close formation above the blue Earth"),
    "ev-invest": (640, 480, "4:3", "new aerospace plant exterior at blue hour, glass and steel facade, lit windows"),
    "ev-regul": (640, 480, "4:3", "modern European institutional building at blue hour, empty square, soft lights"),
    "ev-delay": (640, 480, "4:3", "rocket waiting on its launch pad at night under heavy rain clouds, floodlights"),
    "ev-research": (640, 480, "4:3", "laboratory bench with a small satellite antenna prototype, blue light, macro detail"),
    "ev-other": (640, 480, "4:3", "a satellite in orbit above the Earth with a sun glint on its solar panels"),
}


def prompt(slot: str) -> str:
    w, h, aspect, subject = SLOTS[slot]
    return f"{subject}. {STYLE}. Aspect ratio {aspect}."


def available(img_dir: Path | None = None) -> dict[str, list[str]]:
    """slot → list of URLs under /static/img, in variant order."""
    img_dir = img_dir or IMG_DIR
    found: dict[str, dict[int, str]] = {}
    if not img_dir.exists():
        return {}
    for f in img_dir.iterdir():
        if f.suffix.lower() not in EXTS:
            continue
        m = re.fullmatch(r"(.+?)(?:-(\d))?", f.stem)
        slot, n = m.group(1), int(m.group(2) or 1)
        if slot in SLOTS and n <= MAX_VARIANTS:
            # a .webp wins over another format of the same variant
            if n not in found.get(slot, {}) or f.suffix.lower() == ".webp":
                found.setdefault(slot, {})[n] = f"/static/img/{f.name}"
    return {s: [v[k] for k in sorted(v)] for s, v in found.items()}


_cache: dict = {"key": None, "data": {}}


def current() -> dict[str, list[str]]:
    """available(), rescanned only when the folder changes (new or removed image)."""
    try:
        stamp = (IMG_DIR.stat().st_mtime_ns, len(list(IMG_DIR.iterdir())))
    except FileNotFoundError:
        return {}
    if _cache["key"] != stamp:
        _cache.update(key=stamp, data=available())
    return _cache["data"]


def img_for(slot: str, key: str = "") -> str:
    """Template helper: URL of the image for this slot (and item), or "" to keep the drawn fallback."""
    return pick(current(), slot, key)


def pick(images: dict[str, list[str]], slot: str, key: str = "") -> str:
    """The image of a slot for one item: always the same variant for the same key."""
    urls = images.get(slot) or []
    if not urls:
        return ""
    return urls[int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(urls)] if key else urls[0]


# ---------------------------------------------------------------------- command line
def _import(folder: Path) -> int:  # pragma: no cover - needs Pillow and files
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
    except ImportError:
        print("Pillow is missing: pip install pillow", file=sys.stderr)
        return 1
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    done, skipped = 0, []
    for f in sorted(folder.iterdir()):
        m = re.fullmatch(r"(.+?)(?:-(\d))?", f.stem)
        if f.suffix.lower() not in EXTS or not m or m.group(1) not in SLOTS:
            if f.is_file() and not f.name.startswith("."):
                skipped.append(f.name)
            continue
        w, h, _, _ = SLOTS[m.group(1)]
        with Image.open(f) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            scale = min(1.0, im.width / w, im.height / h)     # never enlarge: an upscaled picture is heavier, not sharper
            w, h = round(w * scale), round(h * scale)
            im = ImageOps.fit(im, (w, h), method=Image.LANCZOS, centering=(0.5, 0.5))
            out = IMG_DIR / f"{f.stem}.webp"
            im.save(out, "WEBP", quality=80, method=6)
        print(f"{f.name} → static/img/{out.name} ({w}×{h}, {out.stat().st_size // 1024} Ko)")
        done += 1
    if skipped:
        print("ignored (name is not a slot): " + ", ".join(skipped))
    print(f"{done} image(s) imported. Reload the page.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:]) or ["list"]
    if args[0] == "list":
        have = available()
        for slot, (w, h, aspect, _) in SLOTS.items():
            n = len(have.get(slot, []))
            print(f"{slot:<18} {aspect:>5}  {w}×{h:<5} {'✓ ' + str(n) + ' image(s)' if n else '— missing'}")
        print("\nPrompts: python -m app.images prompts    Import: python -m app.images import <folder>")
        return 0
    if args[0] == "prompts":
        print("# Prompts for the site's images\n\nName each generated file after its slot "
              "(variants: ev-launch-2.png, ev-launch-3.png…), then run `python -m app.images import <folder>`.\n")
        for slot in SLOTS:
            print(f"## {slot}\n\n{prompt(slot)}\n")
        return 0
    if args[0] == "import" and len(args) > 1:
        return _import(Path(args[1]).expanduser())
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
