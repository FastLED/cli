"""Fail closed unless every declared platform workflow passed on one SHA."""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).with_name("full_coverage.json")
UTC = dt.timezone.utc
PLATFORMS = {
    "linux-x86",
    "linux-arm",
    "windows-x86",
    "windows-arm",
    "macos-x86",
    "macos-arm",
}
# The x86 Linux lint workflow also serves ordinary PRs. These two jobs
# are expected to skip during exact-SHA dispatch and ci-full PR runs.
PR_ONLY_JOBS = {
    "linux-x86-lint.yml": {"Verify local attestations", "Linux x86 Unit Test"},
}
POLL_SECONDS = 120  # 29 workflows × runs/jobs endpoints; stay well below API limits.


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def cell_result(run, jobs, sha, event, earliest, *, pr_only_jobs=()):
    if run["head_sha"] != sha or run["event"] != event:
        return "wrong-sha-or-event"
    if parse_time(run["created_at"]) < earliest:
        return "stale"
    if run["status"] != "completed":
        return "pending"
    if run["conclusion"] != "success":
        return run["conclusion"] or "failed"
    if not any(job.get("name") not in pr_only_jobs for job in jobs):
        return "missing-jobs"
    if any(
        job["conclusion"] != "success"
        and not (job.get("name") in pr_only_jobs and job["conclusion"] == "skipped")
        for job in jobs
    ):
        return "skipped-or-failed-job"
    return "passed"


def test_execution_gaps(cells, platform_tests):
    """Return every platform/step lacking a successful executed test step."""
    gaps = []
    for platform, checks in platform_tests.items():
        for check in checks:
            cell = cells.get(check["workflow"], {})
            executed = any(
                step.get("name") == check["step"]
                and step.get("conclusion") == "success"
                for job in cell.get("jobs", [])
                if job.get("conclusion") == "success"
                for step in job.get("steps", [])
            )
            if not executed:
                gaps.append(f"{platform}: {check['workflow']} / {check['step']}")
    return gaps


def api(path):
    url = "https://api.github.com/" + path.lstrip("/")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pages(path, key):
    result = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        batch = api(f"{path}{separator}per_page=100&page={page}")[key]
        result.extend(batch)
        if len(batch) < 100:
            return result
        page += 1


def collect(repo, sha, event, earliest, workflows):
    report = {}
    for workflow in workflows:
        path = urllib.parse.quote(workflow)
        runs = pages(
            f"repos/{repo}/actions/workflows/{path}/runs?head_sha={sha}&event={event}",
            "workflow_runs",
        )
        eligible = [run for run in runs if parse_time(run["created_at"]) >= earliest]
        if not eligible:
            report[workflow] = {"result": "missing-run"}
            continue
        run = max(eligible, key=lambda item: (item["created_at"], item["id"]))
        jobs = []
        if run["status"] == "completed":
            jobs = pages(f"repos/{repo}/actions/runs/{run['id']}/jobs", "jobs")
        report[workflow] = {
            "result": cell_result(
                run,
                jobs,
                sha,
                event,
                earliest,
                pr_only_jobs=PR_ONLY_JOBS.get(workflow, ()),
            ),
            "run_id": run["id"],
            "url": run["html_url"],
            "jobs": [
                {
                    "name": job["name"],
                    "conclusion": job["conclusion"],
                    "steps": [
                        {"name": step["name"], "conclusion": step["conclusion"]}
                        for step in job.get("steps", [])
                    ],
                }
                for job in jobs
            ],
        }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument(
        "--event", choices=("pull_request", "workflow_dispatch"), required=True
    )
    parser.add_argument("--coverage-run-id", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    args = parser.parse_args()
    if len(args.sha) != 40 or any(c not in "0123456789abcdef" for c in args.sha):
        parser.error("--sha must be a lowercase 40-character commit SHA")
    repo = os.environ["GITHUB_REPOSITORY"]
    manifest = json.loads(MANIFEST.read_text())
    if manifest["schema_version"] != 2 or not manifest["workflows"]:
        parser.error("empty or unsupported coverage manifest")
    if set(manifest.get("platform_tests", {})) != PLATFORMS:
        parser.error("coverage manifest omits a supported platform test contract")
    for checks in manifest["platform_tests"].values():
        if not checks or any(
            check["workflow"] not in manifest["workflows"] for check in checks
        ):
            parser.error(
                "coverage manifest contains an empty or undeclared platform test"
            )
    for platform, checks in manifest["platform_tests"].items():
        if any(not check["workflow"].startswith(platform + "-") for check in checks):
            parser.error("coverage manifest maps a platform to another platform's test")
    own_run = api(f"repos/{repo}/actions/runs/{args.coverage_run_id}")
    if own_run["head_sha"] != args.sha:
        parser.error("coverage workflow itself is not running on the requested SHA")
    # PR labels require a fresh run; dispatch may aggregate platform runs
    # started earlier for the same immutable candidate SHA.
    earliest = (
        parse_time(own_run["created_at"]) - dt.timedelta(minutes=2)
        if args.event == "pull_request"
        else dt.datetime.min.replace(tzinfo=UTC)
    )
    deadline = time.monotonic() + args.timeout_seconds
    while True:
        report = collect(repo, args.sha, args.event, earliest, manifest["workflows"])
        gaps = test_execution_gaps(report, manifest["platform_tests"])
        payload = {
            "schema_version": 2,
            "sha": args.sha,
            "event": args.event,
            "manifest": manifest["workflows"],
            "platform_tests": manifest["platform_tests"],
            "test_execution_gaps": gaps,
            "cells": report,
        }
        args.report.write_text(json.dumps(payload, indent=2) + "\n")
        results = {name: cell["result"] for name, cell in report.items()}
        print(json.dumps(results, sort_keys=True), flush=True)
        if all(value == "passed" for value in results.values()) and not gaps:
            return 0
        terminal = set(results.values()) - {"passed", "pending", "missing-run"}
        if (
            terminal
            or (all(value == "passed" for value in results.values()) and gaps)
            or time.monotonic() >= deadline
        ):
            return 1
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
