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

"""New owned PostgreSQL TLS container; successful verification and certificate rejection."""

import asyncio
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision, ResolvedSecret

pytestmark = pytest.mark.integration


async def docker(*args):
    if os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") != "colima-weave-tests":
        pytest.fail("D1 TLS proof requires explicit WEAVE_TEST_DOCKER_CONTEXT=colima-weave-tests")
    process = await asyncio.create_subprocess_exec(
        "docker",
        "--context",
        "colima-weave-tests",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0, "Task-owned Docker command failed; configuration withheld"
    return stdout.decode().strip()


async def test_real_postgres_verified_tls_and_invalid_ca(tmp_path):
    from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy

    image = "postgres:17-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"
    await docker("image", "inspect", image, "--format", "{{.Id}}")
    key, cert = tmp_path / "server.key", tmp_path / "server.crt"
    process = await asyncio.create_subprocess_exec(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        "1",
        "-subj",
        "/CN=weave-d1-owned",
        "-addext",
        "subjectAltName=IP:127.0.0.1",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await process.wait() == 0
    password = secrets.token_hex(24)
    env = tmp_path / "postgres.env"
    env.write_text("POSTGRES_PASSWORD=" + password + "\nPOSTGRES_DB=d1_tls\n")
    env.chmod(0o600)
    name = "weave-d1-tls-" + uuid4().hex[:12]
    identifier = await docker(
        "create",
        "--name",
        name,
        "-p",
        "127.0.0.1::5432",
        "--env-file",
        str(env),
        image,
        "sh",
        "-c",
        "chown postgres:postgres /tmp/server.key /tmp/server.crt && chmod 600 /tmp/server.key "
        "&& exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c "
        "ssl_key_file=/tmp/server.key",
    )
    await docker("cp", str(key), identifier + ":/tmp/server.key")
    await docker("cp", str(cert), identifier + ":/tmp/server.crt")
    await docker("start", identifier)
    try:
        for _ in range(80):
            result = await docker(
                "exec",
                identifier,
                "sh",
                "-c",
                "pg_isready -h 127.0.0.1 -U postgres -d d1_tls >/dev/null && echo ready || true",
            )
            if result == "ready":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Owned TLS PostgreSQL did not become ready")
        port = int((await docker("port", identifier, "5432/tcp")).rsplit(":", 1)[-1])
        revision = ConnectionRevision(
            id=uuid4(),
            revision=1,
            name="tls",
            connector_version_id=uuid4(),
            connector="weave-postgresql@1.0.0",
            connector_digest="a" * 64,
            adapter="weave-postgresql",
            config={
                "dialect": "postgresql",
                "host": "127.0.0.1",
                "port": port,
                "database": "d1_tls",
                "user": "postgres",
                "role": "read",
                "tls": "verify-full",
            },
            secretRef={"password": "owned-tls"},
            allowed_destinations=(f"postgresql://127.0.0.1:{port}",),
        )
        bound = BoundConnection("tls", revision, lambda slot: ResolvedSecret(value=password))
        trusted = PostgresConnector(PostgresPolicy(private_networks=("127.0.0.0/8",), ca_file=str(cert)))
        untrusted = PostgresConnector(PostgresPolicy(private_networks=("127.0.0.0/8",)))
        probe = await trusted._connect(revision, password)
        await probe.close()
        assert (await trusted.test_connection(bound)).ok
        assert not (await untrusted.test_connection(bound)).ok
        connection = await trusted._connect(revision, password)
        try:
            assert await connection.fetchval("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()") is True
        finally:
            await connection.close()
        proof = {
            "container": identifier,
            "image": await docker("inspect", "--format", "{{.Image}}", identifier),
            "verified_tls": True,
            "untrusted_certificate_rejected": True,
            "server_version": await docker("exec", identifier, "postgres", "--version"),
        }
        target = os.environ.get("WEAVE_D1_TLS_PROOF_PATH")
        if target:
            Path(target).write_text(json.dumps(proof, indent=2) + "\n")
    finally:
        await docker("stop", "--time", "5", identifier)
        env.unlink()
