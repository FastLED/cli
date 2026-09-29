"""Guard the event-to-CI-tier wiring across every platform workflow."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SPEC = importlib.util.spec_from_file_location(
    "check_full_coverage", ROOT / "ci" / "check_full_coverage.py"
)
assert SPEC and SPEC.loader
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_full_coverage", ROOT / "ci" / "verify_full_coverage.py"
)
assert VERIFY_SPEC and VERIFY_SPEC.loader
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)
ARTIFACT_SPEC = importlib.util.spec_from_file_location(
    "release_artifact_lint", ROOT / "ci" / "release_artifact_lint.py"
)
assert ARTIFACT_SPEC and ARTIFACT_SPEC.loader
artifacts = importlib.util.module_from_spec(ARTIFACT_SPEC)
ARTIFACT_SPEC.loader.exec_module(artifacts)


def test_platform_workflows_have_normalized_tiers():
    seen = set()
    for path in WORKFLOWS.glob("*.yml"):
        if not path.name.startswith(("linux-", "windows-", "macos-")):
            continue
        seen.add(path.name)
        workflow = path.read_text()
        assert "  pull_request:" in workflow, path.name
        assert (
            "types: [opened, synchronize, reopened, labeled, unlabeled]" in workflow
        ), path.name
        if path.name.endswith("-lint.yml"):
            # Native Dylint runs on every ordinary PR for all lint platforms
            # (#274), so lint workflows carry no tier gate at all.
            assert "    if: " not in workflow, path.name
            assert "'ci-full'" not in workflow, path.name
            assert "'ci-test'" not in workflow, path.name
        else:
            assert (
                "    if: github.event_name == 'workflow_dispatch'" in workflow
            ), path.name
            assert "'ci-full'" in workflow, path.name
            if path.name in {
                "linux-x86-unit-test.yml",
                "linux-x86-integration-test.yml",
            }:
                assert "'ci-test'" in workflow, path.name
            else:
                assert "'ci-test'" not in workflow, path.name
    assert len(seen) == 29


def test_vscode_matrix_only_runs_for_full_pr_or_tag_release():
    workflow = (WORKFLOWS / "vscode-extension.yml").read_text()
    assert "release_build:" in workflow
    assert workflow.count("inputs.release_build") == 2
    assert workflow.count("'ci-full'") == 2
    release = (WORKFLOWS / "vscode-extension-release.yml").read_text()
    assert "release_build: true" in release


def test_vscode_release_cannot_create_tag_before_full_coverage_and_preflight():
    release = (WORKFLOWS / "vscode-extension-release.yml").read_text()
    assert "  push:\n    tags:" not in release
    assert "  workflow_dispatch:" in release
    assert "candidate_sha:" in release
    assert "dry_run:" in release
    assert "verify_full_coverage.py" in release
    assert "vscode_release_artifact_lint.py" in release
    assert "  create-tag:" in release
    tag = release.split("  create-tag:\n", 1)[1].split("  publish:\n", 1)[0]
    assert "needs.full-coverage.result == 'success'" in tag
    assert "needs.artifact-preflight.result == 'success'" in tag
    assert "inputs.dry_run != true" in tag
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in tag
    assert "git push origin" in tag
    publish = release.split("  publish:\n", 1)[1]
    assert "needs.create-tag.result == 'success'" in publish


def test_vscode_release_artifact_lint_rejects_missing_corrupt_and_wrong_version(
    tmp_path,
):
    spec = importlib.util.spec_from_file_location(
        "vscode_release_artifact_lint", ROOT / "ci" / "vscode_release_artifact_lint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    targets = {
        "win32-x64",
        "win32-arm64",
        "linux-x64",
        "linux-arm64",
        "darwin-x64",
        "darwin-arm64",
        "universal",
    }
    for target in targets:
        package = tmp_path / f"fastled-wasm-1.0.1-{target}.vsix"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "extension/package.json",
                json.dumps({"name": "fastled-wasm", "version": "1.0.1"}),
            )
            if target != "universal":
                archive.writestr(
                    "extension/resources/clangd/manifest.json", b"native manifest"
                )
        if target != "universal":
            (tmp_path / f"{target}.manifest.json").write_bytes(b"native manifest")
            (tmp_path / f"{target}.sha256").write_text(
                f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n"
            )
            (tmp_path / f"{target}.size").write_text(f"{package.stat().st_size}\n")
    assert len(module.check(tmp_path, "1.0.1")) == 7
    with pytest.raises(ValueError):
        module.check(tmp_path, "1.0.2")
    (tmp_path / "fastled-wasm-1.0.1-universal.vsix").write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        module.check(tmp_path, "1.0.1")


def test_vscode_release_artifact_lint_accepts_native_sidecars_only(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "vscode_release_artifact_lint", ROOT / "ci" / "vscode_release_artifact_lint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    version = "1.0.1"
    for target in module.TARGETS:
        package = tmp_path / f"fastled-wasm-{version}-{target}.vsix"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "extension/package.json",
                json.dumps({"name": "fastled-wasm", "version": version}),
            )
            if target != "universal":
                archive.writestr(
                    "extension/resources/clangd/manifest.json", b"native manifest"
                )
        if target != "universal":
            (tmp_path / f"{target}.manifest.json").write_bytes(b"native manifest")
            (tmp_path / f"{target}.sha256").write_text(
                f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n"
            )
            (tmp_path / f"{target}.size").write_text(f"{package.stat().st_size}\n")
    assert len(module.check(tmp_path, version)) == 7
    (tmp_path / "unexpected.txt").write_text("extra")
    with pytest.raises(ValueError, match="unexpected"):
        module.check(tmp_path, version)
    (tmp_path / "unexpected.txt").unlink()
    (tmp_path / "linux-x64.sha256").write_text("incorrect digest\n")
    with pytest.raises(ValueError, match="SHA-256 sidecar mismatch"):
        module.check(tmp_path, version)


def test_release_does_not_wait_for_removed_routine_build_artifacts():
    workflow = (WORKFLOWS / "auto-release.yml").read_text()
    assert "collect-artifacts:" not in workflow
    assert "build-linux-x86:" in workflow
    assert "build-macos-arm:" in workflow
    assert "needs.build-linux-x86.result == 'success'" in workflow
    assert "needs.build-macos-arm.result == 'success'" in workflow
    build = (WORKFLOWS / "_build.yml").read_text()
    assert build.count("if-no-files-found: error") >= 2
    assert "name: smoke-wheel-screenshot-" in build
    assert "name: smoke-wheel-log-" in build
    assert "name: wheel-smoke-" not in build


def test_platform_jobs_checkout_exact_requested_sha():
    for path in WORKFLOWS.glob("*.yml"):
        if not path.name.startswith(("linux-", "windows-", "macos-")):
            continue
        workflow = path.read_text()
        assert "candidate_sha:" in workflow, path.name
        assert "required: true" in workflow, path.name
        assert "github.event.pull_request.head.sha" in workflow, path.name
        if "uses: ./.github/workflows/_" in workflow:
            assert "source-sha:" in workflow, path.name
        else:
            assert "ref: ${{" in workflow, path.name
            assert "git rev-parse HEAD" in workflow, path.name
    for path in WORKFLOWS.glob("_*.yml"):
        workflow = path.read_text()
        assert "source-sha:" in workflow, path.name
        assert "ref: ${{ inputs.source-sha || github.sha }}" in workflow, path.name
        assert "git rev-parse HEAD" in workflow, path.name


def test_windows_arm_test_failures_capture_soldr_diagnostics():
    diagnostic = ROOT / "ci" / "capture_soldr_failure.ps1"
    assert diagnostic.exists()
    script = diagnostic.read_text()
    for command in ("doctor", "status", "logs paths"):
        assert command in script
    assert "WriteAllBytes" in script
    assert "logs-paths.txt" in script
    assert "RUNNER_TEMP" in script
    assert ".soldr" in script
    assert "cache\\zccache" in script
    assert "private" in script
    assert "-Recurse" not in script
    assert "$ProbeTimeoutMs = 15000" in script
    assert "$MaxLogFiles = 24" in script
    assert "$MaxLogBytes = 65536" in script
    assert "$MaxPrivateDirs = 8" in script
    assert "WaitForExit($ProbeTimeoutMs)" in script
    assert "Seek(-$bytesToRead, 'End')" in script
    for name in ("_unit-test.yml", "_integration-test.yml"):
        workflow = (WORKFLOWS / name).read_text()
        assert "if: failure() && inputs.runs-on == 'windows-11-arm'" in workflow
        assert "continue-on-error: true" in workflow
        assert "./ci/capture_soldr_failure.ps1" in workflow
        assert "soldr-failure-diagnostics/**" in workflow


def test_windows_arm_tests_select_soldr_with_embedded_daemon():
    version_selector = (
        "version: ${{ inputs.runs-on == 'windows-11-arm' && '0.9.21' || '0.7.51' }}"
    )
    for name in ("_unit-test.yml", "_integration-test.yml"):
        workflow = (WORKFLOWS / name).read_text()
        setup = workflow.split("- uses: zackees/setup-soldr@v0.9.62", 1)[1]
        setup = setup.split("      - name: Sync Python deps", 1)[0]
        assert version_selector in setup, name
        assert setup.count("version:") == 1, name

    build = (WORKFLOWS / "_build.yml").read_text()
    setup = build.split("- uses: zackees/setup-soldr@v0.9.62", 1)[1]
    setup = setup.split("      - name: Build fastled CLI binary", 1)[0]
    assert "version:" not in setup


def test_windows_arm_targeted_label_runs_only_its_two_test_workflows():
    selected = {"windows-arm-unit-test.yml", "windows-arm-integration-test.yml"}
    for path in WORKFLOWS.glob("*.yml"):
        workflow = path.read_text()
        if path.name in selected:
            assert "'ci-test:windows-arm'" in workflow, path.name
            assert "'ci-full'" in workflow, path.name
            assert (
                "types: [opened, synchronize, reopened, labeled, unlabeled]" in workflow
            )
        else:
            assert "'ci-test:windows-arm'" not in workflow, path.name


def test_full_coverage_manifest_and_fail_closed_results():
    manifest = json.loads((ROOT / "ci" / "full_coverage.json").read_text())
    actual = sorted(
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if path.name.startswith(("linux-", "windows-", "macos-"))
    )
    assert manifest["schema_version"] == 2
    assert manifest["workflows"] == actual
    workflow = (WORKFLOWS / "full-coverage.yml").read_text()
    assert "ci-full" in workflow
    assert "check_full_coverage.py" in workflow
    sha = "a" * 40
    run = {
        "head_sha": sha,
        "event": "pull_request",
        "created_at": "2026-09-23T00:00:00Z",
        "status": "completed",
        "conclusion": "success",
    }
    earliest = coverage.parse_time("2026-09-22T23:59:00Z")
    assert (
        coverage.cell_result(
            run, [{"conclusion": "success"}], sha, "pull_request", earliest
        )
        == "passed"
    )
    assert (
        coverage.cell_result(run, [], sha, "pull_request", earliest) == "missing-jobs"
    )
    assert (
        coverage.cell_result(
            run, [{"conclusion": "skipped"}], sha, "pull_request", earliest
        )
        == "skipped-or-failed-job"
    )
    assert (
        coverage.cell_result(
            run, [{"conclusion": "success"}], "b" * 40, "pull_request", earliest
        )
        == "wrong-sha-or-event"
    )
    assert (
        coverage.cell_result(
            run, [{"conclusion": "success"}], sha, "workflow_dispatch", earliest
        )
        == "wrong-sha-or-event"
    )


def test_publication_requires_exact_sha_full_coverage():
    release = (WORKFLOWS / "auto-release.yml").read_text()
    vscode = (WORKFLOWS / "vscode-extension-release.yml").read_text()
    assert "  push:\n    branches:\n      - main" not in release
    assert "candidate_sha:" in release
    assert "needs.full-coverage.result == 'success'" in release
    assert "verify_full_coverage.py" in release
    assert "verify_full_coverage.py" in vscode
    assert "needs.full-coverage.result == 'success'" in vscode


def test_release_tag_uses_verified_candidate_checkout():
    release = (WORKFLOWS / "auto-release.yml").read_text()
    tag_job = release.split("  create-tag:\n", 1)[1].split("  publish-pypi:\n", 1)[0]
    assert "ref: ${{ inputs.candidate_sha }}" in tag_job
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in tag_job


def test_release_dry_run_and_artifact_preflight_block_tagging():
    release = (WORKFLOWS / "auto-release.yml").read_text()
    assert "dry_run:" in release
    assert "artifact-preflight:" in release
    assert (
        'python ci/release_artifact_lint.py --wheels dist --binaries binaries --version "$VERSION"'
        in release
    )
    tag_job = release.split("  create-tag:\n", 1)[1].split("  publish-pypi:\n", 1)[0]
    assert "needs.artifact-preflight.result == 'success'" in tag_job
    assert "inputs.dry_run != true" in tag_job
    for job in ("publish-pypi", "create-release"):
        section = release.split(f"  {job}:\n", 1)[1].split("    steps:\n", 1)[0]
        assert "inputs.dry_run != true" in section


def test_release_artifact_lint_checks_exact_six_wheels_and_binaries(tmp_path):
    wheels = tmp_path / "dist"
    binaries = tmp_path / "binaries"
    wheels.mkdir()
    binaries.mkdir()
    for target, platform in artifacts.TARGET_PLATFORMS.items():
        with zipfile.ZipFile(
            wheels / f"fastled-2.0.23-py3-none-{platform}.whl", "w"
        ) as archive:
            archive.writestr("fastled-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
            archive.writestr(
                (
                    "fastled/bin/fastled.exe"
                    if "windows" in target
                    else "fastled/bin/fastled"
                ),
                "binary",
            )
        destination = binaries / f"binary-runner-{target}"
        destination.mkdir()
        (
            destination / ("fastled.exe" if "windows" in target else "fastled")
        ).write_bytes(b"binary")
    assert len(artifacts.check(wheels, binaries, "2.0.23")["wheels"]) == 6
    with pytest.raises(ValueError, match="versioned platform wheels"):
        artifacts.check(wheels, binaries, "2.0.24")
    (wheels / "stray.log").write_text("not a wheel")
    with pytest.raises(ValueError, match="versioned platform wheels"):
        artifacts.check(wheels, binaries, "2.0.23")


def test_cross_target_wheel_build_preserves_target_for_setup_py():
    build = (WORKFLOWS / "_build.yml").read_text()
    wheel_step = build.split("      - name: Build wheel\n", 1)[1].split(
        "      - name: Smoke test", 1
    )[0]
    assert "FASTLED_RUST_TARGET: ${{ inputs.rust-target }}" in wheel_step


def test_release_report_validation_rejects_missing_or_wrong_sha_cells():
    sha = "a" * 40
    platforms = (
        "linux-x86",
        "linux-arm",
        "windows-x86",
        "windows-arm",
        "macos-x86",
        "macos-arm",
    )
    names = [f"{platform}-unit-test.yml" for platform in platforms]
    tests = {
        platform: [{"workflow": f"{platform}-unit-test.yml", "step": "Unit Tests"}]
        for platform in platforms
    }
    manifest = {"workflows": names, "platform_tests": tests}
    report = {
        "schema_version": 2,
        "sha": sha,
        "event": "workflow_dispatch",
        "manifest": names,
        "platform_tests": tests,
        "test_execution_gaps": [],
        "cells": {
            name: {
                "result": "passed",
                "run_id": index,
                "jobs": [
                    {
                        "conclusion": "success",
                        "steps": [{"name": "Unit Tests", "conclusion": "success"}],
                    }
                ],
            }
            for index, name in enumerate(names, 1)
        },
    }
    assert verify.validate_report(report, manifest, sha)
    assert not verify.validate_report(report, manifest, "b" * 40)
    report["cells"]["windows-arm-unit-test.yml"]["jobs"] = [
        {"conclusion": "success", "steps": []}
    ]
    assert coverage.test_execution_gaps(report["cells"], tests) == [
        "windows-arm: windows-arm-unit-test.yml / Unit Tests"
    ]
    assert not verify.validate_report(report, manifest, sha)
    report["cells"]["windows-arm-unit-test.yml"]["jobs"] = [
        {
            "conclusion": "success",
            "steps": [{"name": "Unit Tests", "conclusion": "success"}],
        }
    ]
    report["cells"]["macos-arm-unit-test.yml"]["jobs"][0]["steps"][0][
        "conclusion"
    ] = "skipped"
    assert not verify.validate_report(report, manifest, sha)
    report["cells"]["macos-arm-unit-test.yml"]["jobs"][0]["steps"][0][
        "conclusion"
    ] = "success"
    del report["cells"]["macos-arm-unit-test.yml"]
    assert not verify.validate_report(report, manifest, sha)


def test_every_release_platform_has_executed_test_step():
    manifest = json.loads((ROOT / "ci" / "full_coverage.json").read_text())
    required = {
        "linux-x86",
        "linux-arm",
        "windows-x86",
        "windows-arm",
        "macos-x86",
        "macos-arm",
    }
    assert set(manifest["platform_tests"]) == required
    for platform, checks in manifest["platform_tests"].items():
        assert checks, platform
        for check in checks:
            assert check["workflow"] in manifest["workflows"]
            workflow = (WORKFLOWS / check["workflow"]).read_text()
            if "_unit-test.yml" in workflow:
                step_source = (WORKFLOWS / "_unit-test.yml").read_text()
            elif "_integration-test.yml" in workflow:
                step_source = (WORKFLOWS / "_integration-test.yml").read_text()
            else:
                step_source = workflow
            assert f"- name: {check['step']}\n" in step_source, check
