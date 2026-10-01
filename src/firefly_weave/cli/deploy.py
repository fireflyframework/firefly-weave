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

"""Explicit local worker image preparation and owned Compose deployment."""

from pathlib import Path

import click

from firefly_weave.cli import EXIT_INVALID, emit_result, error_result
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.sdk.deployment import deploy_worker, package_worker


@click.group()
def worker() -> None:
    """Package or deploy a worker using explicit local artifacts."""


@worker.command("package")
@click.option("--manifest", required=True, type=click.Path(path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--wheel", type=click.Path(path_type=Path))
@click.option("--requirements", type=click.Path(path_type=Path))
@click.option("--entrypoint", type=click.Path(path_type=Path))
@click.option("--format", "format_", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def package(
    ctx: click.Context,
    manifest: Path,
    output: Path,
    wheel: Path | None,
    requirements: Path | None,
    entrypoint: Path | None,
    format_: str,
) -> None:
    """Prepare an allowlisted context without executing worker source."""
    try:
        result = package_worker(manifest, output, wheel=wheel, requirements=requirements, entrypoint=entrypoint)
    except Exception:
        emit_result(
            error_result("WV-WORKER-INVALID", "Worker operation failed; check inputs and prerequisites."), format_
        )
        ctx.exit(EXIT_INVALID)
    click.echo(
        canonical_bytes(result).decode()
        if format_ == "json"
        else "Worker context prepared; build.json records exact inputs."
    )


@worker.command("deploy")
@click.option("--target", required=True, type=click.Choice(["compose"]))
@click.option("--image", required=True)
@click.option("--project", required=True)
@click.option("--context", required=True)
@click.option("--directory", required=True, type=click.Path(path_type=Path))
@click.option("--env-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "format_", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def deploy(
    ctx: click.Context,
    target: str,
    image: str,
    project: str,
    context: str,
    directory: Path,
    env_file: Path,
    format_: str,
) -> None:
    """Start one verified local image in a fresh, nonce-owned Compose service."""
    try:
        result = deploy_worker(directory, image=image, project=project, context=context, env_file=env_file)
    except Exception:
        emit_result(
            error_result("WV-WORKER-INVALID", "Worker operation failed; check inputs and prerequisites."), format_
        )
        ctx.exit(EXIT_INVALID)
    click.echo(
        canonical_bytes(result).decode()
        if format_ == "json"
        else f"Owned worker container started: {result['container_id']}. Stop with the returned JSON stop_argv."
    )
