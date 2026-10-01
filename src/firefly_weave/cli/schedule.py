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

"""Authenticated schedule commands, importable without server dependencies."""

from pathlib import Path
from typing import Any

import click

from firefly_weave.cli.operations import environment_option, invoke, read_json


@click.group()
def schedule() -> None:
    """Manage UTC schedules and inspect occurrence history."""


@schedule.command("save")
@environment_option
@click.option("--request", "request_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--revision", type=click.IntRange(min=1))
def save(environment_url: str, request_path: Path, revision: int | None) -> None:
    invoke(environment_url, "/schedules", read_json(request_path), {"If-Match": str(revision)} if revision else {})


@schedule.command("list")
@environment_option
@click.option("--after", type=click.UUID)
def list_schedules(environment_url: str, after: Any) -> None:
    invoke(environment_url, "/schedules" + (f"?after={after}" if after else ""))


@schedule.command("show")
@click.argument("identifier", type=click.UUID)
@environment_option
def show(identifier: Any, environment_url: str) -> None:
    invoke(environment_url, f"/schedules/{identifier}")


@schedule.command("occurrences")
@click.argument("identifier", type=click.UUID)
@environment_option
@click.option("--after", type=click.IntRange(min=0), default=0)
def occurrences(identifier: Any, environment_url: str, after: int) -> None:
    invoke(environment_url, f"/schedules/{identifier}/occurrences?after={after}")


def mutation(name: str) -> None:
    @schedule.command(name)
    @click.argument("identifier", type=click.UUID)
    @environment_option
    @click.option("--revision", type=click.IntRange(min=1), required=True)
    def command(identifier: Any, environment_url: str, revision: int) -> None:
        invoke(environment_url, f"/schedules/{identifier}/{name}", {}, {"If-Match": str(revision)})


for name in ("enable", "disable", "delete"):
    mutation(name)
