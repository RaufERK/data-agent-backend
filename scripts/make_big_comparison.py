"""Build one big PNG: for each of 36 cases show original | old navigator | new navigator."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OLD_DIR = Path("eval_results/final_all_36_20260520")
NEW_DIR = Path("eval_results/final_all_36_20260520_rerun")
OUT = Path("eval_results/big_comparison_36.png")

# case_id -> image filename mapping for C01-C17 in rerun (came from 17-problem-case run)
C01_17_IMAGES = {
    "C01": "10.png", "C02": "12.png", "C03": "14.png", "C04": "15.png",
    "C05": "17.png", "C06": "18.png", "C07": "19.png", "C08": "2.png",
    "C09": "20.png", "C10": "22.png", "C11": "23.png", "C12": "25.png",
    "C13": "29.png", "C14": "3.png",  "C15": "31.png", "C16": "5.png",
    "C17": "photo_2026-02-26_17-00-00.jpg",
}

import json

# Build image->old_case_id from old run
old_report = json.load(open(OLD_DIR / "baseline_report.json"))
img_to_old_case = {r["image"]: r["case_id"] for r in old_report}

# Build new case_id->image for C18-C36
new_report = json.load(open(NEW_DIR / "baseline_report.json"))
c18_36_images = {r["case_id"]: r["image"] for r in new_report}

all_new_images = {**C01_17_IMAGES, **c18_36_images}

# Scores
old_scores = {r["case_id"]: r["visual_score"] for r in old_report}
new_scores_by_cid = {}
for cid in all_new_images:
    j = NEW_DIR / cid / "judgment.json"
    if j.exists():
        new_scores_by_cid[cid] = json.load(open(j)).get("score", "?")
    else:
        new_scores_by_cid[cid] = "?"

THUMB_W = 480
THUMB_H = 300
PADDING = 8
HEADER_H = 36
LABEL_H = 24
COLS = 3  # original | old | new

# Try to load a font
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
except:
    font = ImageFont.load_default()
    font_sm = font

CELL_W = THUMB_W + PADDING * 2
CELL_H = THUMB_H + HEADER_H + LABEL_H + PADDING * 2
ROW_H = CELL_H + PADDING

cases = sorted(all_new_images.keys())
N = len(cases)
GRID_COLS = 1  # one row = 3 images side by side per case
IMG_W = COLS * CELL_W + (COLS + 1) * PADDING
IMG_H = N * ROW_H + PADDING

print(f"Canvas: {IMG_W} x {IMG_H}  ({N} cases)")
canvas = Image.new("RGB", (IMG_W, IMG_H), (30, 30, 30))
draw = ImageDraw.Draw(canvas)

def load_thumb(path: Path) -> Image.Image:
    if not path.exists():
        img = Image.new("RGB", (THUMB_W, THUMB_H), (60, 60, 60))
        d = ImageDraw.Draw(img)
        d.text((10, THUMB_H // 2 - 10), "no image", fill=(180, 180, 180))
        return img
    img = Image.open(path).convert("RGB")
    img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
    # pad to exact size
    out = Image.new("RGB", (THUMB_W, THUMB_H), (45, 45, 45))
    x = (THUMB_W - img.width) // 2
    y = (THUMB_H - img.height) // 2
    out.paste(img, (x, y))
    return out

def delta_color(d):
    if d == "?" or d == 0: return (200, 200, 200)
    return (80, 220, 80) if d > 0 else (220, 80, 80)

for row_idx, cid in enumerate(cases):
    img_name = all_new_images[cid]
    old_cid = img_to_old_case.get(img_name, cid)
    old_score = old_scores.get(old_cid, "?")
    new_score = new_scores_by_cid.get(cid, "?")
    delta = (new_score - old_score) if isinstance(new_score, int) and isinstance(old_score, int) else "?"

    y_base = PADDING + row_idx * ROW_H

    # Column paths: original, old navigator, new navigator
    orig_path = NEW_DIR / cid / "original.png"
    old_nav = OLD_DIR / old_cid / "navigator_screenshot.png"
    new_nav = NEW_DIR / cid / "navigator_screenshot.png"

    col_data = [
        ("Оригинал", orig_path, None),
        (f"Старый  {old_score}/10", old_nav, None),
        (f"Новый  {new_score}/10  (Δ {delta:+d})" if isinstance(delta, int) else f"Новый  {new_score}/10", new_nav, delta),
    ]

    for col_idx, (label, path, d) in enumerate(col_data):
        x_base = PADDING + col_idx * (CELL_W + PADDING)

        # Background cell
        bg = (45, 45, 55) if col_idx == 0 else (40, 50, 40) if (isinstance(d, int) and d > 0) else (50, 40, 40) if (isinstance(d, int) and d < 0) else (45, 45, 45)
        draw.rectangle([x_base, y_base, x_base + CELL_W, y_base + CELL_H], fill=bg, outline=(80, 80, 80))

        # Case label on first col
        if col_idx == 0:
            draw.text((x_base + PADDING, y_base + 4), f"{cid} — {img_name}", font=font, fill=(255, 220, 100))
        else:
            col_color = delta_color(d) if d is not None else (200, 200, 200)
            draw.text((x_base + PADDING, y_base + 4), label, font=font, fill=col_color)

        # Thumbnail
        thumb = load_thumb(path)
        canvas.paste(thumb, (x_base + PADDING, y_base + HEADER_H))

    print(f"  [{row_idx+1}/{N}] {cid} {img_name}  {old_score} → {new_score}")

canvas.save(OUT, optimize=True)
print(f"\nSaved: {OUT}  ({OUT.stat().st_size // 1024} KB)")
