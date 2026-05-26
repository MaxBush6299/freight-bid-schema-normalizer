from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .services.pipeline_runner import run_pipeline


def _load_local_settings_env() -> None:
    settings_path = Path("local.settings.json")
    if not settings_path.exists():
        return

    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return

    values = payload.get("Values", {})
    if not isinstance(values, dict):
        return

    for key, value in values.items():
        if key not in os.environ and isinstance(value, str):
            os.environ[key] = value


def main() -> None:
    _load_local_settings_env()

    parser = argparse.ArgumentParser(
        description=(
            "Run the full core normalization pipeline locally "
            "(planner -> policy -> sandbox -> canonical output)."
        )
    )
    parser.add_argument(
        "--input",
        default="examples/inputs/Input 8.xlsx",
        help="Path to input workbook.",
    )
    parser.add_argument(
        "--output-root",
        default="artifacts/local_pipeline",
        help="Directory where run artifacts are written.",
    )
    parser.add_argument(
        "--run-mode",
        default="execute_with_validation",
        choices=["draft", "execute_with_validation"],
        help="Pipeline run mode.",
    )
    parser.add_argument(
        "--planner-mode",
        default="mock",
        choices=["mock", "live"],
        help="Planner mode: mock uses built-in script; live calls Foundry endpoint.",
    )
    args = parser.parse_args()

    result = run_pipeline(
        input_workbook=args.input,
        output_root=args.output_root,
        run_mode=args.run_mode,
        planner_mode=args.planner_mode,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
