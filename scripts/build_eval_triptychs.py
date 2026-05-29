#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BATCH_A = ROOT / "eval_results" / "final_batch"
DEFAULT_BATCH_B = ROOT / "eval_results" / "final_batch_v2"
DEFAULT_OUTPUT = ROOT / "eval_results" / "final_batch_triptychs"


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def fit_image(image: Image.Image, box: tuple[int, int], fill: tuple[int, int, int]) -> Image.Image:
    target_w, target_h = box
    src = image.convert("RGB")
    src.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), fill)
    x = (target_w - src.width) // 2
    y = (target_h - src.height) // 2
    canvas.paste(src, (x, y))
    return canvas


def find_original(case_dir: Path) -> Path | None:
    matches = sorted(
        (path for path in case_dir.iterdir() if path.is_file() and path.name.startswith("original.")),
        key=lambda path: natural_key(path.name),
    )
    return matches[0] if matches else None


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"] if bold else ["DejaVuSans.ttf", "Arial.ttf"]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_triptych(
    case_name: str,
    original_path: Path,
    generated_a_path: Path,
    generated_b_path: Path,
    output_path: Path,
) -> None:
    panel_w = 880
    panel_h = 620
    header_h = 62
    title_h = 54
    gutter = 18
    bg = (18, 22, 33)
    label_bg = (32, 40, 64)
    title_bg = (24, 30, 46)
    text = (245, 247, 250)
    muted = (178, 186, 204)

    original = Image.open(original_path)
    generated_a = Image.open(generated_a_path)
    generated_b = Image.open(generated_b_path)

    width = panel_w * 3 + gutter * 4
    height = title_h + header_h + panel_h + gutter * 3
    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(26, bold=True)
    header_font = load_font(24, bold=True)
    small_font = load_font(18)

    draw.rounded_rectangle((gutter, gutter, width - gutter, gutter + title_h), radius=12, fill=title_bg)
    draw.text((gutter + 18, gutter + 14), f"Case: {case_name}", font=title_font, fill=text)

    labels = [
        ("ORIGINAL", original),
        ("GENERATED RUN 1", generated_a),
        ("GENERATED RUN 2", generated_b),
    ]
    image_y = title_h + header_h + gutter * 2

    for index, (label, image) in enumerate(labels):
        x = gutter + index * (panel_w + gutter)
        header_top = title_h + gutter * 2
        draw.rounded_rectangle((x, header_top, x + panel_w, header_top + header_h), radius=10, fill=label_bg)
        draw.text((x + 16, header_top + 16), label, font=header_font, fill=text)
        draw.text((x + panel_w - 140, header_top + 20), case_name, font=small_font, fill=muted)
        fitted = fit_image(image, (panel_w, panel_h), bg)
        canvas.paste(fitted, (x, image_y))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)


def collect_cases(batch_a: Path, batch_b: Path, selected: set[str] | None) -> list[str]:
    names_a = {path.name for path in batch_a.iterdir() if path.is_dir()}
    names_b = {path.name for path in batch_b.iterdir() if path.is_dir()}
    common = names_a & names_b
    if selected:
        common &= selected
    valid: list[str] = []
    for case_name in sorted(common, key=natural_key):
        generated_a = batch_a / case_name / "generated_dashboard.png"
        generated_b = batch_b / case_name / "generated_dashboard.png"
        if generated_a.exists() and generated_b.exists():
            valid.append(case_name)
    return valid


def write_index(output_dir: Path, triptych_names: Iterable[str]) -> None:
    cards = "\n".join(
        (
            f"<article class=\"card\"><h2>{html.escape(name)}</h2>"
            f"<img src=\"{html.escape(name)}.png\" alt=\"{html.escape(name)}\"></article>"
        )
        for name in triptych_names
    )
    page = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Eval Triptychs</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #111827;
      --panel: #172033;
      --line: #273247;
      --text: #f5f7fa;
      --muted: #b2bacc;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "DejaVu Sans", sans-serif;
      background: radial-gradient(circle at top, #1f2a44, var(--bg) 42%);
      color: var(--text);
    }}
    main {{ max-width: 1880px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 32px; }}
    p {{ margin: 0 0 20px; color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 18px; }}
    .card {{ background: rgba(23, 32, 51, 0.92); border: 1px solid var(--line); border-radius: 16px; padding: 16px; }}
    .card h2 {{ margin: 0 0 12px; font-size: 20px; }}
    .card img {{ width: 100%; height: auto; display: block; border-radius: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Eval Triptychs</h1>
    <p>Each image shows: original, generated from run 1, generated from run 2.</p>
    <section class=\"grid\">{cards}</section>
  </main>
</body>
</html>
"""
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build triptych images from two eval batch directories.")
    parser.add_argument("--batch-a", type=Path, default=DEFAULT_BATCH_A)
    parser.add_argument("--batch-b", type=Path, default=DEFAULT_BATCH_B)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cases", nargs="*", help="Optional list of case directory names to include.")
    args = parser.parse_args()

    batch_a = args.batch_a.resolve()
    batch_b = args.batch_b.resolve()
    output_dir = args.output.resolve()
    selected = set(args.cases or []) or None

    if not batch_a.exists() or not batch_b.exists():
        raise SystemExit("Both batch directories must exist.")

    cases = collect_cases(batch_a, batch_b, selected)
    if not cases:
        raise SystemExit("No matching cases with generated_dashboard.png were found.")

    output_dir.mkdir(parents=True, exist_ok=True)
    built: list[str] = []
    for case_name in cases:
        case_dir_a = batch_a / case_name
        case_dir_b = batch_b / case_name
        original = find_original(case_dir_a) or find_original(case_dir_b)
        if original is None:
            print(f"skip {case_name}: original.* not found")
            continue
        generated_a = case_dir_a / "generated_dashboard.png"
        generated_b = case_dir_b / "generated_dashboard.png"
        output_path = output_dir / f"{case_name}.png"
        build_triptych(case_name, original, generated_a, generated_b, output_path)
        built.append(case_name)
        print(f"built {case_name} -> {output_path}")

    write_index(output_dir, built)
    print(f"built {len(built)} triptychs -> {output_dir}")
    print(f"index -> {output_dir / 'index.html'}")


if __name__ == "__main__":
    main()