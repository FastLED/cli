"""Tests for ci/sign_windows_release.py and its release-workflow wiring."""

import importlib.util
import struct
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SPEC = importlib.util.spec_from_file_location(
    "sign_windows_release", ROOT / "ci" / "sign_windows_release.py"
)
assert SPEC and SPEC.loader
sign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sign
SPEC.loader.exec_module(sign)

PUBLISHER = "Zachary Vorhies"
SIGNED = b"\x30\x82" + PUBLISHER.encode() + sign.RFC3161_TIMESTAMP_OID


def _pe(cert: bytes | None = None) -> bytes:
    """Minimal PE32+ image, optionally with a WIN_CERTIFICATE table."""
    pe_offset = 0x40
    optional = pe_offset + 24
    entry = optional + 112 + sign.SECURITY_DIRECTORY_INDEX * 8
    image = bytearray(entry + 8)
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", image, optional, 0x20B)
    if cert is not None:
        blob = struct.pack(
            "<IHH",
            8 + len(cert),
            sign.WIN_CERT_REVISION_2,
            sign.WIN_CERT_TYPE_PKCS_SIGNED_DATA,
        )
        struct.pack_into("<II", image, entry, len(image), len(blob) + len(cert))
        image += blob + cert
    return bytes(image)


def test_requires_timestamped_signature_by_publisher():
    assert sign.signature_problem(_pe(SIGNED), PUBLISHER) is None
    cases = [
        (_pe(), "no Authenticode signature"),
        (_pe(b"CN=Someone Else" + sign.RFC3161_TIMESTAMP_OID), "publisher"),
        (_pe(b"\x30\x82" + PUBLISHER.encode()), "RFC 3161"),
    ]
    for image, problem in cases:
        assert problem in (sign.signature_problem(image, PUBLISHER) or "")


def test_verify_files_and_wheels_fail_closed(tmp_path):
    exe = tmp_path / "fastled.exe"
    exe.write_bytes(_pe())
    with pytest.raises(sign.SigningError, match="fastled.exe"):
        sign.verify_files([exe], PUBLISHER)
    exe.write_bytes(_pe(SIGNED))
    sign.verify_files([exe], PUBLISHER)
    with pytest.raises(sign.SigningError, match="no Windows PE"):
        sign.verify_files([tmp_path / "missing.exe"], PUBLISHER)

    wheel = tmp_path / "fastled-1.0.0-py3-none-win_amd64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fastled/bin/fastled.exe", _pe())
    with pytest.raises(sign.SigningError, match="fastled/bin/fastled.exe"):
        sign.verify_wheels([wheel], PUBLISHER)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fastled/bin/fastled.exe", _pe(SIGNED))
    sign.verify_wheels([wheel], PUBLISHER)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fastled/__init__.py", "")
    with pytest.raises(sign.SigningError, match="no Windows PE members"):
        sign.verify_wheels([wheel], PUBLISHER)


def test_jsign_command_pins_artifact_signing_and_keeps_token_off_argv():
    config = sign.SigningConfig(
        sign.endpoint_host("https://eus.codesigning.azure.net/"),
        "acct",
        "prof",
        PUBLISHER,
    )
    command = sign.jsign_command(Path("jsign.jar"), config, [Path("fastled.exe")])
    flags = dict(zip(command[3::2], command[4::2]))
    assert flags["--storetype"] == "TRUSTEDSIGNING"
    assert flags["--keystore"] == "eus.codesigning.azure.net"
    assert flags["--storepass"] == f"env:{sign.TOKEN_ENV}"
    assert flags["--alias"] == "acct/prof"
    assert flags["--alg"] == "SHA-256"
    assert flags["--tsaurl"] == "http://timestamp.acs.microsoft.com"
    assert flags["--tsmode"] == "RFC3161"
    assert sign.JSIGN_VERSION == "7.6"
    assert sign.JSIGN_SHA256 == (
        "6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa5"
    )
    assert sign.publisher_common_name("CN=Zachary Vorhies, O=Z, C=US") == PUBLISHER
    with pytest.raises(sign.SigningError):
        sign.endpoint_host("https://example.com/")


def test_config_requires_every_secret():
    env = {
        "AZURE_SIGNING_ENDPOINT": "https://eus.codesigning.azure.net",
        "AZURE_SIGNING_ACCOUNT": "a",
        "AZURE_SIGNING_PROFILE": "p",
    }
    with pytest.raises(sign.SigningError, match="AZURE_SIGN_PUBLISHER"):
        sign.config_from_env(env)


def test_build_signs_before_wheel_and_upload_and_verifies_shipped_bytes():
    build = (WORKFLOWS / "_build.yml").read_text()
    order = [
        build.index("- name: Build fastled CLI binary"),
        build.index("- name: Azure login for Authenticode signing"),
        build.index("- name: Authenticode-sign fastled.exe"),
        build.index("- name: Build wheel"),
        build.index("- name: Verify shipped Windows binaries are signed"),
        build.index("- name: Upload wheel"),
        build.index("- name: Upload binary"),
    ]
    assert order == sorted(order)
    assert "azure/login@a457da9ea143d694b1b9c7c869ebb04ebe844ef5" in build
    assert "client-secret" not in build
    assert (
        build.count("if: inputs.sign-windows && github.event_name != 'pull_request'")
        == 3
    )
    assert "environment: ${{ inputs.sign-windows && 'release' || '' }}" in build
    assert "--wheel" in build and "smoke --file" in build


def test_release_signs_only_windows_lanes_with_new_secret_names():
    release = (WORKFLOWS / "auto-release.yml").read_text()
    jobs = [j.split("\n\n  ", 1)[0] for j in release.split("\n  build-")]
    for job in jobs[1:]:
        windows = job.startswith("windows-")
        assert ("sign-windows: true" in job) == windows
        assert ("id-token: write" in job) == windows
    for legacy in ("AZURE_SIGN_ENDPOINT", "AZURE_SIGN_ACCOUNT", "AZURE_SIGN_PROFILE"):
        assert legacy not in release
        assert legacy not in (WORKFLOWS / "_build.yml").read_text()
    for name in ("windows-x86-build.yml", "windows-arm-build.yml"):
        assert "sign-windows" not in (WORKFLOWS / name).read_text()
