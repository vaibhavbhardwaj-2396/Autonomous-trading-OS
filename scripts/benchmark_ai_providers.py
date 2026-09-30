#!/usr/bin/env python3
"""Run the bounded historical ResearchPacket provider benchmark."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.brain.provider_benchmark import run_benchmark
from research.store import Store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", action="append", required=True,
                        help="provider:model (repeat up to four times)")
    parser.add_argument("--cases", type=int, default=3)
    args = parser.parse_args()
    targets = []
    for raw in args.target:
        provider, sep, model = raw.partition(":")
        if not sep or not provider or not model:
            parser.error("each --target must be provider:model")
        targets.append({"provider": provider, "model": model})
    with Store.open() as store:
        result = run_benchmark(store, targets, limit=args.cases)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("COMPLETE", "NO_REPRESENTATIVE_PACKETS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
