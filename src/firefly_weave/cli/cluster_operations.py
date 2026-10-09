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

"""Guided local runner setup and canonical remote Operations commands."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import click

from firefly_weave.cli.remote import family, machine_result


@click.group("operations")
def operations() -> None:
    """Manage container destinations with reviewed plans and an outbound runner.

    Observe a target, set desired components, create a plan, review and approve
    its digest, then apply it. The runner keeps provider credentials locally.
    """


for name, prefix in (
    ("targets", "deployment_targets"),
    ("deployments", "deployments"),
    ("observations", "deployment_observations"),
    ("plans", "deployment_plans"),
    ("jobs", "deployment_jobs"),
    ("runners", "deployment_runners"),
):
    operations.add_command(family(name, prefix))


@operations.group("runner")
def runner() -> None:
    """Configure and run the Operations agent on a Linux or macOS destination host."""


@runner.command("setup")
@click.option(
    "--output", type=click.Path(path_type=Path), required=True, help="Create a new private runner configuration."
)
def setup(output: Path) -> None:
    """Walk through the destination, workspace, allowlist, and machine identity.

    Create a target in Studio first. Copy its ID and exact destination identity.
    This writes local configuration only; it does not sign in or change infrastructure.
    """
    from firefly_weave.deployment_runner.application import RunnerConfiguration

    if output.exists():
        raise click.ClickException("That file already exists. Choose a new path; existing configuration is preserved.")
    click.echo("1 of 4 — Choose the Weave workspace that owns this runner.")
    base_url = click.prompt("Weave API origin (https://...)")
    scope = {
        key: click.prompt(label, type=click.UUID)
        for key, label in (
            ("tenant_id", "Tenant ID"),
            ("project_id", "Project ID"),
            ("environment_id", "Environment ID"),
        )
    }
    click.echo("2 of 4 — Bind the runner to one explicitly registered destination.")
    target_id = click.prompt("Target ID from Studio", type=click.UUID)
    adapter = click.prompt(
        "Container adapter", type=click.Choice(["docker-compose", "kubernetes", "azure-container-apps"])
    )
    executable_name = {"docker-compose": "docker", "kubernetes": "kubectl", "azure-container-apps": "az"}[adapter]
    executable = click.prompt(
        "Absolute executable path", default=shutil.which(executable_name) or "/usr/local/bin/" + executable_name
    )
    context = click.prompt(
        "Azure subscription UUID" if adapter == "azure-container-apps" else "Explicit " + executable_name + " context"
    )
    boundary = click.prompt(
        {
            "docker-compose": "Compose project name",
            "kubernetes": "Kubernetes namespace",
            "azure-container-apps": "Azure resource group",
        }[adapter]
    )
    identity = click.prompt(
        {
            "docker-compose": "Pinned Docker daemon ID",
            "kubernetes": "Pinned namespace UID",
            "azure-container-apps": "Pinned Container Apps environment resource ID",
        }[adapter]
    )
    destination: dict[str, Any] = {
        "target_id": str(target_id),
        "adapter": adapter,
        "external_identity": identity,
        "boundary": boundary,
        "executable": executable,
        "context": context,
        "capabilities": ["observe"],
    }
    if adapter == "docker-compose":
        destination["compose_file"] = click.prompt("Absolute operator-owned Compose file")
    if adapter in {"docker-compose", "azure-container-apps"}:
        destination["lock_file"] = click.prompt("Absolute shared runner lock file")
    click.echo("3 of 4 — Allow only the components and image repositories you operate.")
    components: list[dict[str, str]] = []
    while len(components) < 100:
        component = {
            "name": click.prompt("Service or deployment name"),
            "kind": click.prompt(
                "Component role (lumi = Weave AI gateway)", type=click.Choice(["api", "worker", "lumi"])
            ),
            "configuration": click.prompt("Local configuration alias", default="current"),
        }
        if adapter in {"kubernetes", "azure-container-apps"}:
            component["container"] = click.prompt("Container name inside that deployment")
        components.append(component)
        if not click.confirm("Allow another component?", default=False):
            break
    destination["components"] = components
    destination["image_repositories"] = [
        item.strip()
        for item in click.prompt("Allowed image repositories (comma-separated; no tags or digests)").split(",")
    ]
    if click.confirm("Allow reviewed updates and worker scaling?", default=False):
        destination["capabilities"] += ["update", "scale_workers"]
        if adapter == "docker-compose" and click.confirm(
            "Allow deploying the trusted Compose template?", default=False
        ):
            destination["capabilities"].append("deploy")
    click.echo("4 of 4 — Use a dedicated runner identity, separate from your Studio login.")
    oauth_file = click.prompt("Absolute OAuth client-credentials configuration file")
    try:
        config = RunnerConfiguration.model_validate_json(
            json.dumps(
                {
                    "base_url": base_url,
                    "scope": {key: str(value) for key, value in scope.items()},
                    "destination": destination,
                    "oauth_file": oauth_file,
                }
            )
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(config.model_dump_json(indent=2) + "\n")
    except (ValueError, OSError):
        raise click.ClickException(
            "Configuration could not be saved. Check the destination IDs, absolute paths, and allowlist."
        ) from None
    click.echo("Configuration saved. Run 'weave operations runner check --config PATH' before starting the runner.")


@runner.command("check")
@click.option("--config", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
def check(config: Path) -> None:
    """Validate local policy and read the destination; do not register or apply jobs."""
    import asyncio

    from firefly_weave.deployment_runner.application import adapter_for, load_configuration

    try:
        settings = load_configuration(config)
        machine_result(asyncio.run(adapter_for(settings.destination).observe()))
    except Exception:
        raise click.ClickException(
            "Runner check failed. Inspect the local policy, provider access, and pinned destination identity."
        ) from None


@runner.command("run")
@click.option("--config", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--once", is_flag=True, help="Register, claim at most one job, and exit.")
def run(config: Path, once: bool) -> None:
    """Poll Weave over HTTPS and execute only leased, approved local operations."""
    import asyncio

    from firefly_weave.deployment_runner.application import load_configuration, serve

    try:
        settings = load_configuration(config)
        asyncio.run(serve(settings, once=once))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return
    except Exception:
        raise click.ClickException(
            "Runner stopped. Inspect identity, API access, destination policy, and the job receipt before restarting."
        ) from None
