"""Pin the publishing contract: ./push delegates to the prooft runner (GATE-014)."""

import ast
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUSH = ROOT / "push"
POLICY = ROOT / "ci-attestations.yml"


@pytest.fixture
def delegate(monkeypatch):
    """Run ./push in-process with os.execvp captured instead of replacing pytest."""
    import os
    import sys

    calls = []
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setattr(os, "execvp", lambda file, argv: calls.append((file, argv)))

    def run(*args: str):
        monkeypatch.setattr(sys, "argv", ["push", *args])
        runpy.run_path(str(PUSH), run_name="__main__")
        return calls

    return run


@pytest.mark.parametrize("args", [(), ("--test",), ("--no-pr", "--remote", "mirror")])
def test_push_execs_the_pinned_prooft_runner(delegate, args):
    from ci.prooft_verify import PROOFT_SOURCE

    assert delegate(*args) == [
        ("uvx", ["uvx", "--from", PROOFT_SOURCE, "prooft", "--push", *args])
    ]


def test_push_is_a_thin_python_delegate():
    source = PUSH.read_text()
    assert source.startswith("#!/usr/bin/env python\n")
    mode = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "-s", "push"], text=True
    )
    assert mode.startswith("100755 ")
    tree = ast.parse(source)
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # The runner owns lint, tests, the push and the PR; ./push never does them.
    assert not strings & {"git", "gh", "run", "soldr", "apply", "--no-lint"}
    # One pin: the source comes from ci/prooft_verify.py, never a second literal.
    assert "prooft@" not in source
    code = [
        line
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(code) <= 25


def test_policy_runs_lint_before_tests():
    lanes: dict[str, dict[str, str]] = {}
    section = lane = None
    for line in POLICY.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            section = line.rstrip(":")
        elif section == "lanes" and not line.startswith("    "):
            lane = line.strip().rstrip(":")
            lanes[lane] = {}
        elif section == "lanes" and lane:
            key, _, value = line.strip().partition(":")
            lanes[lane][key] = value.strip()
    assert lanes["lint"]["stage"] == "lint"
    assert lanes["lint"]["run"] == "[./lint]"
    assert lanes["test"]["stage"] == "test"
    assert lanes["test"]["run"] == (
        "[uv, run, --no-project, python, ci/attest_test.py]"
    )
    assert list(lanes) == ["lint", "test"]


def test_verifier_shares_pin_and_uses_base_policy(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    import ci.prooft_verify as verifier

    calls = []

    def run(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(verifier, "PROOFT_SOURCE", "test-pin")
    monkeypatch.setattr(verifier.subprocess, "run", run)
    assert verifier.main("base", "head", "--literal title") == 0
    assert calls[0] == ["git", "fetch", "--no-tags", "origin", "head"]
    assert calls[1] == verifier.prooft(
        "plan",
        "--base",
        "base",
        "--head",
        "head",
        "--title=--literal title",
        "--github-output",
    )
    assert calls[1][2] == "test-pin"


@pytest.mark.parametrize(
    ("cpus", "load", "workers"), [(16, 0, 16), (16, 80, 3), (16, 170, 2), (1, 2, 1)]
)
def test_concurrency_tracks_host_load(monkeypatch, cpus, load, workers):
    monkeypatch.syspath_prepend(str(ROOT))
    from ci.attest_test import load_aware_workers

    assert load_aware_workers(cpus, load) == workers
