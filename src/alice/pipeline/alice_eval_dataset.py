#!/usr/bin/env python3
"""Validate and optionally load Alice regression eval cases into Phoenix."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from alice import repo_paths

ROOT = repo_paths.ROOT
DEFAULT_CASES = ROOT / "evals" / "alice_scope_regression_cases.jsonl"
DATASET_NAME = "alice-scope-regression"


REQUIRED_TOP_KEYS = {"id", "input", "expected", "metadata"}


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        missing = REQUIRED_TOP_KEYS - set(rec)
        if missing:
            raise ValueError(f"{path}:{line_no} missing keys {sorted(missing)}")
        if not rec["input"].get("user_text"):
            raise ValueError(f"{path}:{line_no} missing input.user_text")
        if not rec["expected"].get("classification"):
            raise ValueError(f"{path}:{line_no} missing expected.classification")
        cases.append(rec)
    if not cases:
        raise ValueError(f"{path} contained no cases")
    return cases


def phoenix_examples(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "input": {
                "user_text": case["input"]["user_text"],
                "context": case["input"].get("context", ""),
            },
            "output": {
                "classification": case["expected"]["classification"],
                "must_not_include": case["expected"].get("must_not_include", []),
                "must_include_any": case["expected"].get("must_include_any", []),
                "required_tools_any": case["expected"].get("required_tools_any", []),
            },
            "metadata": {
                "id": case["id"],
                "layer": case["metadata"].get("layer", ""),
                "failure_mode": case["metadata"].get("failure_mode", ""),
                "source": case["metadata"].get("source", ""),
            },
        }
        for case in cases
    ]


def load_into_phoenix(cases: list[dict[str, Any]], *, name: str = DATASET_NAME):
    from phoenix.client import Client

    return Client(base_url="http://localhost:6006").datasets.create_dataset(
        name=name,
        examples=phoenix_examples(cases),
        input_keys=("user_text", "context"),
        output_keys=("classification", "must_not_include", "must_include_any", "required_tools_any"),
        metadata_keys=("id", "layer", "failure_mode", "source"),
        dataset_description="Alice regression cases for scope, paste buffering, observability, and guardrails.",
        timeout=10,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--load-phoenix", action="store_true")
    parser.add_argument("--name", default=DATASET_NAME)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.load_phoenix:
        dataset = load_into_phoenix(cases, name=args.name)
        print(json.dumps({"dataset": getattr(dataset, "name", args.name), "cases": len(cases)}, sort_keys=True))
    else:
        print(json.dumps({"cases": len(cases), "path": str(args.cases)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
