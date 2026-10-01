#!/bin/sh
# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

# Keep the complete installer inside a function: a truncated piped download
# cannot execute the Python payload before the closing function is parsed.
weave_install_main() {
    if [ -n "${WEAVE_INSTALL_PYTHON:-}" ]; then
        if ! "$WEAVE_INSTALL_PYTHON" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
            echo "weave installer: WEAVE_INSTALL_PYTHON must select Python 3.12 or newer." >&2
            return 1
        fi
    else
        for weave_python in python3 python3.14 python3.13 python3.12; do
            if command -v "$weave_python" >/dev/null 2>&1 && "$weave_python" -I -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
                WEAVE_INSTALL_PYTHON=$weave_python
                break
            fi
        done
        if [ -z "${WEAVE_INSTALL_PYTHON:-}" ]; then
            echo "weave installer: Install Python 3.12 or newer with venv/pip support, then retry." >&2
            echo "Set WEAVE_INSTALL_PYTHON to select an existing interpreter." >&2
            return 1
        fi
    fi
    "$WEAVE_INSTALL_PYTHON" -I - "$@" <<'WEAVE_INSTALL_PYTHON'
# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Install the released CLI into an isolated, user-owned Python environment.

The identical source is embedded in install.sh; the parity test prevents drift.
Release hashes protect transport integrity, not against a compromised publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

REPOSITORY = "fireflyframework/firefly-weave"
API = f"https://api.github.com/repos/{REPOSITORY}/releases"
OWNER = "firefly-weave-cli-installer-v1"
MAX_DOWNLOAD = 64 * 1024 * 1024
TAG = re.compile(r"v?[0-9][A-Za-z0-9.+-]{0,79}\Z")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class InstallError(Exception):
    """An actionable installation failure without exposing subprocess secrets."""


def safe_name(value: object) -> str:
    if not isinstance(value, str) or not FILENAME.fullmatch(value) or ".." in value:
        raise InstallError("Release contains an unsafe artifact filename.")
    return value


def trusted_download_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise InstallError("Refusing a release download outside GitHub HTTPS.")


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Check every redirect before urllib sends another network request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, *, limit: int = MAX_DOWNLOAD) -> bytes:
    trusted_download_url(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": "firefly-weave-installer", "Accept": "application/json"}
    )
    try:
        with urllib.request.build_opener(GitHubRedirectHandler()).open(request, timeout=60) as response:
            trusted_download_url(response.url)
            data = response.read(limit + 1)
            if len(data) > limit:
                raise InstallError("Release response exceeds its size limit.")
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise InstallError(
                "No installable release was found. Try --prerelease or an exact --version tag; "
                "older releases without CLI installer artifacts are unsupported."
            ) from None
        raise InstallError(
            f"GitHub download failed (HTTP {exc.code}). Retry after checking GitHub availability."
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise InstallError("GitHub download failed; check your HTTPS connection and try again.") from exc


def fetch_release(destination: Path, version: str | None, prerelease: bool) -> Path:
    if version:
        if not TAG.fullmatch(version):
            raise InstallError("--version must be an exact, safe release tag, for example v0.1.0a2.")
        metadata = json.loads(download(f"{API}/tags/{urllib.parse.quote(version, safe='')}", limit=1024 * 1024))
    elif prerelease:
        releases = json.loads(download(f"{API}?per_page=100", limit=4 * 1024 * 1024))
        eligible = [item for item in releases if not item.get("draft") and item.get("published_at")]
        if not eligible:
            raise InstallError("No published releases are available.")
        metadata = max(eligible, key=lambda item: item["published_at"])
    else:
        metadata = json.loads(download(f"{API}/latest", limit=1024 * 1024))
    if not isinstance(metadata, dict) or metadata.get("draft"):
        raise InstallError("GitHub returned invalid release metadata.")
    tag = metadata.get("tag_name", "")
    if not isinstance(tag, str) or not TAG.fullmatch(tag) or (version and tag != version):
        raise InstallError("GitHub returned an unexpected release tag.")
    assets = {item["name"]: item["browser_download_url"] for item in metadata.get("assets", [])}

    def fetch(name: str) -> None:
        safe_name(name)
        expected = f"https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(tag, safe='')}/{name}"
        if assets.get(name) != expected:
            raise InstallError(
                f"Release {tag} lacks trusted CLI artifact {name}. "
                "Select a newer release with --prerelease or --version."
            )
        (destination / name).write_bytes(download(expected))

    fetch("SHA256SUMS")
    fetch("cli-install.json")
    sums = read_checksums(destination)
    verify_file(destination, "cli-install.json", sums)
    manifest = read_manifest(destination)
    if manifest["version"] != tag.removeprefix("v"):
        raise InstallError("Installation manifest version does not match the selected release tag.")
    for name in (manifest["wheel"], manifest["requirements"]):
        fetch(name)
    return destination


def read_checksums(source: Path) -> dict[str, str]:
    checksums = {}
    for line in (source / "SHA256SUMS").read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise InstallError("Malformed SHA256SUMS entry.")
        digest, name = match.groups()
        safe_name(name)
        if name in checksums:
            raise InstallError("Duplicate SHA256SUMS filename.")
        checksums[name] = digest
    return checksums


def verify_file(source: Path, name: str, sums: dict[str, str], expected: str | None = None) -> None:
    path = source / safe_name(name)
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"Release artifact {name} is missing or is a symlink.")
    if path.stat().st_size > MAX_DOWNLOAD:
        raise InstallError(f"Release artifact {name} exceeds its size limit.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if sums.get(name) != digest or (expected is not None and expected != digest):
        raise InstallError(f"Release checksum mismatch for {name}; no command was changed.")


def read_manifest(source: Path) -> dict:
    manifest = json.loads((source / "cli-install.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise InstallError("Unsupported CLI installation manifest schema.")
    version = manifest.get("version")
    if not isinstance(version, str) or not TAG.fullmatch(version):
        raise InstallError("Invalid release version in installation manifest.")
    wheel = safe_name(manifest.get("wheel"))
    if not wheel.startswith("firefly_weave-") or not wheel.endswith("-py3-none-any.whl"):
        raise InstallError("Release manifest must identify a Firefly Weave Python wheel.")
    if manifest.get("requirements") != "cli-requirements.txt":
        raise InstallError("Release manifest has an unexpected requirements filename.")
    for field in ("wheel_sha256", "requirements_sha256"):
        if not isinstance(manifest.get(field), str) or not HASH.fullmatch(manifest[field]):
            raise InstallError("Release manifest contains an invalid SHA-256 digest.")
    return manifest


def load_release(source: Path) -> dict:
    sums = read_checksums(source)
    verify_file(source, "cli-install.json", sums)
    manifest = read_manifest(source)
    verify_file(source, manifest["wheel"], sums, manifest["wheel_sha256"])
    verify_file(source, manifest["requirements"], sums, manifest["requirements_sha256"])
    return manifest


def clean_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PIP_")) and key not in {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
    }
    # pip's isolated mode still reads global configuration; explicitly disable it.
    environment["PIP_CONFIG_FILE"] = os.devnull
    return environment


def run(arguments: list[str], description: str) -> bytes:
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                arguments, env=clean_environment(), stdout=output, stderr=output, check=False, timeout=900
            )
        except subprocess.TimeoutExpired:
            raise InstallError(f"{description} timed out; the previous weave command is unchanged.") from None
        if result.returncode:
            raise InstallError(
                f"{description} failed (exit {result.returncode}). "
                "The previous weave command is unchanged. Check Python/venv support and package index connectivity."
            )
        output.seek(0, os.SEEK_END)
        size = output.tell()
        if size > 1024 * 1024:
            output.seek(size - 1024 * 1024)
        else:
            output.seek(0)
        return output.read()


def build_environment(source: Path, manifest: dict, destination: Path) -> None:
    print("Creating an isolated CLI environment...")
    run([sys.executable, "-I", "-m", "venv", str(destination)], "Python virtual environment creation")
    python = str(destination / "bin/python")
    print("Installing hash-locked CLI dependencies...")
    run(
        [
            python,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(source / manifest["requirements"]),
        ],
        "Locked dependency installation",
    )
    run(
        [
            python,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-deps",
            str(source / manifest["wheel"]),
        ],
        "CLI wheel installation",
    )
    run([python, "-I", "-m", "pip", "--isolated", "check"], "Dependency verification")
    run([python, "-I", "-c", "import httpx, keyring, pyfly"], "CLI client dependency verification")
    data = run([python, "-I", "-m", "firefly_weave.cli.main", "version", "--output", "json"], "CLI smoke test")
    try:
        actual = json.loads(data)
    except (ValueError, UnicodeError):
        raise InstallError("CLI smoke test returned invalid JSON; the previous command is unchanged.") from None
    if actual.get("version") != manifest["version"]:
        raise InstallError("Installed CLI version does not match the release manifest.")


def checked_path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise InstallError(f"Refusing symlink in managed installation path: {parent}")
    return path


def receipt_for(root: Path, bindir: Path) -> dict:
    receipt_path = root / "installation.json"
    if receipt_path.is_symlink():
        raise InstallError("Refusing symlinked installation receipt.")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if (
            not isinstance(receipt, dict)
            or receipt.get("owner") != OWNER
            or receipt.get("root") != str(root)
            or receipt.get("bin_dir") != str(bindir)
            or not isinstance(receipt.get("environments"), list)
        ):
            raise InstallError("Installation directory has an unrelated ownership receipt.")
        return receipt
    if root.exists() and any(path.name != ".install.lock" for path in root.iterdir()):
        raise InstallError("Installation directory is not empty and has no managed ownership receipt.")
    return {"owner": OWNER, "root": str(root), "bin_dir": str(bindir), "environments": []}


def check_command(command: Path, root: Path, receipt: dict) -> None:
    if not command.exists() and not command.is_symlink():
        return
    if not command.is_symlink():
        raise InstallError(f"Refusing to replace unmanaged command: {command}")
    target = command.readlink()
    if not target.is_absolute() or str(target.parent.parent) not in receipt["environments"]:
        raise InstallError(f"Refusing to replace unmanaged command symlink: {command}")
    if target.parent.parent.parent != root / "versions" or target.name != "weave" or target.parent.name != "bin":
        raise InstallError("Managed command points outside its owned environment.")
    checked_path(target.parent.parent)


def write_receipt(root: Path, receipt: dict) -> None:
    temporary = root / f".receipt-{uuid.uuid4().hex}.json"
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, root / "installation.json")


@contextmanager
def installation_lock(root: Path):
    """Serialize receipt and entrypoint updates without leaving stale process locks."""
    import fcntl

    try:
        descriptor = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise InstallError("Cannot safely open the managed installation lock.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise InstallError("Installation lock must be a regular file owned and writable only by this user.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another installer is modifying this installation; retry after it finishes.") from None
        yield
    finally:
        # Retain the lock inode: removing it permits concurrent locks on different files.
        os.close(descriptor)


def install(source: Path, root: Path, bindir: Path) -> None:
    manifest = load_release(source)
    root, bindir = checked_path(root), checked_path(bindir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with installation_lock(root):
        receipt = receipt_for(root, bindir)
        command = bindir / "weave"
        check_command(command, root, receipt)
        versions = checked_path(root / "versions")
        versions.mkdir(exist_ok=True, mode=0o700)
        bindir.mkdir(parents=True, exist_ok=True)
        write_receipt(root, receipt)
        destination = versions / f"{manifest['version']}-{uuid.uuid4().hex[:12]}"
        build_environment(source, manifest, destination)
        check_command(command, root, receipt)
        receipt["environments"].append(str(destination))
        write_receipt(root, receipt)
        temporary = bindir / f".weave-{uuid.uuid4().hex}"
        temporary.symlink_to(destination / "bin/weave")
        try:
            os.replace(temporary, command)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"Installed Firefly Weave {manifest['version']}: {command}")
        print("Run: " + shlex.quote(str(command)) + " --help")
        if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
            print("Add the following to your shell profile, or run it in this shell:")
            print("  export PATH=" + shlex.quote(str(bindir)) + ':"$PATH"')
        print(f"Previous environments are retained for rollback in {versions}.")


def uninstall(root: Path, bindir: Path) -> None:
    root, bindir = checked_path(root), checked_path(bindir)
    if not (root / "installation.json").is_file():
        raise InstallError("No managed installation receipt was found; nothing was removed.")
    with installation_lock(root):
        receipt = receipt_for(root, bindir)
        command = bindir / "weave"
        check_command(command, root, receipt)
        if command.is_symlink():
            command.unlink()
        print(f"Removed the managed weave entrypoint. Environments and receipt remain at {root}.")
        print("Remove retained environments manually when you no longer need rollback.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Firefly Weave CLI without sudo or a source checkout.")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--version", help="exact GitHub release tag, including v if present")
    selector.add_argument("--prerelease", action="store_true", help="allow the most recently published prerelease")
    selector.add_argument(
        "--from-release", type=Path, help="install an explicitly trusted, prepared local release directory"
    )
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".local/share/firefly-weave")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument(
        "--uninstall", action="store_true", help="remove only the managed entrypoint; retain environments"
    )
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 12) or platform.system() not in {"Darwin", "Linux"}:
            raise InstallError("Use Python 3.12 or newer on macOS, Linux, or WSL. Native Windows is not supported.")
        if args.uninstall:
            if args.version or args.prerelease or args.from_release:
                raise InstallError("--uninstall cannot be combined with a release selector.")
            uninstall(args.install_dir, args.bin_dir)
        elif args.from_release:
            install(args.from_release.expanduser().resolve(), args.install_dir, args.bin_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="weave-release-") as temporary:
                source = fetch_release(Path(temporary), args.version, args.prerelease)
                install(source, args.install_dir, args.bin_dir)
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError) as exc:
        # Raw dependency output and environment values can include proxy credentials.
        message = str(exc) if isinstance(exc, InstallError) else "Invalid release data or filesystem operation failed."
        print(f"weave installer: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
WEAVE_INSTALL_PYTHON
}
weave_install_main "$@"
