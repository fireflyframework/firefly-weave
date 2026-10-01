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

"""Run explicit local and release gates, retaining bounded private stage evidence."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from firefly_weave.sdk.deployment import real_path, run_command

RELEASE_TESTS = (
    "tests/e2e/test_packaging.py",
    "tests/e2e/test_recovery_matrix.py",
    "tests/e2e/test_restore.py",
    "tests/e2e/test_worker_shutdown.py",
    "tests/benchmarks/test_queue_load.py",
)


def release_prerequisites(root: Path, context: str | None) -> None:
    if context != "colima-weave-tests" or os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") != context:
        raise ValueError("An explicit owned release Docker context is required")
    required = (
        "WEAVE_TEST_DATABASE_URL",
        "WEAVE_KC_ADMIN_SECRET",
        "WEAVE_HOST_SECRET",
        "WEAVE_WORKER_SECRET",
        "WEAVE_DENIED_SECRET",
        "WEAVE_TEST_KAFKA_IMAGE",
        "WEAVE_TEST_POSTGRES_CONTAINER",
        "WEAVE_PREDECESSOR_PYTHON",
        "WEAVE_PREDECESSOR_WHEEL",
        "WEAVE_PREDECESSOR_WHEEL_SHA256",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Required real release dependencies are not configured")
    if os.environ.get("WEAVE_KEYCLOAK_TEST_URL") != "http://localhost:18081":
        raise ValueError("Owned Keycloak endpoint required")
    from urllib.parse import urlsplit

    database = urlsplit(os.environ["WEAVE_TEST_DATABASE_URL"])
    if (
        database.hostname not in {"localhost", "127.0.0.1"}
        or database.port != 55433
        or database.path != "/weave_b1_control"
    ):
        raise ValueError("Owned PostgreSQL control database required")
    if any(not (root / path).is_file() for path in RELEASE_TESTS):
        raise ValueError("Every required release gate must exist")


def run(root: Path, evidence: Path, *, release: bool, context: str | None, integration: bool = False) -> int:
    if release and integration:
        raise ValueError("Select either integration or release checks")
    root, evidence = real_path(root), real_path(evidence)
    evidence.mkdir(mode=0o700)
    python = sys.executable
    # The fixture belongs only to the test interpreter, never an artifact closure.
    # Dependency resolution is forbidden: its framework pin must not replace the
    # corrected framework already selected by the locked project environment.
    integration_stages = [
        (
            "test-fixtures",
            ["uv", "pip", "install", "--python", python, "--no-deps", str(root / "tests/fixtures/e2-provider")],
            120,
        ),
        (
            "integration",
            [python, "-m", "pytest", "tests/integration", "-q", "--tb=short", "--show-capture=no"],
            1800,
        ),
    ]
    stages = (
        integration_stages
        if integration
        else [
            ("source", [python, "scripts/source_coverage.py", "--strict"], 60),
            ("docs", [python, "scripts/check_docs.py"], 60),
            ("docs-site", [python, "-m", "mkdocs", "build", "--strict"], 120),
            ("lint", [python, "-m", "ruff", "check", "src", "tests", "examples", "scripts"], 60),
            ("format", [python, "-m", "ruff", "format", "--check", "src", "tests", "examples", "scripts"], 60),
            ("types", [python, "-m", "mypy"], 120),
            (
                "unit-contracts",
                [python, "-m", "pytest", "tests/unit", "tests/contracts", "-q", "--tb=short", "--show-capture=no"],
                600,
            ),
            ("prepare", [python, "scripts/prepare_release.py", "--output", str(evidence / "release")], 300),
            (
                "installed-artifacts",
                [
                    python,
                    "scripts/verify_artifacts.py",
                    "--release",
                    str(evidence / "release"),
                    "--output",
                    str(evidence / "installed"),
                ],
                1200,
            ),
        ]
    )
    if release:
        stages.extend(
            [
                (
                    "images",
                    [
                        python,
                        "scripts/build_release_images.py",
                        "--release",
                        str(evidence / "release"),
                        "--output",
                        str(evidence / "images"),
                        "--context",
                        str(context),
                    ],
                    4800,
                ),
                *integration_stages,
                ("process-e2e", [python, "-m", "pytest", "tests/e2e", "-q", "--tb=short", "--show-capture=no"], 2400),
                (
                    "queue-measurement",
                    [
                        python,
                        "-m",
                        "pytest",
                        "tests/benchmarks/test_queue_load.py",
                        "-q",
                        "--tb=short",
                        "--show-capture=no",
                    ],
                    1200,
                ),
            ]
        )
    results = {name: {"status": "not_run"} for name, _, _ in stages}
    failed = False
    try:
        if release:
            release_prerequisites(root, context)
            os.environ["WEAVE_IMAGE_PROOF_PATH"] = str(evidence / "native-image.json")
            os.environ["WEAVE_D5_EVIDENCE"] = str(evidence)
        for name, command, timeout in stages:
            print("Running " + name, flush=True)
            started = time.monotonic()
            try:
                run_command(command, timeout=timeout, limit=4 * 1024 * 1024, log_path=evidence / (name + ".log"))
                if name == "images":
                    images = json.loads((evidence / "images/images.json").read_text())
                    prepared = json.loads((evidence / "release/release.json").read_text())
                    os.environ["WEAVE_E2E_PYTHON"] = str(evidence / "installed/server/bin/python")
                    os.environ["WEAVE_E2E_WORKER_PYTHON"] = str(evidence / "installed/worker/bin/python")
                    os.environ["WEAVE_E2E_TEAMS_PYTHON"] = str(evidence / "installed/teams/bin/python")
                    os.environ["WEAVE_E2E_WHEEL"] = str(evidence / "release/artifacts" / prepared["wheel"])
                    os.environ["WEAVE_E2E_WHEEL_SHA256"] = prepared["wheel_sha256"]
                    for closure, variable in (
                        ("server", "WEAVE_E2E_IMAGE_ID"),
                        ("server", "WEAVE_B8_IMAGE_ID"),
                        ("server", "WEAVE_D1_IMAGE_ID"),
                        ("kafka", "WEAVE_D2_IMAGE_ID"),
                        ("worker", "WEAVE_E2E_WORKER_IMAGE_ID"),
                        ("teams", "WEAVE_E2E_TEAMS_IMAGE_ID"),
                        ("kafka", "WEAVE_E2E_KAFKA_IMAGE_ID"),
                    ):
                        os.environ[variable] = images["images"][closure]
                results[name] = {"status": "passed", "seconds": round(time.monotonic() - started, 3)}
            except BaseException:
                results[name] = {"status": "failed", "seconds": round(time.monotonic() - started, 3)}
                failed = True
                break
    except BaseException:
        failed = True
        results["prerequisites"] = {"status": "failed"}
    finally:
        with os.fdopen(os.open(evidence / "checks.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(
                {"release": release, "integration": integration, "complete": not failed, "stages": results},
                stream,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
    print("Checks failed; inspect private evidence." if failed else "All requested checks passed.", flush=True)
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--release", action="store_true")
    mode.add_argument("--integration", action="store_true", help="Prepare test-only fixtures and run integration")
    parser.add_argument("--docker-context")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    root = Path(__file__).resolve().parents[1]
    if args.evidence is None:
        parent = root / "build"
        parent.mkdir(exist_ok=True)
        args.evidence = parent / ("checks-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12])
    if Path.cwd().resolve() != root:
        parser.exit(2, "Run the check command from the project root.\n")
    try:
        raise SystemExit(
            run(root, args.evidence, release=args.release, context=args.docker_context, integration=args.integration)
        )
    except (OSError, ValueError):
        parser.exit(2, "Evidence directory must be new with an existing, nonsymlinked parent.\n")
