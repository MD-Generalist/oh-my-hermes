#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "lib"))

from common import CONDITIONS, load_object  # noqa: E402
from corpus import generate_public  # noqa: E402
from runner import doctor, execute_one, run_matrix  # noqa: E402


def emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline-first OMH-native Hermes Agent model calibration benchmark"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--manifest", type=Path, default=BASE / "manifest.json")
    corpus_parser = sub.add_parser("corpus")
    corpus_parser.add_argument("--output", type=Path, default=BASE / "artifacts" / "corpus")
    corpus_parser.add_argument("--verify", action="store_true")
    for name in ("smoke", "run"):
        command = sub.add_parser(name)
        command.add_argument("--harness", choices=("fake", "omh", "hermes_current_session"), default="fake")
        command.add_argument("--manifest", type=Path, default=BASE / "manifest.json")
        command.add_argument("--model", action="append")
        command.add_argument("--condition", choices=CONDITIONS, default="baseline")
        command.add_argument("--split", choices=("development", "evaluation"), default="development")
        command.add_argument("--output", type=Path)
        command.add_argument("--omh-executable", default="omh")
        command.add_argument("--hermes-executable", default="hermes")
        command.add_argument(
            "--current-session-provider",
            help="Registered provider ID to use only with --harness hermes_current_session.",
        )
        command.add_argument("--allow-paid-live", action="store_true")
        command.add_argument("--max-paid-calls", type=int, default=0)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = doctor(BASE, args.manifest)
    elif args.command == "corpus":
        result = generate_public(BASE, args.output, verify=args.verify)
    else:
        if args.current_session_provider and args.harness != "hermes_current_session":
            parser.error("--current-session-provider requires --harness hermes_current_session")
        if args.harness in {"omh", "hermes_current_session"} and not args.allow_paid_live:
            parser.error("live Hermes harness requires --allow-paid-live")
        if args.harness in {"omh", "hermes_current_session"} and args.max_paid_calls < 1:
            parser.error("live Hermes harness requires --max-paid-calls")
        manifest = load_object(args.manifest)
        live_models = [item for item in manifest["models"] if item.get("live")]
        if args.model:
            live_models = [item for item in live_models if item["id"] in set(args.model)]
            if not live_models:
                parser.error("no manifest model matched --model")
        output = args.output or BASE / "artifacts" / args.condition / f"{args.split}-runs.jsonl"
        if args.command == "smoke":
            template_id, task_class, seed = ("D-RENAME", "edit", 7919)
            model = live_models[0] if args.harness in {"omh", "hermes_current_session"} else None
            result = execute_one(
                BASE,
                manifest,
                "development",
                template_id,
                task_class,
                seed,
                args.condition,
                output,
                args.harness,
                model=model,
                omh_executable=args.omh_executable,
                hermes_executable=args.hermes_executable,
                current_session_provider=args.current_session_provider,
            )
        else:
            result = run_matrix(
                BASE,
                manifest,
                args.split,
                args.condition,
                output,
                args.harness,
                models=live_models if args.harness in {"omh", "hermes_current_session"} else None,
                omh_executable=args.omh_executable,
                hermes_executable=args.hermes_executable,
                current_session_provider=args.current_session_provider,
                max_paid_calls=args.max_paid_calls,
            )
    emit(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
