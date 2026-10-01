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

"""Run an exact installed native application with one private test-only barrier.

This launcher is never part of the product wheel or production image context.
Run with ``python -I`` outside the checkout; source imports are not a fallback.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import socket
import sys
import zipfile
from pathlib import Path, PurePosixPath
from uuid import UUID

OWNERS = {
    "run": "firefly_weave.runtime.service:start",
    "completion": "firefly_weave.api.workers:complete",
    "signal": "firefly_weave.api.runs:signal",
    "provider_admission": "firefly_weave.providers.service:receive",
    "provider_dispatch": "firefly_weave.providers.dispatcher:dispatch_one",
}
PHASES = {f"{side}_{kind}_commit" for side in ("before", "after") for kind in OWNERS} | {"after_outbox_accept"}


class ArtifactError(RuntimeError):
    """The requested installed distribution is not the selected artifact."""


def verify_install(wheel: Path, expected: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or wheel.stat().st_size > 32 * 1024 * 1024:
        raise ArtifactError("Invalid wheel identity")
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected:
        raise ArtifactError("Wheel identity does not match")
    distribution = importlib.metadata.distribution("firefly-weave")
    base = Path(distribution.locate_file("firefly_weave")).resolve()
    prefix = Path(sys.prefix).resolve()
    if not base.is_relative_to(prefix) or "site-packages" not in base.parts or (base / "__init__.py").is_symlink():
        raise ArtifactError("Installed package origin is outside the selected environment")
    actual = {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    expected_files = set()
    size = 0
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ArtifactError("Duplicate wheel member")
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ArtifactError("Invalid wheel path")
            if not item.filename.startswith("firefly_weave/") or item.is_dir():
                continue
            size += item.file_size
            if size > 64 * 1024 * 1024 or item.file_size > 8 * 1024 * 1024:
                raise ArtifactError("Wheel resources exceed verification limits")
            relative = str(path.relative_to("firefly_weave"))
            installed = base / relative
            expected_files.add(relative)
            if installed.is_symlink() or not installed.is_file() or not installed.resolve().is_relative_to(base):
                raise ArtifactError("Installed member is missing or redirected")
            if hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(archive.read(item)).digest():
                raise ArtifactError("Installed member differs from selected wheel")
    if actual != expected_files or not actual:
        raise ArtifactError("Installed inventory differs from selected wheel")
    imported = importlib.util.find_spec("firefly_weave")
    if imported is None or Path(imported.origin).resolve() != base / "__init__.py":
        raise ArtifactError("Import would not use the verified installed package")
    return {"wheel_sha256": expected, "members": len(actual), "installed_origin": str(base)}


def validate_target(value: dict) -> dict:
    required = {"case", "phase", "method", "path", "scope"}
    if not required <= set(value) or set(value) - required - {"resource"} or value["phase"] not in PHASES:
        raise ValueError("A supported exact commit target is required")
    UUID(value["case"])
    background = "provider_dispatch" in value["phase"] or value["phase"] == "after_outbox_accept"
    if background:
        UUID(value["resource"])
    if (
        value["method"] != ("BACKGROUND" if background else "POST")
        or not value["path"].startswith("/")
        or len(value["path"]) > 512
    ):
        raise ValueError("Exact POST route required")
    if set(value["scope"]) != {"tenant_id", "project_id", "environment_id"}:
        raise ValueError("Complete scoped target required")
    for identifier in value["scope"].values():
        UUID(identifier)
    kind = value["phase"].split("_", 1)[1].removesuffix("_commit")
    return {**value, "owner": OWNERS.get(kind, "firefly_weave.operations.event_delivery:_settle")}


def load_barriers():
    path = Path(__file__).with_name("barriers.py")
    spec = importlib.util.spec_from_file_location("weave_test_barriers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def background_barrier(target, barrier, barriers):
    if "provider_dispatch" in target.phase:
        from firefly_weave.providers.dispatcher import ProviderDispatcher

        original = ProviderDispatcher.dispatch_one

        async def dispatch(self, scope, receipt_id):
            selected = str(receipt_id) == target.resource and all(
                str(getattr(scope, key)) == value for key, value in target.scope.items()
            )
            token = barriers._request_case.set(target.case if selected else None)
            try:
                return await original(self, scope, receipt_id)
            finally:
                barriers._request_case.reset(token)

        ProviderDispatcher.dispatch_one = dispatch
    elif target.phase == "after_outbox_accept":
        from firefly_weave.operations.event_delivery import OutboxDispatcher

        original = OutboxDispatcher._settle

        async def settle(self, scope, identifier, token, code, accepted, **kwargs):
            if (
                str(identifier) == target.resource
                and code == "ACK"
                and accepted
                and all(str(getattr(scope, key)) == value for key, value in target.scope.items())
            ):
                await barrier.pause(target.phase, {"case": target.case, "scope": target.scope})
            return await original(self, scope, identifier, token, code, accepted, **kwargs)

        OutboxDispatcher._settle = settle


def inject_local_teams_jwks(application, path, *, receiver_origin=None):
    """Replace only the key-fetch transport; native signature/claims checks remain."""
    import httpx
    from pyfly.client.ports.outbound import BoundedHttpClientPort

    from firefly_weave.connectors.egress import SecureHttpClient, origin

    allowed = origin(receiver_origin) if receiver_origin is not None else None
    if allowed is not None and (
        allowed[:2] != ("http", "127.0.0.1") or receiver_origin != f"http://127.0.0.1:{allowed[2]}"
    ):
        raise ValueError("Exact owned loopback receiver origin required")
    content = path.read_bytes()
    if not 0 < len(content) <= 262144:
        raise ValueError("Bounded local JWKS fixture required")

    class LocalKeys:
        async def request_bounded(self, method, url, **kwargs):
            if method == "GET" and url == "https://login.botframework.com/v1/.well-known/keys":
                return httpx.Response(200, content=content)
            if allowed is not None and method == "POST" and origin(url) == allowed:
                return await SecureHttpClient().request_bounded(method, url, **kwargs)
            raise RuntimeError("Teams protocol fixture forbids other outbound traffic")

    application.state.pyfly.context.container.register_instance(BoundedHttpClientPort, LocalKeys())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--barrier-fd", type=int)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--teams-jwks", type=Path)
    parser.add_argument("--teams-receiver-origin")
    args = parser.parse_args()
    if not sys.flags.isolated or not 1024 <= args.port <= 65535:
        raise ValueError("Isolated Python and an explicit unprivileged port required")
    identity = verify_install(args.wheel, args.wheel_sha256)
    print(json.dumps({"artifact": identity, "pid": os.getpid()}), flush=True)
    from firefly_weave.main import create_application

    if (args.barrier_fd is None) != (args.target is None):
        raise ValueError("Barrier descriptor and target must be supplied together")
    target = None
    if args.target is not None:
        if args.target.stat().st_size > 4096:
            raise ValueError("Oversized target")
        values = validate_target(json.loads(args.target.read_bytes()))
        barriers = load_barriers()
        target = barriers.CommitTarget(**values)
        barrier = barriers.BarrierChannel(socket.socket(fileno=args.barrier_fd), target.case)
        from firefly_weave.persistence.uow import UnitOfWork

        if target.phase != "after_outbox_accept":
            UnitOfWork.open = barriers.instrument_commit(UnitOfWork.open, target, barrier)
        background_barrier(target, barrier, barriers)
    application = create_application()
    if args.teams_jwks is not None:
        inject_local_teams_jwks(application, args.teams_jwks, receiver_origin=args.teams_receiver_origin)
    if target is not None:
        application = barriers.CorrelatedRequest(application, target)
    import uvicorn

    uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
