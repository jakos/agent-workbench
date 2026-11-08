# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Frozen PR-review evaluation. Offline integrity checks never contact a model."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pr_review


def digest(value: object) -> str:
    return hashlib.sha256(pr_review.encode(value).encode("utf-8")).hexdigest()


def oracle(case: dict) -> bool:
    """Independent structural/behavioral counterexamples; never execute case code."""
    text = case["file"]["new"]
    kind = case["oracle"]
    if kind == "hook-envelope":
        return "hooks" not in json.loads(text)
    if kind == "duplicate-hook":
        return json.loads(text).get("hooks") == "./hooks/hooks.json"
    if kind == "global-marketplace":
        return "extraKnownMarketplaces" in json.loads(text)
    if kind == "windows-shell":
        return json.loads(text)["hooks"]["PostToolUse"][0]["hooks"][0][
            "command"
        ].startswith("bash ")
    if kind == "space-path":
        return "$PROJECT" in text and '"$PROJECT"' not in text
    if kind == "bsd-sed":
        return r"s/\t/" in text
    if kind == "clean-hook":
        return not isinstance(json.loads(text).get("hooks"), dict)
    if kind == "clean-command":
        spec = json.loads(text)["hooks"]["PostToolUse"][0]["hooks"][0]
        return spec["command"] != "uv" or spec.get("args", [])[:3] != [
            "run",
            "--quiet",
            "--script",
        ]
    raise ValueError("unknown local oracle")


def load(path: Path) -> list[dict]:
    cases = pr_review.decode(path.read_text(encoding="utf-8"))
    if (
        not isinstance(cases, list)
        or len(cases) != 8
        or len({c["id"] for c in cases}) != 8
    ):
        raise ValueError("evaluation requires eight unique frozen cases")
    for case in cases:
        if (
            not pr_review.SHA.fullmatch(case["source_commit"])
            or case["variant"] != "reconstructed"
        ):
            raise ValueError("invalid provenance")
        if (
            digest(case["file"]) != case["input_digest"]
            or oracle(case) != case["defect"]
        ):
            raise ValueError("case digest or independent oracle failed")
        if not case["scenario"] or not pr_review.risk.safe_path(case["file"]["path"]):
            raise ValueError("incomplete case")
    if sum(c["defect"] for c in cases) != 6:
        raise ValueError("evaluation requires six defects and two controls")
    return cases


def evidence(case: dict, policy: str) -> dict:
    return {
        "repository": "fixture/review",
        "number": 1,
        "base": "a" * 40,
        "head": "b" * 40,
        "merge_base": "a" * 40,
        "risk": "high",
        "policy": policy,
        "files": [case["file"]],
    }


def manifest(cases: list[dict], policy: str, config: dict, ceiling: float) -> dict:
    return {
        "inputs": [c["input_digest"] for c in cases],
        "cases_digest": digest(cases),
        "policy_digest": digest(policy),
        "config_digest": digest(config),
        "provider": config["provider"],
        "model": config["model"],
        "calls": 8,
        "max_usd": ceiling,
    }


def score(case: dict, result: dict) -> dict:
    complete = result["coverage"] == "complete"
    # Location and a scenario-specific term are both required. Mechanical score is
    # evidence to inspect, not a substitute for independent human semantic judgement.
    hits = [
        f
        for f in result["findings"]
        if f["path"] == case["file"]["path"]
        and any(
            word in (f["title"] + " " + f["scenario"]).lower() for word in case["terms"]
        )
    ]
    return {
        "detected": bool(hits) if complete and case["defect"] else None,
        "false_positives": len(result["findings"])
        if complete and not case["defect"]
        else None,
        "failed_attempt": not complete,
        "defect_evidence": hits,
    }


def evaluate(
    cases: list[dict],
    policy: str,
    config: dict,
    adapter: pr_review.Adapter,
    ceiling: float,
    transport,
    *,
    live: bool = False,
    secret: str = "evaluation-placeholder",
) -> dict:
    if not 0 < ceiling <= 4 or not config["enabled"]:
        raise ValueError("invalid evaluation budget or disabled config")
    prepared = [evidence(case, policy) for case in cases]
    if len(prepared) != 8:
        raise ValueError("exactly eight calls maximum")
    bounds = [pr_review.preflight(config, item, adapter)[2] for item in prepared]
    if sum(bounds) > ceiling:
        raise ValueError("aggregate preflight refused")
    report = {
        "mode": "live" if live else "fake-only",
        "manifest": manifest(cases, policy, config, ceiling),
        "reserved_usd": sum(bounds),
        "results": [],
    }
    for case, item in zip(cases, prepared):
        # The caller injects a transport with credentials; no credential is retained.
        result = pr_review.review(config, item, secret, adapter, transport)
        report["results"].append(
            {
                "id": case["id"],
                "input_digest": case["input_digest"],
                **score(case, result),
                "review": result,
            }
        )
    costs = [row["review"]["cost_usd"] for row in report["results"]]
    report["known_usd"] = sum(c for c in costs if c is not None)
    report["unknown_cost_attempts"] = sum(c is None for c in costs)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("offline", "prepare-live", "live"))
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parent / "pr-review/cases.json",
    )
    parser.add_argument(
        "--policy", type=Path, default=Path(__file__).resolve().parents[3] / "REVIEW.md"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[3] / pr_review.CONFIG,
    )
    parser.add_argument("--max-usd", type=float, default=4.0)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        cases = load(args.cases)
        if args.command == "offline":
            print("8 frozen cases verified; semantic detection UNMEASURED")
            return 0
        policy = args.policy.read_text(encoding="utf-8")
        config = pr_review.parse_config(args.config.read_text(encoding="utf-8"))
        if not 0 < args.max_usd <= 4:
            raise ValueError()
        plan = manifest(cases, policy, config, args.max_usd)
        if args.command == "prepare-live":
            print(
                pr_review.encode(
                    {"manifest": plan, "confirmation_digest": digest(plan)}
                )
            )
            return 0
        if args.confirm != digest(plan) or args.output is None or not config["enabled"]:
            raise ValueError()
        import os

        secret = os.environ.get(config["secret_name"], "")
        if not secret:
            raise ValueError()
        adapter = pr_review.adapter_for(config["provider"])

        def transport(
            url: str,
            method: str,
            headers: dict,
            body: dict,
            timeout: int,
            hosts: frozenset,
        ) -> dict:
            return pr_review.http_json(
                url, method, adapter.headers(secret), body, timeout, hosts
            )

        report = evaluate(
            cases,
            policy,
            config,
            adapter,
            args.max_usd,
            transport,
            live=True,
            secret=secret,
        )
        pr_review.save(args.output, report)
        return 0
    except (ValueError, KeyError, TypeError, OSError):
        print(
            "evaluation refused invalid evidence, confirmation or budget",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
