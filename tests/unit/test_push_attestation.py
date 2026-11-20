"""Exercise the publishing contract without a real remote or Rust build."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "ci/prooft_verify.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "FastLED test")
    (tmp_path / "push").write_bytes((ROOT / "push").read_bytes())
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/__init__.py").touch()
    (tmp_path / "ci/prooft_verify.py").write_bytes(VERIFY.read_bytes())
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "test")
    return tmp_path


def run_push(repo: Path, *args: str):
    return subprocess.run(
        ["uv", "run", "--no-project", "python", "push", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("state", ["main", "detached", "staged", "unstaged"])
def test_refuses_unprovable_checkout(checkout, state):
    if state == "detached":
        git(checkout, "checkout", "--detach")
    if state in {"staged", "unstaged"}:
        git(checkout, "checkout", "-b", "feature")
        with (checkout / "push").open("a") as stream:
            stream.write("\n")
        if state == "staged":
            git(checkout, "add", "push")
    assert run_push(checkout).returncode == 3


def test_no_proof_bypass(checkout):
    assert run_push(checkout, "--no-proof").returncode == 2


@pytest.fixture
def publisher(checkout, monkeypatch):
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader

    monkeypatch.syspath_prepend(str(ROOT))
    spec = spec_from_loader(
        "publisher", SourceFileLoader("publisher", str(checkout / "push"))
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", checkout)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.platform, "libc_ver", lambda: ("glibc", "2.39"))
    git(checkout, "checkout", "-b", "feature")
    (checkout / "untracked.txt").write_text("untracked changes are allowed")
    return module


@pytest.mark.parametrize(
    ("failure", "flags", "expected", "commands"),
    [
        (None, ["--no-pr"], 0, ["lint", "test", "verify", "push"]),
        (None, ["--prove-only"], 0, ["lint", "test", "verify"]),
        ("lint", [], 1, ["lint"]),
        ("test", [], 1, ["lint", "test"]),
        ("verify", [], 1, ["lint", "test", "verify"]),
    ],
)
def test_attest_and_verify_before_push(
    publisher, monkeypatch, failure, flags, expected, commands
):
    import sys

    calls = []
    states = {gate: "missing" for lane in publisher.LANES for gate in lane.gates}

    head = git(publisher.ROOT, "rev-parse", "HEAD")

    def verify(rev="HEAD"):
        if calls:
            assert rev == head
            calls.append("verify")
            return (1 if failure == "verify" else 0), states
        return 0, states

    def step(argv):
        if argv[0] == "git":
            assert argv == [
                "git",
                "push",
                "--force-with-lease",
                "-u",
                "mirror",
                f"{head}:refs/heads/feature",
            ]
            calls.append("push")
            return 0
        assert argv[0:2] == ["uvx", "--from"]
        assert argv[6] == "run"
        lane = next(lane for lane in publisher.LANES if lane.name == argv[7])
        calls.append(lane.name)
        assert argv[8:] == ["--", *lane.command]
        if failure == lane.name:
            return 1
        states.update(dict.fromkeys(lane.gates, "valid"))
        return 0

    monkeypatch.setattr(sys, "argv", ["push", "--remote", "mirror", *flags])
    actual_git = publisher.git

    def git_command(*args):
        if args[0] == "branch":
            assert args == ("branch", "--set-upstream-to", "mirror/feature", "feature")
            return ""
        return actual_git(*args)

    monkeypatch.setattr(publisher, "git", git_command)
    monkeypatch.setattr(publisher, "proof_states", verify)
    monkeypatch.setattr(publisher, "step", step)
    assert publisher.main() == expected
    assert calls == commands


def test_valid_lanes_are_reused(publisher, monkeypatch):
    states = {gate: "valid" for lane in publisher.LANES for gate in lane.gates}
    monkeypatch.setattr(publisher, "proof_states", lambda _rev="HEAD": (0, states))
    monkeypatch.setattr(publisher, "step", lambda *_: pytest.fail("ran a proven lane"))
    publisher.attest()


def test_verifier_shares_pin_and_uses_base_policy(publisher, monkeypatch):
    import ci.prooft_verify as verifier

    calls = []

    def run(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(verifier, "PROOFT_SOURCE", "test-pin")
    monkeypatch.setattr(verifier.subprocess, "run", run)
    assert verifier.main("base", "head", "--literal title") == 0
    assert calls[0] == ["git", "fetch", "--no-tags", "origin", "head"]
    assert calls[1] == publisher.prooft(
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


def test_changed_head_refuses_to_publish(publisher, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["push"])
    head = git(publisher.ROOT, "rev-parse", "HEAD")

    def attest():
        git(publisher.ROOT, "commit", "--allow-empty", "-m", "concurrent commit")
        return head

    monkeypatch.setattr(publisher, "attest", attest)
    monkeypatch.setattr(
        publisher, "step", lambda *_: pytest.fail("published another HEAD")
    )
    assert publisher.main() == 1


@pytest.mark.parametrize("same_repository", [True, False])
def test_open_pr_matches_pushed_repository(publisher, monkeypatch, same_repository):
    url = "https://github.com/FastLED/cli.git"
    branch = "feature"
    git(publisher.ROOT, "remote", "add", "mirror", url)
    calls = []

    def gh_json(*args):
        if args[:2] == ("repo", "view"):
            assert args[2] == url
            return {"nameWithOwner": "FastLED/cli", "url": url}
        assert args[:6] == ("pr", "list", "--repo", url, "--head", branch)
        owner = "FastLED" if same_repository else "another-fork"
        return [
            {"headRepository": {"nameWithOwner": f"{owner}/cli"}, "headRefName": branch}
        ]

    def create(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(publisher, "gh_json", gh_json)
    monkeypatch.setattr(publisher, "step", create)
    assert publisher.open_pr("mirror", branch, ["ci-test"]) == 0
    assert calls == (
        []
        if same_repository
        else [
            [
                "gh",
                "pr",
                "create",
                "--repo",
                url,
                "--fill",
                "--head",
                branch,
                "--label",
                "ci-test",
            ]
        ]
    )
