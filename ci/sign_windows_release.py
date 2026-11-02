#!/usr/bin/env python3
"""Authenticode-sign FastLED CLI Windows release binaries via Azure Artifact Signing.

Adapted from FastLED/fbuild ci/sign_windows_release.py. The signer is jsign
(https://github.com/ebourg/jsign), a pure-Java Authenticode implementation with
a ``TRUSTEDSIGNING`` keystore. The jar is pinned by version and SHA-256 and
refused on mismatch.

The workflow runs ``azure/login`` (OIDC, no client secret) first; this script
asks the Azure CLI for a short-lived code-signing token, masks it, and hands it
to jsign through the environment, never on argv.

``_build.yml`` calls ``sign`` on ``target/<triple>/release/fastled.exe`` right
after ``cargo build`` and before the binary is bundled into the wheel or
uploaded, so the release zip, the PyPI wheel and the ``binary-*`` artifact all
carry signed bytes. ``sign`` and ``verify`` check every PE structurally (a
PKCS#7 ``WIN_CERTIFICATE`` naming the publisher with an RFC 3161 timestamp);
``verify --wheel`` checks every PE inside built wheels; ``smoke`` requires
Windows' ``Get-AuthenticodeSignature`` to report ``Valid``.

Usage (CI):
    python ci/sign_windows_release.py sign --file target/<triple>/release/fastled.exe
    python ci/sign_windows_release.py verify --wheel dist/fastled-*.whl
    python ci/sign_windows_release.py smoke --file src/fastled/bin/fastled.exe
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

JSIGN_VERSION = "7.6"
JSIGN_URL = f"https://github.com/ebourg/jsign/releases/download/{JSIGN_VERSION}/jsign-{JSIGN_VERSION}.jar"
JSIGN_SHA256 = "6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa5"
DEFAULT_JSIGN_DIR = Path("target/jsign")

TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"
CODESIGNING_RESOURCE = "https://codesigning.azure.net"
TOKEN_ENV = "FASTLED_AZURE_SIGNING_TOKEN"

PE_SUFFIXES = (".exe", ".dll", ".pyd")

# WIN_CERTIFICATE: wRevision 0x0200, wCertificateType WIN_CERT_TYPE_PKCS_SIGNED_DATA.
WIN_CERT_REVISION_2 = 0x0200
WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002
# DER of OID 1.3.6.1.4.1.311.3.3.1 (Microsoft RFC 3161 timestamp countersignature).
RFC3161_TIMESTAMP_OID = bytes.fromhex("060a2b060104018237030301")
SECURITY_DIRECTORY_INDEX = 4


class SigningError(RuntimeError):
    """A signing precondition or post-signing check failed."""


@dataclass(frozen=True)
class SigningConfig:
    """Azure Artifact Signing coordinates, read from the job environment."""

    endpoint_host: str
    account: str
    profile: str
    publisher: str


def endpoint_host(endpoint: str) -> str:
    """``https://eus.codesigning.azure.net/`` -> ``eus.codesigning.azure.net``."""
    host = endpoint.strip().removeprefix("https://").removeprefix("http://")
    host = host.split("/", 1)[0]
    if not host.endswith(".codesigning.azure.net"):
        raise SigningError(f"unexpected Artifact Signing endpoint: {endpoint!r}")
    return host


def _required_env(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SigningError(f"{name} is not set (expected an org Actions secret)")
    return value


def publisher_common_name(publisher: str) -> str:
    """Accept either a bare name or a full subject DN (``CN=..., O=...``)."""
    for part in publisher.split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().upper() == "CN" and value.strip():
            return value.strip().strip('"')
    return publisher.strip()


def config_from_env(env: dict[str, str]) -> SigningConfig:
    return SigningConfig(
        endpoint_host=endpoint_host(_required_env(env, "AZURE_SIGNING_ENDPOINT")),
        account=_required_env(env, "AZURE_SIGNING_ACCOUNT"),
        profile=_required_env(env, "AZURE_SIGNING_PROFILE"),
        publisher=publisher_common_name(_required_env(env, "AZURE_SIGN_PUBLISHER")),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_jsign(dest_dir: Path) -> Path:
    """Download the pinned jsign jar once and verify its SHA-256."""
    jar = dest_dir / f"jsign-{JSIGN_VERSION}.jar"
    if jar.is_file() and sha256_file(jar) == JSIGN_SHA256:
        return jar
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = jar.with_suffix(".partial")
    with (
        urllib.request.urlopen(JSIGN_URL, timeout=120) as response,
        partial.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)
    actual = sha256_file(partial)
    if actual != JSIGN_SHA256:
        partial.unlink()
        raise SigningError(
            f"jsign {JSIGN_VERSION} sha256 mismatch: got {actual}, pinned {JSIGN_SHA256}"
        )
    partial.replace(jar)
    print(f"jsign {JSIGN_VERSION}: sha256 verified ({JSIGN_SHA256})")
    return jar


def _az_executable() -> str:
    # On Windows runners the Azure CLI is az.cmd, which CreateProcess will not
    # resolve from a bare "az".
    found = shutil.which("az")
    if not found:
        raise SigningError("Azure CLI (az) is not on PATH")
    return found


def access_token() -> str:
    """Mint an Artifact Signing token from the ``azure/login`` session."""
    completed = subprocess.run(
        [
            _az_executable(),
            "account",
            "get-access-token",
            "--resource",
            CODESIGNING_RESOURCE,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    token = completed.stdout.strip()
    if not token:
        raise SigningError("az returned an empty code-signing access token")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{token}", flush=True)
    return token


def jsign_command(jar: Path, config: SigningConfig, files: list[Path]) -> list[str]:
    return [
        "java",
        "-jar",
        str(jar),
        "--storetype",
        "TRUSTEDSIGNING",
        "--keystore",
        config.endpoint_host,
        "--storepass",
        f"env:{TOKEN_ENV}",
        "--alias",
        f"{config.account}/{config.profile}",
        "--alg",
        "SHA-256",
        "--tsaurl",
        TIMESTAMP_URL,
        "--tsmode",
        "RFC3161",
        "--replace",
        *[str(path) for path in files],
    ]


def is_pe_name(name: str) -> bool:
    return name.lower().endswith(PE_SUFFIXES)


def pe_files(paths: list[Path]) -> list[Path]:
    """Expand ``paths`` (PE files or directories) to PE files; fail on none."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(
                p for p in path.rglob("*") if p.is_file() and is_pe_name(p.name)
            )
        elif path.is_file() and is_pe_name(path.name):
            found = [path]
        else:
            found = []
        if not found:
            raise SigningError(f"no Windows PE files found at {path}")
        files.extend(found)
    return files


def authenticode_blob(data: bytes) -> bytes | None:
    """Return the PE's WIN_CERTIFICATE table, or None when it has none."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        raise SigningError("not a PE image (missing MZ header)")
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise SigningError("not a PE image (missing PE signature)")
    optional = pe_offset + 24
    (magic,) = struct.unpack_from("<H", data, optional)
    directories = {0x10B: optional + 96, 0x20B: optional + 112}.get(magic)
    if directories is None:
        raise SigningError(f"unknown PE optional-header magic {magic:#x}")
    entry = directories + SECURITY_DIRECTORY_INDEX * 8
    offset, size = struct.unpack_from("<II", data, entry)
    if offset == 0 or size == 0:
        return None
    return data[offset : offset + size]


def signature_problem(data: bytes, publisher: str) -> str | None:
    """Describe why ``data`` is not a timestamped signature by ``publisher``."""
    blob = authenticode_blob(data)
    if blob is None or len(blob) < 8:
        return "no Authenticode signature (empty PE security directory)"
    _length, revision, cert_type = struct.unpack_from("<IHH", blob, 0)
    if revision != WIN_CERT_REVISION_2 or cert_type != WIN_CERT_TYPE_PKCS_SIGNED_DATA:
        return f"unexpected WIN_CERTIFICATE revision/type {revision:#x}/{cert_type:#x}"
    if publisher.encode("utf-8") not in blob:
        return f"signature does not name publisher {publisher!r}"
    if RFC3161_TIMESTAMP_OID not in blob:
        return "signature carries no RFC 3161 timestamp"
    return None


def _report(items: list[tuple[str, bytes]], publisher: str) -> None:
    failures = []
    for label, data in items:
        problem = signature_problem(data, publisher)
        if problem:
            failures.append(f"  {label}: {problem}")
        else:
            print(f"signed: {label}")
    if failures:
        raise SigningError("unsigned Windows release binaries:\n" + "\n".join(failures))


def verify_files(paths: list[Path], publisher: str) -> None:
    _report([(str(p), p.read_bytes()) for p in pe_files(paths)], publisher)


def verify_wheels(wheels: list[Path], publisher: str) -> None:
    """Every PE member of every wheel must carry the publisher's signature."""
    items: list[tuple[str, bytes]] = []
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            members = [n for n in archive.namelist() if is_pe_name(n)]
            if not members:
                raise SigningError(f"no Windows PE members in wheel {wheel}")
            items.extend((f"{wheel}!{n}", archive.read(n)) for n in members)
    _report(items, publisher)


def sign_files(jar: Path, config: SigningConfig, paths: list[Path]) -> None:
    files = pe_files(paths)
    env = dict(os.environ)
    env[TOKEN_ENV] = access_token()
    print(f"jsign: signing {len(files)} file(s) with {config.account}/{config.profile}")
    subprocess.run(jsign_command(jar, config, files), env=env, check=True)


AUTHENTICODE_PS = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:FASTLED_SIGNED_PE; "
    'Write-Output "$($s.Status)|$($s.SignerCertificate.Subject)"'
)


def authenticode_status(path: Path) -> tuple[str, str]:
    """Windows' own verdict (chain, timestamp, hash) via Get-AuthenticodeSignature."""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", AUTHENTICODE_PS],
        env={**os.environ, "FASTLED_SIGNED_PE": str(path)},
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    status, _, subject = completed.stdout.strip().partition("|")
    return status, subject


def smoke_files(paths: list[Path]) -> None:
    """Require Get-AuthenticodeSignature ``Valid`` for every PE."""
    bad = []
    for path in pe_files(paths):
        status, subject = authenticode_status(path)
        print(f"{status}: {path} [{subject}]")
        if status != "Valid":
            bad.append(f"  {path}: {status}")
    if bad:
        raise SigningError(
            "Windows release binaries without a Valid Authenticode signature:\n"
            + "\n".join(bad)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("mode", choices=("sign", "verify", "smoke"))
    parser.add_argument("--file", dest="files", type=Path, action="append", default=[])
    parser.add_argument(
        "--wheel", dest="wheels", type=Path, action="append", default=[]
    )
    parser.add_argument("--jsign-dir", type=Path, default=DEFAULT_JSIGN_DIR)
    args = parser.parse_args(argv)
    if not args.files and not args.wheels:
        parser.error("pass at least one --file or --wheel")
    if args.wheels and args.mode != "verify":
        parser.error("--wheel is only valid with verify")

    try:
        if args.mode == "smoke":
            smoke_files(args.files)
            return 0
        config = config_from_env(dict(os.environ))
        if args.mode == "sign":
            sign_files(fetch_jsign(args.jsign_dir), config, args.files)
        if args.files:
            verify_files(args.files, config.publisher)
        if args.wheels:
            verify_wheels(args.wheels, config.publisher)
    except SigningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
