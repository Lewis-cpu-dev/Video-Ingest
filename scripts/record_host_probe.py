#!/usr/bin/env python3
"""Compare an operator-captured host answer against private Gate 0 ground truth."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.probe import ProbeStore


def exact_answer(answer, truth) -> bool:
    """Require JSON types as well as values (Python otherwise equates True and 1)."""
    if type(answer) is not type(truth):
        return False
    if isinstance(truth, dict):
        return answer.keys() == truth.keys() and all(exact_answer(answer[k], v) for k, v in truth.items())
    if isinstance(truth, list):
        return len(answer) == len(truth) and all(exact_answer(a, b) for a, b in zip(answer, truth))
    return answer == truth


def workspace_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise argparse.ArgumentTypeError("Expected a file inside this workspace")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--host-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--account-mode", required=True)
    parser.add_argument("--connection", required=True,
                        choices=["secure-mcp-tunnel", "private-https-gateway", "local-stdio", "local-http"],
                        help="Actual tested connection, not a proposed deployment")
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".runtime/probes",
                        help="Same workspace probe directory passed to run_probe.py")
    parser.add_argument("--evidence", required=True, type=workspace_path,
                        help="Saved redacted actual-host transcript or screenshot, within workspace")
    answers = parser.add_mutually_exclusive_group(required=True)
    answers.add_argument("--answer-json", type=workspace_path,
                         help="Exact answer using the keys documented in compatibility_matrix.md")
    answers.add_argument("--unsupported", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    if args.unsupported and not args.reason.strip():
        parser.error("--unsupported requires --reason describing an observed limitation")
    if not args.evidence.stat().st_size:
        parser.error("--evidence must contain the captured host response")
    try:
        record, _ = ProbeStore(args.data_dir).record(args.probe_id)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    truth = dict(record["truth"])
    truth.pop("ready_at", None)
    answer = json.loads(args.answer_json.read_text()) if args.answer_json else None
    status = "unsupported" if args.unsupported else "passed" if exact_answer(answer, truth) else "failed"
    report = {"probe_id": args.probe_id, "kind": record["kind"], "status": status,
              "tested_at": datetime.now(UTC).isoformat(), "host": args.host,
              "host_version": args.host_version, "model": args.model, "account_mode": args.account_mode,
              "connection": args.connection,
              "evidence": str(args.evidence.relative_to(ROOT)),
              "evidence_sha256": hashlib.sha256(args.evidence.read_bytes()).hexdigest(),
              "answer": answer, "reason": args.reason,
              "verification_method": "operator-captured host answer compared to private generated ground truth"}
    output = ROOT / "docs/runs/host"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{args.probe_id}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"probe_id": args.probe_id, "status": status}))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
