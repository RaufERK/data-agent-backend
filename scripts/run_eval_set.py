"""
Runner для тест-наборов eval.

Использование:
    cd /home/user-tot/Desktop/data_agent

    # Запустить один набор:
    .venv/bin/python scripts/run_eval_set.py E
    .venv/bin/python scripts/run_eval_set.py A
    .venv/bin/python scripts/run_eval_set.py B
    .venv/bin/python scripts/run_eval_set.py C
    .venv/bin/python scripts/run_eval_set.py D

    # Запустить все наборы последовательно:
    .venv/bin/python scripts/run_eval_set.py all

    # Список наборов:
    .venv/bin/python scripts/run_eval_set.py --list

После прогона результаты в eval_results/set_X_*/
  baseline_report.json  — все метрики
  baseline_report.csv   — таблица
  C01/judgment.json     — оценка LLM для каждого кейса
  C01/comparison.png    — визуальное сравнение (если есть)
  C01/navigator_screenshot.png — скриншот Navigator
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval_sets import EVAL_SETS, IMAGES_DIR

VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
EVAL_SCRIPT = str(ROOT / "scripts" / "baseline_gold_eval.py")


def run_set(set_id: str, verbose: bool = True) -> int:
    cfg = EVAL_SETS[set_id]
    out_dir = ROOT / cfg["output"]

    print(f"\n{'='*70}")
    print(f"  Набор {set_id}: {cfg['name']}")
    print(f"  Цель: score >= {cfg['target_score']}/10")
    print(f"  Фокус: {cfg['fix_focus']}")
    print(f"  Изображения: {cfg['images']}")
    print(f"  Результаты: {out_dir}")
    print(f"{'='*70}")

    cmd = [
        VENV_PYTHON,
        EVAL_SCRIPT,
        "--images", *cfg["images"],
        "--images-dir", IMAGES_DIR,
        "--output", str(out_dir),
    ]

    result = subprocess.run(cmd, cwd=str(ROOT))
    return result.returncode


def list_sets() -> None:
    print("\nДоступные тест-наборы:\n")
    for sid, cfg in EVAL_SETS.items():
        print(f"  {sid}  {cfg['name']}")
        print(f"     Цель: score >= {cfg['target_score']}/10")
        print(f"     Фокус: {cfg['fix_focus']}")
        print(f"     Файлы: {', '.join(cfg['images'])}")
        print(f"     Вывод: {cfg['output']}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run eval test sets")
    parser.add_argument(
        "set_id",
        nargs="?",
        choices=[*list(EVAL_SETS.keys()), "all"],
        help="Набор для запуска: A, B, C, D, E или all",
    )
    parser.add_argument("--list", action="store_true", help="Показать список наборов")
    args = parser.parse_args()

    if args.list or not args.set_id:
        list_sets()
        return 0

    if args.set_id == "all":
        failed = []
        for sid in EVAL_SETS:
            rc = run_set(sid)
            if rc != 0:
                failed.append(sid)
        if failed:
            print(f"\nFAILED sets: {failed}")
            return 1
        print("\nAll sets completed OK")
        return 0

    return run_set(args.set_id)


if __name__ == "__main__":
    raise SystemExit(main())
