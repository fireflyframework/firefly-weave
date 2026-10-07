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

"""Test-only administration bridge for a verified installed predecessor."""

import argparse
import ast
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile

CHILD = Path(__file__).resolve()
LIMIT = 256 * 1024


def artifact_head(wheel, sha, *, current_sha=None):
    if sha == current_sha:
        raise ValueError("Predecessor artifact must differ from the current artifact")
    with Path(wheel).open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("Bounded pinned predecessor artifact required")
    with ZipFile(io.BytesIO(raw)) as archive:
        members = [item for item in archive.infolist() if item.filename == "firefly_weave/persistence/migrations.py"]
        if len(members) != 1 or members[0].file_size > LIMIT:
            raise ValueError("Unique bounded predecessor schema metadata required")
        with archive.open(members[0]) as source:
            raw = source.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Unique bounded predecessor schema metadata required")
    assignments = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if (
        len(assignments) != 1
        or not isinstance(assignments[0], ast.Assign)
        or len(assignments[0].targets) != 1
        or not isinstance(assignments[0].value, ast.Constant)
        or not isinstance(assignments[0].value.value, str)
        or re.fullmatch(r"[0-9]{4}_[a-z][a-z0-9_]*", assignments[0].value.value) is None
    ):
        raise ValueError("Unique literal predecessor schema metadata required")
    return assignments[0].value.value


def verify_expected_head(wheel, sha, expected_head):
    if artifact_head(wheel, sha) != expected_head:
        raise ValueError("Selected predecessor expected schema does not match pinned artifact")
    return expected_head


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_private(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > LIMIT:
            raise ValueError("Bounded private regular input required")
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Private input exceeds limit")
    return raw


def write_private(path, value):
    raw = json.dumps(value, sort_keys=True).encode()
    if len(raw) > LIMIT:
        raise ValueError("Private output exceeds limit")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def validate_receipt(value, expected):
    if (
        any(value.get(key) != item for key, item in expected.items())
        or not isinstance(value.get("pid"), int)
        or value["pid"] <= 0
        or value["pid"] == os.getpid()
        or not isinstance(value.get("members"), int)
        or value["members"] <= 0
        or "site-packages" not in Path(value.get("installed_origin", "")).parts
        or not isinstance(value.get("result"), dict)
    ):
        raise ValueError("Predecessor provisioning receipt does not match selected request/artifact/schema")
    return value["result"]


async def invoke(python, wheel, sha, directory, request):
    expected_head = artifact_head(wheel, sha)
    name = "access-" + uuid4().hex
    request_path, output = directory / (name + ".input.json"), directory / (name + ".output.json")
    digest = write_private(request_path, request)
    logs = directory / name
    logs.mkdir(mode=0o700)
    processes = load("predecessor_processes", "processes.py")
    owner = processes.ProcessOwner(logs, limit=LIMIT)
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "WEAVE_HOST_SECRET", "WEAVE_WORKER_SECRET", "WEAVE_DENIED_SECRET")
        if key in os.environ
    }
    try:
        await owner.command(
            str(python),
            "-I",
            str(CHILD),
            "--wheel",
            str(wheel),
            "--wheel-sha256",
            sha,
            "--expected-head",
            expected_head,
            "--request",
            str(request_path),
            "--output",
            str(output),
            env=env,
            timeout=60,
        )
        return validate_receipt(
            json.loads(read_private(output)), {"wheel_sha256": sha, "request_sha256": digest, "head": expected_head}
        )
    finally:
        await owner.close()


async def dispatch(request, expected_head):
    # Import after verify_install: every domain class below belongs to this child.
    import inspect

    from firefly_weave.access.service import AccessService
    from firefly_weave.persistence.migrations import SCHEMA_VERSION
    from firefly_weave.persistence.uow import UnitOfWork

    if expected_head != SCHEMA_VERSION:
        raise ValueError("Retained predecessor schema required")
    if request["operation"] == "inspect":
        return {
            "schema": SCHEMA_VERSION,
            "service_origin": inspect.getfile(AccessService),
            "uow_origin": inspect.getfile(UnitOfWork),
            "python": sys.executable,
        }
    from pyfly.container import Container
    from pyfly.container.scanner import scan_package
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from firefly_weave.access.models import Grant

    runtime = request["runtime"]
    owner = create_async_engine(runtime["WEAVE_MIGRATION_DATABASE_URL"], hide_parameters=True)
    try:
        async with owner.connect() as connection:
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if head != expected_head:
                raise ValueError("Provisioning database is not the selected predecessor schema")
    finally:
        await owner.dispose()
    if request["operation"] == "bootstrap":
        target = SimpleNamespace(
            runtime=runtime,
            owner_url=runtime["WEAVE_MIGRATION_DATABASE_URL"],
            tag=request["tag"],
            tokens=[],
            engine=None,
        )
        try:
            tenant = await load("predecessor_setup", "access_setup.py").provision(target, request["keycloak"])
            return {
                "admin_id": str(target.admin.id),
                "tenant_id": str(tenant),
                "head": target.migration_head,
                **{
                    key: str(getattr(target, key))
                    for key in ("host_id", "worker_id", "native_id", "other_tenant", "token_url")
                },
                "tokens": target.tokens,
            }
        finally:
            if target.engine is not None:
                await target.engine.dispose()
    if request["operation"] != "grant":
        raise ValueError("Unsupported predecessor administration operation")
    engine = create_async_engine(runtime["WEAVE_DATABASE_URL"], hide_parameters=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        graph = Container()
        scan_package("firefly_weave.access", graph)
        graph.register_instance(async_sessionmaker, sessions)
        graph.register_instance(UnitOfWork, UnitOfWork(sessions))
        access = graph.resolve(AccessService)
        actor = await access.load_principal(UUID(request["admin_id"]))
        identifier = await access.grant(actor, UUID(request["principal_id"]), Grant.model_validate(request["grant"]))
        return {"grant_id": str(identifier)}
    finally:
        await engine.dispose()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not sys.flags.isolated:
        raise ValueError("Isolated installed interpreter required")
    proof = load("predecessor_artifact", "installed_api.py").verify_install(args.wheel, args.wheel_sha256)
    expected_head = verify_expected_head(args.wheel, args.wheel_sha256, args.expected_head)
    raw = read_private(args.request)
    result = await dispatch(json.loads(raw), expected_head)
    write_private(
        args.output,
        {
            **proof,
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "head": expected_head,
            "pid": os.getpid(),
            "result": result,
        },
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit(
            "Installed predecessor administration failed; retained private inputs require inspection"
        ) from None
