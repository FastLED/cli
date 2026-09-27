from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")

_STUB_SCRIPT = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$LINT_STUB_LOG"
if [ "$(basename "$0") $1 $2" = "soldr dylint prepare" ]; then
  exit 1
fi
exit 0
"""


def _path_entry_has_dylint_or_rustup(entry: str) -> bool:
    directory = Path(entry)
    if not directory.is_dir():
        return False
    return (directory / "cargo-dylint").exists() or (directory / "rustup").exists()


def _run_lint(
    tmp_path: Path, *, ci: bool, args: tuple[str, ...] = ()
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    for name in ("uv", "soldr"):
        stub = bin_dir / name
        stub.write_text(_STUB_SCRIPT, encoding="utf-8")
        stub.chmod(0o755)

    original_entries = os.environ.get("PATH", "").split(os.pathsep)
    filtered_entries = [
        entry
        for entry in original_entries
        if not _path_entry_has_dylint_or_rustup(entry)
    ]
    path = os.pathsep.join([str(bin_dir), *filtered_entries])

    stub_log = tmp_path / "stub.log"

    env = dict(os.environ)
    env["PATH"] = path
    env["LINT_STUB_LOG"] = str(stub_log)
    if ci:
        env["CI"] = "true"
    else:
        env.pop("CI", None)

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "lint"), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    if stub_log.exists():
        log_lines = stub_log.read_text(encoding="utf-8").splitlines()
    else:
        log_lines = []

    return result, log_lines


def _log_has_match(log_lines: list[str], pattern: str) -> bool:
    return any(re.search(pattern, line) for line in log_lines)


def test_python_stages_run_and_cover_ci_without_dylint(tmp_path: Path) -> None:
    result, log_lines = _run_lint(tmp_path, ci=False)

    assert result.returncode != 0
    assert "dylint prerequisites are missing" in result.stderr

    # Every Python lint stage must cover src, tests, and ci -- the stub uv
    # collapses "uv run <tool> ..." into a single logged argv line, so match
    # on whatever prefix the stub actually recorded and keep the assertion
    # anchored on the "src tests ci" tail so a dropped "ci" fails the test.
    assert _log_has_match(log_lines, r"^uv (run )?ruff check --fix src tests ci$")
    assert _log_has_match(log_lines, r"^uv (run )?black src tests ci$")
    assert _log_has_match(log_lines, r"^uv (run )?isort --profile black src tests ci$")
    assert _log_has_match(log_lines, r"^uv (run )?pyright src tests ci$")
    assert _log_has_match(
        log_lines,
        r"^uv run python ci/lint_python/keyboard_interrupt_checker\.py src tests ci$",
    )

    # The Rust stages still ran through soldr.
    assert any(line == "soldr cargo fmt --all --check" for line in log_lines)
    assert any(
        line.startswith("soldr cargo clippy --workspace --all-targets")
        for line in log_lines
    )

    # Preparation fails closed, so the Dylint pass cannot run.
    assert "soldr dylint prepare" in log_lines
    assert "soldr dylint --all -- --workspace --all-targets" not in log_lines


def test_missing_dylint_is_a_hard_failure_in_ci(tmp_path: Path) -> None:
    result, log_lines = _run_lint(tmp_path, ci=True)

    assert result.returncode != 0
    assert "Error: dylint prerequisites are missing:" in result.stderr
    # The failure is reported only after the Python stages have run, so a CI
    # host missing cargo-dylint still reports every Python finding.
    assert _log_has_match(log_lines, r"^uv (run )?pyright src tests ci$")


def test_explicit_skip_keeps_other_lints_but_does_not_run_dylint(tmp_path: Path) -> None:
    result, log_lines = _run_lint(tmp_path, ci=True, args=("--skip-dylint",))
    assert result.returncode == 0
    assert any(line.startswith("soldr cargo clippy") for line in log_lines)
    assert not any("dylint" in line for line in log_lines)


def test_python_stages_precede_the_rust_stages(tmp_path: Path) -> None:
    _result, log_lines = _run_lint(tmp_path, ci=False)

    pyright_index = next(
        index
        for index, line in enumerate(log_lines)
        if re.search(r"^uv (run )?pyright src tests ci$", line)
    )
    rust_index = next(
        index for index, line in enumerate(log_lines) if line.startswith("soldr cargo")
    )

    assert pyright_index < rust_index


@pytest.mark.parametrize(
    "workflow",
    [
        "linux-arm-lint.yml",
        "macos-arm-lint.yml",
        "macos-x86-lint.yml",
        "windows-x86-lint.yml",
    ],
)
def test_native_lint_without_dylint_runs_on_unlabelled_pr(workflow: str) -> None:
    source = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(
        encoding="utf-8"
    )
    assert "pull_request:" in source
    assert "uses: ./.github/workflows/_lint.yml" in source
    assert "run-dylint: true" not in source
    assert "contains(github.event.pull_request.labels.*.name, 'ci-full')" not in source


def test_only_linux_x86_runs_unified_dylint() -> None:
    source = (REPO_ROOT / ".github" / "workflows" / "linux-x86-lint.yml").read_text(
        encoding="utf-8"
    )
    assert "run-dylint: true" in source
