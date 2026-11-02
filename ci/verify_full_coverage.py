"""Require a successful GitHub full-coverage dispatch at the release SHA."""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

MANIFEST = Path(__file__).with_name("full_coverage.json")
PLATFORMS = {
    "linux-x86",
    "linux-arm",
    "windows-x86",
    "windows-arm",
    "macos-x86",
    "macos-arm",
}


def get_json(path):
    request = urllib.request.Request(
        "https://api.github.com/" + path,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url):
    """Fetch an artifact zip without leaking the token across the redirect.

    GitHub answers with a redirect to a pre-signed blob URL, and that host
    rejects a request that also carries an Authorization header (HTTP 401).
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location")
        if error.code not in (301, 302, 303, 307, 308) or not location:
            raise
    with opener.open(urllib.request.Request(location), timeout=60) as response:
        return response.read()


def validate_report(report, manifest, sha):
    if report.get("schema_version") != 2 or report.get("sha") != sha:
        return False
    if report.get("event") != "workflow_dispatch":
        return False
    names = manifest.get("workflows", [])
    if not names or report.get("manifest") != names:
        return False
    tests = manifest.get("platform_tests", {})
    if set(tests) != PLATFORMS or report.get("platform_tests") != tests:
        return False
    if any(not checks for checks in tests.values()):
        return False
    if any(
        not check["workflow"].startswith(platform + "-")
        for platform, checks in tests.items()
        for check in checks
    ):
        return False
    cells = report.get("cells", {})
    if set(cells) != set(names):
        return False
    if not all(
        cell.get("result") == "passed" and isinstance(cell.get("run_id"), int)
        for cell in cells.values()
    ):
        return False
    if report.get("test_execution_gaps") != []:
        return False
    for checks in tests.values():
        for check in checks:
            if check["workflow"] not in cells:
                return False
            jobs = cells[check["workflow"]].get("jobs", [])
            if not any(
                step.get("name") == check["step"]
                and step.get("conclusion") == "success"
                for job in jobs
                if job.get("conclusion") == "success"
                for step in job.get("steps", [])
            ):
                return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    if len(args.sha) != 40 or any(c not in "0123456789abcdef" for c in args.sha):
        parser.error("--sha must be a lowercase 40-character commit SHA")
    manifest = json.loads(MANIFEST.read_text())
    if manifest.get("schema_version") != 2:
        parser.error("unsupported coverage manifest")
    repo = os.environ["GITHUB_REPOSITORY"]
    runs = get_json(
        f"repos/{repo}/actions/workflows/full-coverage.yml/runs?head_sha={args.sha}&event=workflow_dispatch&per_page=100"
    )["workflow_runs"]
    for run in runs:
        if run["head_sha"] != args.sha or run["conclusion"] != "success":
            continue
        artifacts = get_json(
            f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100"
        )["artifacts"]
        for artifact in artifacts:
            if artifact["name"] != f"full-coverage-{run['id']}" or artifact["expired"]:
                continue
            with zipfile.ZipFile(
                io.BytesIO(download(artifact["archive_download_url"]))
            ) as archive:
                report = json.loads(archive.read("full-coverage.json"))
            if validate_report(report, manifest, args.sha):
                print(
                    f"Verified all {len(manifest['workflows'])} platform workflows: {run['html_url']}"
                )
                return 0
    print("No passing exact-SHA release full-coverage report found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
