#!/usr/bin/env python3
"""Batch screenshot eval for image-dashboard reconstruction.

For every source dashboard image:
  1. Upload it into the local frontend and wait for reconstructed dashboard.
  2. Save generated screenshot.
  3. Save side-by-side comparison PNG.
  4. Ask the configured vision LLM for a strict 1..10 similarity score.
  5. Write JSON/CSV/Markdown reports.

The script is intentionally domain-agnostic: scoring is based on visual
similarity criteria, not dashboard-specific names.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DEFAULT_INPUT = Path("/home/user-tot/Desktop/pannels/gold_dash/дэши")
DEFAULT_OUTPUT = ROOT / "eval_results" / "gold_dash_batch"
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:3001")
VISION_MODEL = os.environ.get("GIGACHAT_VISION_MODEL", "GigaChat-2-Max")
VISION_BASE_URL = os.environ.get("GPT2GIGA_URL", "http://localhost:8090/v1")
VISION_API_KEY = os.environ.get("GPT2GIGA_API_KEY", "dummy-local-key")


@dataclass
class BatchResult:
    index: int
    total: int
    image_name: str
    generated_screenshot: str = ""
    comparison_png: str = ""
    score: int = 0
    verdict: str = ""
    strengths: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    fix_recommendations: list[str] = field(default_factory=list)
    error: str = ""
    seconds: float = 0.0


def natural_key(path: Path) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def progress_line(index: int, total: int, status: str, name: str, extra: str = "") -> None:
    width = 28
    done = int(width * index / max(total, 1))
    bar = "#" * done + "-" * (width - done)
    suffix = f" | {extra}" if extra else ""
    print(f"[{index:02d}/{total:02d}] [{bar}] {status:<12} {name}{suffix}", flush=True)


def image_to_data_url(path: Path) -> str:
    data = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if ext in {"jpg", "jpeg"} else f"image/{ext or 'png'}"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def fit_image(image: Image.Image, box: tuple[int, int], fill: tuple[int, int, int]) -> Image.Image:
    target_w, target_h = box
    src = image.convert("RGB")
    src.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), fill)
    x = (target_w - src.width) // 2
    y = (target_h - src.height) // 2
    canvas.paste(src, (x, y))
    return canvas


def make_comparison(original: Path, generated: Path, output: Path, score: int | None, verdict: str = "") -> None:
    orig = Image.open(original)
    gen = Image.open(generated)
    panel_w = 960
    panel_h = 640
    header_h = 58
    gutter = 18
    bg = (18, 22, 33)
    label_bg = (32, 40, 64)
    text = (245, 247, 250)
    muted = (178, 186, 204)

    canvas = Image.new("RGB", (panel_w * 2 + gutter * 3, panel_h + header_h + gutter * 2), bg)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
        small = ImageFont.truetype("DejaVuSans.ttf", 17)
    except Exception:
        font = ImageFont.load_default()
        small = ImageFont.load_default()

    left_x = gutter
    right_x = panel_w + gutter * 2
    img_y = header_h + gutter
    for x, label in ((left_x, "ORIGINAL"), (right_x, "GENERATED")):
        draw.rounded_rectangle((x, gutter, x + panel_w, header_h), radius=10, fill=label_bg)
        draw.text((x + 16, gutter + 15), label, font=font, fill=text)
    score_label = f"STRICT SCORE: {score}/10" if score else "STRICT SCORE: n/a"
    draw.text((right_x + panel_w - 260, gutter + 18), score_label, font=small, fill=muted)

    canvas.paste(fit_image(orig, (panel_w, panel_h), bg), (left_x, img_y))
    canvas.paste(fit_image(gen, (panel_w, panel_h), bg), (right_x, img_y))

    if verdict:
        wrapped = textwrap.wrap(verdict, width=118)[:2]
        for line_idx, line in enumerate(wrapped):
            draw.text((gutter, canvas.height - gutter - 40 + line_idx * 20), line, font=small, fill=muted)

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)


def screenshot_generated_dashboard(image_path: Path, output_path: Path, timeout_ms: int) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 2048, "height": 1220}, device_scale_factor=1)
        page.goto(FRONTEND_URL, wait_until="networkidle", timeout=20_000)

        file_input = page.locator("input[type='file'][accept*='.png'], input[type='file'][accept*='image']").first
        if file_input.count() == 0:
            file_input = page.locator("input[type='file']").first
        file_input.set_input_files(str(image_path))

        btn = page.get_by_role("button", name=re.compile("ПОСТРОИТЬ ДАШБОРД", re.IGNORECASE)).first
        btn.wait_for(state="visible", timeout=timeout_ms)
        btn.click()

        page.get_by_text("Опубликовать в DataLens").first.wait_for(timeout=90_000)
        # Wait until the dashboard content has stabilised: no "Анализируем" spinner,
        # publish button visible, and enough time has passed for cards to render.
        # Accept any number of cards (including 0) — some dashboards legitimately have
        # few or no recognised widgets, and a blank canvas is still a valid screenshot.
        page.wait_for_function(
            """() => {
                const body = document.body.innerText || "";
                if (body.includes("Анализируем макет")) return false;
                if (!body.includes("Опубликовать в DataLens")) return false;
                // Dashboard panel has mounted — content is ready for screenshot
                return true;
            }""",
            timeout=90_000,
        )
        page.wait_for_timeout(1800)
        page.evaluate(
            """() => {
                for (const el of Array.from(document.querySelectorAll("*"))) {
                    const rect = el.getBoundingClientRect();
                    const text = el.textContent || "";
                    if (rect.left < 85 && rect.height > 500 && rect.width < 95) {
                        el.style.setProperty("display", "none", "important");
                    }
                    if (text.includes("ИИ-ассистент") && rect.left > window.innerWidth * 0.55 && rect.height > 250) {
                        el.style.setProperty("display", "none", "important");
                    }
                }
                document.documentElement.style.overflow = "hidden";
                document.body.style.overflow = "hidden";
                window.scrollTo(0, 0);
            }"""
        )
        page.wait_for_timeout(500)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output_path), full_page=False)
        browser.close()


def strict_judge(original: Path, generated: Path) -> dict[str, Any]:
    client = OpenAI(base_url=VISION_BASE_URL, api_key=VISION_API_KEY)
    prompt = (
        "Ты evaluator визуального восстановления BI dashboard по скриншоту.\n"
        "Сравни ORIGINAL и GENERATED. Оцени только визуальную похожесть, не доменную корректность.\n"
        "Критерии (равный вес):\n"
        "  1. Количество виджетов совпадает\n"
        "  2. Типы виджетов совпадают (график→график, таблица→таблица, KPI→KPI)\n"
        "  3. Layout и позиции блоков\n"
        "  4. Цвета, фон, контраст\n"
        "  5. Сохранность KPI-чисел, легенд, таблиц\n"
        "  6. Нет пустых или явно неверных виджетов\n\n"
        "Шкала 1-10 (используй всю шкалу!):\n"
        "  10 — почти пиксельно похоже\n"
        "   8 — узнаваемо, мелкие отличия в деталях\n"
        "   6 — правильная структура, часть виджетов неточна\n"
        "   4 — основная идея угадывается, есть потери типов или KPI\n"
        "   3 — количество виджетов примерно совпадает но типы или layout сильно отличаются\n"
        "   2 — структура есть, но большинство виджетов неверны\n"
        "   1 — практически ничего не совпадает\n\n"
        "Важно: если количество виджетов и их типы совпадают хотя бы приблизительно — "
        "это уже не меньше 3 баллов. Не занижай оценку из-за цветовых отличий или мелких деталей.\n\n"
        "Ответь строго JSON без markdown:\n"
        "{\n"
        '  "score": <число от 1 до 10>,\n'
        '  "strengths": ["..."],\n'
        '  "issues": ["..."],\n'
        '  "fix_recommendations": ["..."],\n'
        '  "verdict": "..."\n'
        "}"
    )
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt + "\n\nORIGINAL:"},
                {"type": "image_url", "image_url": {"url": image_to_data_url(original)}},
                {"type": "text", "text": "GENERATED:"},
                {"type": "image_url", "image_url": {"url": image_to_data_url(generated)}},
            ],
        }],
        max_tokens=1200,
        timeout=120,
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    parsed = json.loads(raw)
    parsed["score"] = max(1, min(10, int(parsed.get("score") or 1)))
    return parsed


def run_one(image_path: Path, index: int, total: int, output_dir: Path, timeout_ms: int, resume: bool) -> BatchResult:
    started = time.time()
    case_dir = output_dir / image_path.stem
    case_dir.mkdir(parents=True, exist_ok=True)
    original_copy = case_dir / f"original{image_path.suffix.lower()}"
    generated = case_dir / "generated_dashboard.png"
    comparison = case_dir / "comparison.png"
    judgment_path = case_dir / "judgment.json"
    result_path = case_dir / "result.json"

    if not original_copy.exists():
        shutil.copy2(image_path, original_copy)

    if resume and result_path.exists() and generated.exists() and comparison.exists():
        data = json.loads(result_path.read_text(encoding="utf-8"))
        result = BatchResult(**data)
        progress_line(index, total, "skip", image_path.name, f"score={result.score}/10")
        return result

    result = BatchResult(index=index, total=total, image_name=image_path.name)
    try:
        progress_line(index, total, "render", image_path.name)
        screenshot_generated_dashboard(image_path, generated, timeout_ms=timeout_ms)
        result.generated_screenshot = str(generated)

        progress_line(index, total, "judge", image_path.name)
        judgment = strict_judge(image_path, generated)
        judgment_path.write_text(json.dumps(judgment, ensure_ascii=False, indent=2), encoding="utf-8")
        result.score = int(judgment.get("score") or 0)
        result.verdict = str(judgment.get("verdict") or "")
        result.strengths = list(judgment.get("strengths") or [])
        result.issues = list(judgment.get("issues") or [])
        result.fix_recommendations = list(judgment.get("fix_recommendations") or [])

        make_comparison(image_path, generated, comparison, result.score, result.verdict)
        result.comparison_png = str(comparison)
        progress_line(index, total, "done", image_path.name, f"score={result.score}/10")
    except PlaywrightTimeoutError as exc:
        result.error = f"playwright timeout: {exc}"
        progress_line(index, total, "failed", image_path.name, "timeout")
        if generated.exists():
            make_comparison(image_path, generated, comparison, None, result.error)
            result.generated_screenshot = str(generated)
            result.comparison_png = str(comparison)
    except Exception as exc:
        result.error = str(exc)
        progress_line(index, total, "failed", image_path.name, result.error[:90])
        if generated.exists():
            make_comparison(image_path, generated, comparison, None, result.error)
            result.generated_screenshot = str(generated)
            result.comparison_png = str(comparison)
    finally:
        result.seconds = round(time.time() - started, 2)
        result_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def write_reports(results: list[BatchResult], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "batch_results.json"
    csv_path = output_dir / "batch_results.csv"
    md_path = output_dir / "summary.md"

    json_path.write_text(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "index", "image_name", "score", "error", "seconds", "generated_screenshot",
            "comparison_png", "issues", "fix_recommendations", "verdict",
        ])
        writer.writeheader()
        for item in results:
            writer.writerow({
                "index": item.index,
                "image_name": item.image_name,
                "score": item.score,
                "error": item.error,
                "seconds": item.seconds,
                "generated_screenshot": item.generated_screenshot,
                "comparison_png": item.comparison_png,
                "issues": " | ".join(item.issues),
                "fix_recommendations": " | ".join(item.fix_recommendations),
                "verdict": item.verdict,
            })

    scored = [item.score for item in results if item.score > 0]
    avg = sum(scored) / len(scored) if scored else 0.0
    issue_counts: dict[str, int] = {}
    fix_counts: dict[str, int] = {}
    for item in results:
        for issue in item.issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
        for fix in item.fix_recommendations:
            fix_counts[fix] = fix_counts.get(fix, 0) + 1

    lines = [
        "# Gold Dash Batch Eval",
        "",
        f"- Images: {len(results)}",
        f"- Scored: {len(scored)}",
        f"- Average strict score: {avg:.2f}/10",
        f"- Failed: {sum(1 for item in results if item.error)}",
        "",
        "## Per Image",
        "",
        "| # | Image | Score | Error |",
        "|---:|---|---:|---|",
    ]
    for item in results:
        lines.append(f"| {item.index} | {item.image_name} | {item.score or '-'} | {item.error[:90]} |")
    lines.extend(["", "## Top Issues", ""])
    for issue, count in sorted(issue_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        lines.append(f"- {count}x {issue}")
    lines.extend(["", "## Suggested Fix Themes", ""])
    for fix, count in sorted(fix_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        lines.append(f"- {count}x {fix}")
    lines.extend(["", f"JSON: `{json_path}`", f"CSV: `{csv_path}`"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nBatch report:")
    print(f"  avg_score={avg:.2f}/10 scored={len(scored)}/{len(results)} failed={sum(1 for item in results if item.error)}")
    print(f"  json={json_path}")
    print(f"  csv={csv_path}")
    print(f"  summary={md_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    images = sorted(
        [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}],
        key=natural_key,
    )
    if args.limit:
        images = images[:args.limit]
    if not images:
        print(f"No images found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Batch dashboard eval: {len(images)} image(s)")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir}")
    print(f"Frontend: {FRONTEND_URL}")
    print(f"Judge: {VISION_BASE_URL} / {VISION_MODEL}")

    results: list[BatchResult] = []
    total = len(images)
    for idx, image in enumerate(images, start=1):
        results.append(run_one(image, idx, total, output_dir, args.timeout_ms, resume=not args.no_resume))
        write_reports(results, output_dir)

    write_reports(results, output_dir)


if __name__ == "__main__":
    main()
