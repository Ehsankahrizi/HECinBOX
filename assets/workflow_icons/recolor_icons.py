"""Recolor + high-quality re-master for PNG icons.

Enhancements over the naive version:
  • NumPy-vectorized recolor (fast, exact) instead of a per-pixel loop.
  • Premultiplied-alpha LANCZOS resize — eliminates the dark/light
    fringe (halo) that straight-alpha resampling produces on soft
    edges. (Invisible for pure black, essential for any tint.)
  • Coverage-preserving recolor: shape & anti-aliasing come from the
    alpha channel; only RGB is replaced, so edges stay smooth.
  • Optional alpha-gamma to keep thin strokes from fading when an icon
    is scaled down, and an optional unsharp pass to crisp the edges.
  • Luminance fallback: if a source has no real transparency (flat
    white background), alpha is derived from darkness so line art on
    white still recolors cleanly.
  • Robust loading (EXIF orientation, mode coercion, error capture).
"""
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter, ImageOps

# ───────────────────────── settings ─────────────────────────
INPUT_DIR    = Path("icons_in")     # folder containing your PNG icons
OUTPUT_DIR   = Path("icons_out")    # folder for high-quality outputs
INPUT_GLOB   = "*.png"              # which files to process
TARGET_SIZE  = 1024                 # output width/height in pixels
TARGET_COLOR = (0, 0, 0)            # recolor target (R, G, B)
PAD_FRAC     = 0.90                 # icon fills this fraction of the canvas
ALPHA_GAMMA  = 0.92                 # <1 thickens thin strokes; 1.0 = off
UNSHARP      = True                 # crisp the edges after resize
SUPERSAMPLE  = 2                    # render bigger, then downscale (AA boost)
DERIVE_ALPHA_IF_OPAQUE = True       # handle flat-background sources
# ─────────────────────────────────────────────────────────────


def load_clean(path: Path) -> Image.Image:
    """Open as upright RGBA, deriving alpha from luminance if needed."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)        # respect camera/exif rotation
    img = img.convert("RGBA")

    if DERIVE_ALPHA_IF_OPAQUE:
        a = np.asarray(img)[..., 3]
        # "Opaque" = almost every pixel fully solid → no usable matte.
        if (a > 250).mean() > 0.98:
            arr = np.asarray(img).astype(np.float64)
            rgb = arr[..., :3] / 255.0
            # Perceptual luminance; dark ink → high coverage.
            lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
            cov = np.clip(1.0 - lum, 0.0, 1.0)
            out = arr.copy()
            out[..., 3] = (cov * 255.0).round()
            img = Image.fromarray(out.astype(np.uint8), "RGBA")
    return img


def trim_transparent(img: Image.Image) -> Image.Image:
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def recolor_rgba(img: Image.Image, target_rgb) -> Image.Image:
    """Replace RGB on every visible pixel; keep alpha (= shape + AA)."""
    arr = np.asarray(img).copy()
    visible = arr[..., 3] > 0
    arr[visible, 0] = target_rgb[0]
    arr[visible, 1] = target_rgb[1]
    arr[visible, 2] = target_rgb[2]
    return Image.fromarray(arr, "RGBA")


def premultiplied_resize(img: Image.Image, new_w: int, new_h: int) -> Image.Image:
    """LANCZOS resize in premultiplied alpha → no edge halos."""
    arr = np.asarray(img).astype(np.float64) / 255.0
    rgb, a = arr[..., :3], arr[..., 3:4]
    pre = rgb * a                                    # premultiply

    def _rs(ch2d):
        im = Image.fromarray((np.clip(ch2d, 0, 1) * 255).round().astype(np.uint8))
        im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        return np.asarray(im).astype(np.float64) / 255.0

    pr = np.dstack([_rs(pre[..., i]) for i in range(3)])
    ar = _rs(a[..., 0])

    if ALPHA_GAMMA != 1.0:
        ar = np.power(np.clip(ar, 0, 1), ALPHA_GAMMA)

    eps = 1e-6
    out_rgb = np.where(ar[..., None] > eps, pr / np.maximum(ar[..., None], eps), 0.0)
    out = np.dstack([np.clip(out_rgb, 0, 1), np.clip(ar, 0, 1)])
    return Image.fromarray((out * 255).round().astype(np.uint8), "RGBA")


def fit_with_padding(img: Image.Image, size: int) -> Image.Image:
    """Scale to fit PAD_FRAC of a square canvas; center on transparency."""
    w, h = img.size
    scale = min((size * PAD_FRAC) / w, (size * PAD_FRAC) / h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    img = premultiplied_resize(img, new_w, new_h)

    if UNSHARP:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=80, threshold=0))

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(img, ((size - new_w) // 2, (size - new_h) // 2))
    return canvas


def process_image(path: Path) -> Path:
    img = load_clean(path)
    img = trim_transparent(img)
    img = recolor_rgba(img, TARGET_COLOR)

    # Supersample: work at N× then downscale for extra-smooth edges.
    work = TARGET_SIZE * max(1, SUPERSAMPLE)
    img = fit_with_padding(img, work)
    if work != TARGET_SIZE:
        img = premultiplied_resize(img, TARGET_SIZE, TARGET_SIZE)

    out_path = OUTPUT_DIR / f"{path.stem}_hq.png"
    img.save(out_path, format="PNG", optimize=True)
    return out_path


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(INPUT_DIR.glob(INPUT_GLOB))
    if not files:
        print(f"No PNG files found in: {INPUT_DIR.resolve()} (glob {INPUT_GLOB!r})")
        return
    for f in files:
        try:
            out = process_image(f)
            print(f"Saved: {out}")
        except Exception as e:
            print(f"FAILED {f.name}: {e}")


if __name__ == "__main__":
    main()
