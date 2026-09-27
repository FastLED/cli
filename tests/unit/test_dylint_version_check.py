from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from ci.lint_python import dylint_version_checker


def _mock_version_command(
    monkeypatch: pytest.MonkeyPatch, output: str, exit_code: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            exit_code,
            stdout=output,
            stderr="broken" if exit_code else "",
        )

    monkeypatch.setattr(dylint_version_checker.subprocess, "run", run)
    return calls


def test_accepts_exact_cargo_dylint_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _mock_version_command(monkeypatch, "cargo-dylint 6.0.3\n")

    result = dylint_version_checker.check_version("cargo-dylint", "6.0.3")

    assert result == 0
    assert calls == [["cargo-dylint", "dylint", "--version"]]
    assert capsys.readouterr().out.strip() == "cargo-dylint 6.0.3"


def test_rejects_stale_cargo_dylint_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_version_command(monkeypatch, "cargo-dylint 6.0.2\n")

    result = dylint_version_checker.check_version("cargo-dylint", "6.0.3")

    assert result != 0
    error = capsys.readouterr().err
    assert "expected cargo-dylint 6.0.3" in error
    assert "found cargo-dylint 6.0.2" in error


def test_rejects_unusable_cargo_dylint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_version_command(monkeypatch, "", exit_code=1)

    result = dylint_version_checker.check_version("cargo-dylint", "6.0.3")

    assert result != 0
    assert "could not query" in capsys.readouterr().err


def test_lint_entrypoints_enforce_the_exact_pin() -> None:
    lint_script = (REPO_ROOT / "lint").read_text(encoding="utf-8")
    lint_workflow = (REPO_ROOT / ".github" / "workflows" / "_lint.yml").read_text(
        encoding="utf-8"
    )

    assert "soldr dylint prepare" in lint_script
    assert "soldr dylint --all -- --workspace --all-targets" in lint_script
    assert 'cargo-dylint-version: "6.0.3"' in lint_workflow
    assert "dylint-toolchain: nightly-2026-05-28" in lint_workflow
    assert "soldr dylint prepare" in lint_workflow
    assert "soldr cargo install cargo-dylint" not in lint_workflow
    linker_config = (REPO_ROOT / "dylints/ban_std_pathbuf/.cargo/config.toml").read_text(encoding="utf-8")
    assert 'rustflags = ["-C", "linker=dylint-link"]' in linker_config
