#!/usr/bin/env python3
"""Issue #31 Phase C: 合成の5課題で指示改善の前後比較をローカル実演する。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot_learning.core import InvalidData, assert_external_output, digest, json_bytes
from copilot_learning.evals import task_fingerprint
from copilot_learning.instruction_effect_eval import (
    blank_observations, evaluate_instruction_effect,
)
from copilot_learning.skill_preview import ROOT, RESERVED, _path


def synthetic_demo() -> tuple[dict, dict]:
    """合成課題と自作の観測値。実Copilot成績は一切使用しない。"""
    tasks = [
        ("fix-python-import", "troubleshooting", "Find an import error in synthetic Python code."),
        ("project-facts", "project_facts", "Summarize a synthetic repository configuration."),
        ("resume-task", "session_resumption", "Continue a synthetic unfinished coding task."),
        ("review-preference", "preferences", "Follow the synthetic code review preference."),
        ("excluded-scope", "negative_scope", "Decline a synthetic out-of-scope task."),
    ]
    dataset = {
        "schema_version": "1.0", "record_type": "offline-instruction-effect-holdout",
        "label_source": "synthetic",
        "training_group_ids": ["training-series"],
        "training_task_sha256": [task_fingerprint("Synthetic unrelated training task.")],
        "proposal_review_sha256": digest(b"synthetic-phase-b-proposal"),
        "evaluation_protocol_sha256": digest(b"synthetic-rubric"),
        "instruction_snapshots": {
            "baseline": digest(b"synthetic-AGENTS-before"),
            "candidate": digest(b"synthetic-AGENTS-after"),
        },
        "cases": [
            {"case_id": key, "group_id": f"holdout-{i}", "family": family,
             "task": task, "task_sha256": task_fingerprint(task)}
            for i, (key, family, task) in enumerate(tasks)
        ],
    }
    profile = {"model_id": "synthetic.demo", "budget_tokens": 4096,
               "tool_policy": "read_only", "sampling_seed": 42}
    observations = {}
    outcomes = {
        "baseline": ["success", "success", "failure", "failure", "failure"],
        "candidate": ["success", "success", "success", "failure", "success"],
    }
    for arm, statuses in outcomes.items():
        cases = {}
        for i, (key, _, _) in enumerate(tasks):
            cases[key] = {
                "run_id": f"synthetic-{arm}-{i}",
                "trace_sha256": digest(json_bytes(["trace", arm, key])),
                "outcome": statuses[i],
                "reviewer_sha256": digest(b"synthetic-reviewer"),
                "review_receipt_sha256": digest(json_bytes(["review", arm, key])),
                "tool_calls": (8 - i) if arm == "baseline" else (6 - i),
                "retry_count": 2 if arm == "baseline" else 1,
                "input_tokens": 1000, "output_tokens": 200,
                "latency_ms": 3000 if arm == "baseline" else 2500,
                "cost_usd": 0.020 if arm == "baseline" else 0.018,
            }
        observations[arm] = {
            "source_kind": "synthetic",
            "instruction_sha256": dataset["instruction_snapshots"][arm],
            "profile": dict(profile),
            "executor_sha256": digest(b"synthetic-runner"),
            "cases": cases,
        }
    submission = {
        "schema_version": "1.0", "record_type": "offline-instruction-effect-observations",
        "dataset_sha256": digest(json_bytes(dataset)), "arms": observations,
    }
    return dataset, submission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Git管理外の新規フォルダ")
    args = parser.parse_args(argv)
    try:
        target = _path(args.out)
        assert_external_output(target, ROOT)
        if any(part.rstrip(" .").casefold() in RESERVED for part in target.parts) or target.exists():
            raise InvalidData("既存フォルダや自動読込先には出力できません")
        dataset, observed = synthetic_demo()
        comparison = evaluate_instruction_effect(dataset, observed)
        payloads = {
            "dataset.synthetic.json": json_bytes(dataset),
            "observations.synthetic.json": json_bytes(observed),
            "observations.blank.json": json_bytes(blank_observations(dataset)),
            "comparison.synthetic.json": json_bytes(comparison),
        }
        manifest = {"record_type": "offline-synthetic-instruction-effect-demo",
                    "files": {name: digest(data) for name, data in payloads.items()},
                    "real_copilot_execution_verified": False, "network_calls": 0}
        target.parent.mkdir(parents=True, exist_ok=True)
        _path(target.parent)
        target.mkdir(mode=0o700)
        created: list[Path] = []
        try:
            for name, data in (*sorted(payloads.items()), ("manifest.json", json_bytes(manifest))):
                file = target / name
                with file.open("xb") as f:
                    created.append(file)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
        except BaseException:
            for p in reversed(created):
                p.unlink()
            target.rmdir()
            raise
        print(json.dumps({
            "status": "synthetic_demo_only", "n_cases": comparison["n_cases"],
            "paired_delta_success_rate": comparison["paired_comparison"]["delta_success_rate"],
            "real_copilot_execution_verified": False, "network_calls": 0,
        }, sort_keys=True))
        return 0
    except (InvalidData, OSError, ValueError):
        print("ERROR: 合成デモの出力を完了できませんでした", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
