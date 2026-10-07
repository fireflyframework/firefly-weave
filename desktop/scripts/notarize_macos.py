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

"""Build notarized macOS installers only from an explicitly approved release tag."""

import argparse
import base64
import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

CREDENTIALS = (
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
    "APPLE_TEAM_ID",
    "APPLE_API_ISSUER",
    "APPLE_API_KEY",
    "APPLE_API_PRIVATE_KEY",
)
STATE = "weave-macos-signing-state.json"


def verify_source(root, env, version):
    if (
        env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REPOSITORY") != "fireflyframework/firefly-weave"
        or env.get("GITHUB_REF") != f"refs/tags/v{version}"
    ):
        raise RuntimeError("Signing requires an explicitly dispatched trusted release tag")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    tag = subprocess.check_output(["git", "rev-parse", f"refs/tags/v{version}^{{commit}}"], cwd=root, text=True).strip()
    if head != tag or head != env.get("GITHUB_SHA"):
        raise RuntimeError("Release tag, workflow commit and checked-out commit must match")


def validate_credentials(env):
    missing = [name for name in CREDENTIALS if not env.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing signing credentials: " + ", ".join(missing))
    team = env["APPLE_TEAM_ID"]
    if not re.fullmatch(r"[A-Z0-9]{10}", team) or not re.fullmatch(
        r"Developer ID Application: .+ \(" + re.escape(team) + r"\)", env["APPLE_SIGNING_IDENTITY"]
    ):
        raise RuntimeError("A Developer ID Application identity from the expected team is required")
    if not re.fullmatch(r"[A-Z0-9]{10}", env["APPLE_API_KEY"]):
        raise RuntimeError("Invalid APPLE_API_KEY identifier")
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", env["APPLE_API_ISSUER"]):
        raise RuntimeError("Invalid APPLE_API_ISSUER identifier")
    if not env["APPLE_API_PRIVATE_KEY"].startswith("-----BEGIN PRIVATE KEY-----"):
        raise RuntimeError("Invalid APPLE_API_PRIVATE_KEY format")
    try:
        base64.b64decode(env["APPLE_CERTIFICATE"], validate=True)
    except ValueError:
        raise RuntimeError("Invalid APPLE_CERTIFICATE encoding") from None


def quiet(command, **kwargs):
    # Tool errors can contain passwords or private paths; never echo argv/output.
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError(f"{Path(command[0]).name} failed; signing was not completed") from None


def private_file(path, content):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(content)


def cleanup(temp):
    state = temp / STATE
    if not state.exists():
        return
    record = json.loads(state.read_text())
    directory = Path(record["directory"])
    if directory.parent != temp.resolve() or not directory.name.startswith("weave-signing-"):
        raise RuntimeError("Refusing cleanup outside the owned signing directory")
    try:
        quiet(["security", "list-keychains", "-d", "user", "-s", *record["search_list"]], timeout=30)
        if record["keychain_created"] and directory.exists():
            quiet(["security", "delete-keychain", str(directory / "signing.keychain-db")], timeout=30)
    finally:
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=False)
    state.unlink()


@contextmanager
def signing_session(env):
    validate_credentials(env)
    temp = Path(env["RUNNER_TEMP"]).resolve()
    state = temp / STATE
    if state.exists():
        raise RuntimeError("An earlier signing session requires cleanup")
    previous = shlex.split(quiet(["security", "list-keychains", "-d", "user"], timeout=30).stdout)
    directory = Path(tempfile.mkdtemp(prefix="weave-signing-", dir=temp))
    record = {"directory": str(directory), "search_list": previous, "keychain_created": False}
    private_file(state, json.dumps(record).encode())
    keychain = directory / "signing.keychain-db"
    password = os.urandom(32).hex()
    try:
        certificate = directory / "certificate.p12"
        key = directory / "AuthKey.p8"
        private_file(certificate, base64.b64decode(env["APPLE_CERTIFICATE"], validate=True))
        private_file(key, env["APPLE_API_PRIVATE_KEY"].encode())
        quiet(["security", "create-keychain", "-p", password, str(keychain)], timeout=30)
        record["keychain_created"] = True
        state.write_text(json.dumps(record))
        quiet(["security", "set-keychain-settings", "-lut", "21600", str(keychain)], timeout=30)
        quiet(["security", "unlock-keychain", "-p", password, str(keychain)], timeout=30)
        quiet(
            [
                "security",
                "import",
                str(certificate),
                "-k",
                str(keychain),
                "-P",
                env["APPLE_CERTIFICATE_PASSWORD"],
                "-T",
                "/usr/bin/codesign",
            ],
            timeout=30,
        )
        certificate.unlink()
        quiet(
            [
                "security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:,codesign:",
                "-s",
                "-k",
                password,
                str(keychain),
            ],
            timeout=30,
        )
        quiet(["security", "list-keychains", "-d", "user", "-s", str(keychain), *previous], timeout=30)
        identities = quiet(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], timeout=30).stdout
        found = re.findall(r'\b[0-9A-Fa-f]{40} "([^"]+)"', identities)
        if found != [env["APPLE_SIGNING_IDENTITY"]]:
            raise RuntimeError("Imported keychain must contain exactly the expected Developer ID Application identity")
        child = {name: value for name, value in env.items() if not name.startswith("APPLE_")}
        child.update({name: env[name] for name in ("APPLE_SIGNING_IDENTITY", "APPLE_API_ISSUER", "APPLE_API_KEY")})
        child["APPLE_API_KEY_PATH"] = str(key)
        yield child, directory
    finally:
        cleanup(temp)


def verify_signature_details(details, identity, team, *, runtime=True):
    lines = details.splitlines()
    if (
        f"Authority={identity}" not in lines
        or f"TeamIdentifier={team}" not in lines
        or not any(line.startswith("Timestamp=") and line != "Timestamp=none" for line in lines)
        or (runtime and not any("flags=" in line and "runtime" in line for line in lines))
    ):
        raise RuntimeError("Developer ID signature, team, secure timestamp or hardened runtime mismatch")


def verify_signed(path, identity, team, *, runtime=True):
    quiet(["codesign", "--verify", "--strict", "--verbose=4", str(path)], timeout=60)
    details = quiet(["codesign", "--display", "--verbose=4", str(path)], timeout=60)
    verify_signature_details(details.stdout + details.stderr, identity, team, runtime=runtime)
    if runtime:
        entitlements = quiet(["codesign", "--display", "--entitlements", ":-", str(path)], timeout=60)
        import plistlib

        if entitlements.stdout.strip() and plistlib.loads(entitlements.stdout.encode()).get(
            "com.apple.security.get-task-allow"
        ):
            raise RuntimeError("Debugging entitlement is forbidden in a release signature")


def accepted_submission(response):
    if response.get("status") != "Accepted" or not response.get("id"):
        raise RuntimeError("Apple notarization must finish with Accepted status")
    return response["id"]


def verify_tickets(app, dmg, identity, team):
    seals = runpy.run_path(str(Path(__file__).with_name("verify_macos_bundle.py")))
    seals["verify"](app)
    for binary in sorted((app / "Contents/MacOS").iterdir()):
        if binary.is_file():
            verify_signed(binary, identity, team)
    verify_signed(app, identity, team)
    verify_signed(dmg, identity, team, runtime=False)
    for path in (app, dmg):
        quiet(["xcrun", "stapler", "validate", str(path)], timeout=120)
    quiet(["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)], timeout=120)
    quiet(
        ["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=4", str(dmg)],
        timeout=120,
    )
    expected = {path.name: digest(path) for path in (app / "Contents/MacOS").iterdir() if path.is_file()}

    def check_mounted(mounted):
        actual = {path.name: digest(path) for path in (mounted / "Contents/MacOS").iterdir() if path.is_file()}
        if actual != expected:
            raise RuntimeError("The DMG must contain the exact verified application executables")
        verify_signed(mounted, identity, team)
        quiet(["xcrun", "stapler", "validate", str(mounted)], timeout=120)
        quiet(["spctl", "--assess", "--type", "execute", "--verbose=4", str(mounted)], timeout=120)

    seals["verify_dmg"](dmg, trust_check=check_mounted)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(root, target, env):
    config = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text())
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    verify_source(root, env, version)
    if sys.platform != "darwin" or target not in {"aarch64-apple-darwin", "x86_64-apple-darwin"}:
        raise RuntimeError("Notarization requires a matching native macOS runner")
    output = root / "desktop/work/notarized-assets"
    if output.exists():
        raise RuntimeError("Refusing to reuse existing notarized assets")
    with signing_session(env) as (child, directory):
        override = directory / "tauri-signing.json"
        override.write_text(
            json.dumps(
                {"bundle": {"macOS": {"signingIdentity": env["APPLE_SIGNING_IDENTITY"], "hardenedRuntime": True}}}
            )
        )
        print("Freezing Developer ID host and building the notarized application", flush=True)
        subprocess.run(
            [sys.executable, str(root / "desktop/scripts/build_sidecar.py"), "--target", target],
            check=True,
            cwd=root,
            env=child,
        )
        subprocess.run(
            ["npm", "run", "build", "--", "--target", target, "--bundles", "app,dmg", "--config", str(override)],
            check=True,
            cwd=root / "desktop",
            env=child,
        )
        bundle = root / "desktop/src-tauri/target" / target / "release/bundle"
        app = bundle / "macos/Firefly Weave Studio.app"
        dmgs = list((bundle / "dmg").glob("*.dmg"))
        if len(dmgs) != 1:
            raise RuntimeError("Expected exactly one DMG")
        dmg = dmgs[0]
        # Tauri submits and staples the app before building the DMG. The outer
        # installer receives its own explicit Accepted result and stapled ticket.
        quiet(["xcrun", "stapler", "validate", str(app)], timeout=120)
        quiet(["codesign", "--force", "--timestamp", "--sign", env["APPLE_SIGNING_IDENTITY"], str(dmg)], timeout=120)
        response = quiet(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(dmg),
                "--key",
                child["APPLE_API_KEY_PATH"],
                "--key-id",
                env["APPLE_API_KEY"],
                "--issuer",
                env["APPLE_API_ISSUER"],
                "--wait",
                "--timeout",
                "30m",
                "--output-format",
                "json",
            ],
            timeout=1900,
            env=child,
        )
        submission = accepted_submission(json.loads(response.stdout))
        quiet(["xcrun", "stapler", "staple", str(dmg)], timeout=120)
        verify_tickets(app, dmg, env["APPLE_SIGNING_IDENTITY"], env["APPLE_TEAM_ID"])
        host = app / "Contents/MacOS/weave-studio-host"
        subprocess.run(
            [sys.executable, str(root / "desktop/scripts/smoke_sidecar.py"), str(host), "--expected-version", version],
            check=True,
            env=child,
            cwd=root,
        )
        metadata = {
            "product_version": config["version"],
            "python_version": version,
            "target": target,
            "commit": env["GITHUB_SHA"],
            "tag": env["GITHUB_REF"].removeprefix("refs/tags/"),
            "unsigned": False,
            "signing": "developer-id",
            "notarized": True,
            "team_id": env["APPLE_TEAM_ID"],
            "dmg_submission_id": submission,
            "app_ticket_validated": True,
            "dmg_ticket_validated": True,
            "gatekeeper_assessed": True,
            "dmg_sha256": digest(dmg),
        }
        host_metadata = json.loads((root / "desktop/work/sidecar-manifest.json").read_text())
        host_metadata.update(
            {
                "pre_sign_sha256": host_metadata["sha256"],
                "pre_sign_filename": host_metadata["filename"],
                "filename": host.name,
                "sha256": digest(host),
                "digest_scope": "signed-app-Contents/MacOS",
            }
        )
    # No distributable assets are retained if credential cleanup fails.
    output.mkdir(parents=True)
    shutil.copy2(dmg, output / f"firefly-weave-studio-{config['version']}-{target}.dmg")
    (output / f"weave-studio-{target}-build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (output / f"weave-studio-{target}-host.json").write_text(json.dumps(host_metadata, indent=2) + "\n")
    subprocess.run([sys.executable, str(root / "desktop/scripts/hash_installers.py"), str(output)], check=True)
    (output / "SHA256SUMS").rename(output / f"weave-studio-{target}-SHA256SUMS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--verify-source", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.cleanup:
        cleanup(Path(os.environ["RUNNER_TEMP"]).resolve())
        return
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    verify_source(root, os.environ, version)
    if args.verify_source:
        return

    def interrupted(signum, frame):
        raise SystemExit("Signing interrupted; cleaning up temporary credentials")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    build(root, args.target, os.environ)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc) if isinstance(exc, RuntimeError) else "Release build failed") from None
