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

"""Restore an owned, stopped local runtime into a fresh retained database."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from firefly_weave.sdk.deployment import read_file, real_path, run_command

CATALOG = {
    "schemas": "SELECT nspname,pg_get_userbyid(nspowner),nspacl::text FROM pg_namespace WHERE nspname='public'",
    "tables": """SELECT n.nspname,c.relname,c.relkind::text,pg_get_userbyid(c.relowner),c.relacl::text,
        c.relrowsecurity,c.relforcerowsecurity,c.relreplident::text,c.reloptions
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f') ORDER BY 1,2""",
    "columns": """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,
        a.attacl::text,a.attidentity::text,a.attgenerated::text,pg_get_expr(d.adbin,d.adrelid)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped ORDER BY 1,a.attnum""",
    "policies": """SELECT c.relname,p.polname,p.polpermissive,p.polcmd::text,
        ARRAY(SELECT CASE WHEN role=0 THEN 'public' ELSE pg_get_userbyid(role) END
              FROM unnest(p.polroles) role ORDER BY 1),
        pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid)
        FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY 1,2""",
    "functions": """SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_userbyid(p.proowner),
        p.proacl::text,p.prosecdef,p.proconfig,pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.prokind IN ('f','p') ORDER BY 1,2""",
    "constraints": """SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY 1,2""",
    "indexes": "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1,2",
    "defaults": """SELECT pg_get_userbyid(d.defaclrole),n.nspname,d.defaclobjtype::text,d.defaclacl::text
        FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
        WHERE n.nspname='public' OR n.oid IS NULL ORDER BY 1,2,3""",
    "roles": """SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls
        FROM pg_roles WHERE rolname LIKE 'weave_%' ORDER BY 1""",
    "memberships": """SELECT pg_get_userbyid(roleid),pg_get_userbyid(member),pg_get_userbyid(grantor),
        admin_option,inherit_option,set_option FROM pg_auth_members
        WHERE pg_get_userbyid(roleid) LIKE 'weave_%' ORDER BY 1,2,3""",
}


def guard_control(value: str):
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.port not in {55433, 55434}
        or url.database != "weave_b1_control"
        or url.username != "weave_b1_owner"
    ):
        raise ValueError("An explicitly owned local PostgreSQL control database is required")
    return url


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_database_sql(name: str, source: dict) -> str:
    if not re.fullmatch(r"weave_restore_[a-f0-9]{32}", name) or source.get("provider") != "c":
        raise ValueError("Fresh target name and supported matching libc locale required")
    return (
        f"CREATE DATABASE {identifier(name)} TEMPLATE template0 OWNER {identifier(source['owner'])} "
        f"ENCODING {literal(source['encoding'])} LOCALE_PROVIDER libc "
        f"LC_COLLATE {literal(source['collate'])} LC_CTYPE {literal(source['ctype'])}"
    )


def require_same_manifest(before: dict, after: dict) -> None:
    if before != after:
        raise ValueError("Restored catalog/data manifest differs; retained target must not start")


def save(path: Path, value: dict | bytes) -> None:
    data = value if isinstance(value, bytes) else (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


async def manifest(url) -> dict:
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await connection.execute(text("SET LOCAL row_security=off"))
            if not await connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
                raise ValueError("Verified complete backup authority required")
            catalog = {}
            for name, query in CATALOG.items():
                catalog[name] = [list(row) for row in (await connection.execute(text(query))).all()]
            relations = (
                await connection.execute(
                    text(
                        "SELECT relname,relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='public' AND relkind IN ('r','p','S') ORDER BY relname"
                    )
                )
            ).all()
            data = {}
            for name, kind in relations:
                relation = "public." + identifier(name)
                query = (
                    f"SELECT json_build_object('last_value',last_value,'is_called',is_called)::text FROM {relation}"
                    if kind == "S"
                    else f"SELECT to_jsonb(t)::text AS value FROM ONLY {relation} t ORDER BY value"
                )
                digest, count = hashlib.sha256(), 0
                rows = await connection.stream(text(query))
                async for row in rows:
                    digest.update(row[0].encode() + b"\n")
                    count += 1
                data[name] = {"rows": count, "sha256": digest.hexdigest()}
            return {"catalog": catalog, "data": data}
    finally:
        await engine.dispose()


async def restore(runtime_file: Path, output: Path, context: str, container: str) -> dict:
    control_url = guard_control(os.environ.get("WEAVE_TEST_DATABASE_URL", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", context + container):
        raise ValueError("Explicit local context and container required")
    if control_url.port == 55433 and context != "colima-weave-tests":
        raise ValueError("The isolated acceptance backend requires its explicit context")
    runtime = dict(line.split("=", 1) for line in read_file(runtime_file, 65536, private=True).decode().splitlines())
    runtime = {key: shlex.split(value)[0] for key, value in runtime.items()}
    source_url = make_url(runtime["WEAVE_MIGRATION_DATABASE_URL"])
    source = source_url.database
    if (
        not source
        or not re.fullmatch(r"weave_b2_dev_[a-f0-9]{32}", source)
        or source_url.set(database="weave_b1_control") != control_url
    ):
        raise ValueError("Fresh owned runtime receipt required")
    for key in ("WEAVE_DATABASE_URL", "WEAVE_SCHEDULER_DATABASE_URL"):
        url = make_url(runtime[key])
        if (url.host, url.port, url.database) != (source_url.host, source_url.port, source):
            raise ValueError("Runtime identities must refer to the same owned source")
    output = real_path(output)
    output.mkdir(mode=0o700)
    command = ["docker", "--context", context]
    endpoint = json.loads(run_command(command + ["context", "inspect", context]))[0]["Endpoints"]["docker"]["Host"]
    if not endpoint.startswith("unix://"):
        raise ValueError("Local Docker socket context required")
    inspected = json.loads(run_command(command + ["inspect", container]))[0]
    container_id = inspected["Id"]
    mappings = inspected["NetworkSettings"]["Ports"].get("5432/tcp") or []
    if not inspected["State"]["Running"] or not any(
        item["HostPort"] == str(control_url.port) and item["HostIp"] in {"127.0.0.1", "::1"} for item in mappings
    ):
        raise ValueError("PostgreSQL container does not match the guarded loopback endpoint")
    pg = command + ["exec", "--user", "postgres", container_id]
    backend_guard = run_command(
        pg
        + [
            "psql",
            "-XAt",
            "-U",
            control_url.username,
            "-d",
            "weave_b1_control",
            "-c",
            "SELECT identity FROM weave_test_backend_guard",
        ]
    )
    if backend_guard.strip() != b"firefly-weave-b1-local-integration":
        raise ValueError("Container PostgreSQL guard mismatch")
    control = create_async_engine(control_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    target = "weave_restore_" + uuid4().hex
    began = time.monotonic()
    try:
        async with control.connect() as connection:
            if (
                await connection.scalar(text("SELECT identity FROM weave_test_backend_guard"))
                != "firefly-weave-b1-local-integration"
            ):
                raise ValueError("Local PostgreSQL guard missing")
            if not await connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")):
                raise ValueError("Verified owner-preserving restore administrator required")
            version = int(await connection.scalar(text("SHOW server_version_num")))
            if not 170000 <= version < 180000:
                raise ValueError("This local recipe requires PostgreSQL 17 tools/server")
            info = (
                (
                    await connection.execute(
                        text(
                            "SELECT pg_get_userbyid(datdba) AS owner,pg_encoding_to_char(encoding) AS encoding,"
                            "datcollate AS collate,datctype AS ctype,datlocprovider::text AS provider,"
                            "datacl::text AS acl "
                            "FROM pg_database WHERE datname=:name"
                        ),
                        {"name": source},
                    )
                )
                .mappings()
                .one()
            )
            source_info = dict(info)
            if source_info["acl"] is not None:
                raise ValueError("This local recipe requires the default database ACL; object ACLs are fully restored")
            for key in ("WEAVE_DATABASE_URL", "WEAVE_SCHEDULER_DATABASE_URL"):
                role = make_url(runtime[key]).username
                flags = (
                    await connection.execute(
                        text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=:role"), {"role": role}
                    )
                ).one()
                if any(flags) or role == source_info["owner"]:
                    raise ValueError("Runtime and scheduler must remain nonowner NOSUPERUSER NOBYPASSRLS")
            create_sql = create_database_sql(target, source_info)
            active = await connection.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"), {"name": source}
            )
            if active:
                raise ValueError("Source still has database sessions; stop every owned writer first")
            await connection.execute(text(f"ALTER DATABASE {identifier(source)} CONNECTION LIMIT 0"))
            active = await connection.scalar(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname=:name"), {"name": source}
            )
            if active:
                raise ValueError("A source session raced with fencing; stop it before a new retained attempt")
            # The source stays fenced; no cleanup or automatic reopening is performed.
            save(
                output / "trial.json",
                {
                    "source": source,
                    "target": target,
                    "source_fenced": True,
                    "postgres_container": container_id,
                    "database": source_info,
                    "server_version_num": version,
                },
            )
        before = await manifest(source_url)
        save(output / "before.json", before)
        dump = run_command(
            pg + ["pg_dump", "-U", control_url.username, "-d", source, "--format=custom"],
            timeout=180,
            limit=128 * 1024 * 1024,
        )
        if not dump.startswith(b"PGDMP"):
            raise ValueError("A complete custom-format archive is required")
        archive = output / "source.dump"
        save(archive, dump)
        remote = "/tmp/weave-restore-" + uuid4().hex
        root_pg = command + ["exec", "--user", "0", container_id]
        run_command(root_pg + ["mkdir", "-m", "0700", remote])
        run_command(command + ["cp", str(archive), container_id + ":" + remote + "/source.dump"])
        inventory = run_command(root_pg + ["pg_restore", "--list", remote + "/source.dump"], limit=4 * 1024 * 1024)
        save(output / "archive-inventory.txt", inventory)
        async with control.connect() as connection:
            await connection.execute(text(create_sql))
        target_url = source_url.set(database=target)
        target_engine = create_async_engine(target_url, hide_parameters=True)
        try:
            async with target_engine.connect() as connection:
                if await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'"
                    )
                ):
                    raise ValueError("Fresh target contains objects before restore")
        finally:
            await target_engine.dispose()
        run_command(
            root_pg
            + [
                "pg_restore",
                "-U",
                control_url.username,
                "-d",
                target,
                "--single-transaction",
                "--exit-on-error",
                remote + "/source.dump",
            ],
            timeout=180,
            limit=4 * 1024 * 1024,
            log_path=output / "restore.log",
        )
        after = await manifest(target_url)
        save(output / "after.json", after)
        require_same_manifest(before, after)
        require_same_manifest(before, await manifest(source_url))
        changed = dict(runtime)
        for key in ("WEAVE_DATABASE_URL", "WEAVE_SCHEDULER_DATABASE_URL", "WEAVE_MIGRATION_DATABASE_URL"):
            changed[key] = make_url(runtime[key]).set(database=target).render_as_string(hide_password=False)
        save(
            output / "target.env",
            ("\n".join(key + "=" + shlex.quote(value) for key, value in changed.items()) + "\n").encode(),
        )
        result = {
            "complete": True,
            "source": source,
            "target": target,
            "source_fenced": True,
            "archive_sha256": hashlib.sha256(dump).hexdigest(),
            "archive_bytes": len(dump),
            "table_sequence_count": len(before["data"]),
            "catalog_data_equal": True,
            "seconds": round(time.monotonic() - began, 3),
            "container_archive": remote + "/source.dump",
        }
        save(output / "restore.json", result)
        return result
    finally:
        await control.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--postgres-container", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(restore(args.runtime, args.output, args.context, args.postgres_container))
    except Exception:
        parser.exit(
            1, "Restore failed; inspect retained private evidence. No source reopening or cleanup was performed.\n"
        )
    print(json.dumps({"complete": result["complete"], "catalog_data_equal": result["catalog_data_equal"]}))
