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

"""Lazy authenticated operator commands; offline imports never load the server."""

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click


def open_request(request: Any, timeout: int) -> Any:
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
            return None

    return build_opener(NoRedirect()).open(request, timeout=timeout)


def invoke(environment_url: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> None:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request

    token = os.environ.get("WEAVE_ACCESS_TOKEN")
    parsed = urlsplit(environment_url)
    if not token or parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        raise click.ClickException("Configure an environment URL and WEAVE_ACCESS_TOKEN")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise click.ClickException("TLS is required outside local development")
    request = Request(
        environment_url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", **(headers or {})},
    )
    try:
        with open_request(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            code = json.load(error).get("code", "WV-HTTP-ERROR")
        except (ValueError, AttributeError):
            code = "WV-HTTP-ERROR"
        click.echo(json.dumps({"code": code, "status": error.code}))
        raise click.exceptions.Exit(1) from None
    except (URLError, OSError, ValueError):
        raise click.ClickException("Operator request failed") from None
    click.echo(json.dumps(result, sort_keys=True))


def environment_option(function: Any) -> Any:
    return click.option("--environment-url", envvar="WEAVE_ENVIRONMENT_URL", required=True)(function)


@click.group()
def run() -> None:
    """Cancel an execution or create a linked new run."""


@run.command("cancel")
@click.argument("run_id", type=click.UUID)
@environment_option
@click.option("--reason", required=True)
def cancel(run_id: Any, environment_url: str, reason: str) -> None:
    """Stop scheduling; already issued external effects may continue."""
    invoke(environment_url, f"/runs/{run_id}/cancel", {"reason": reason})


@run.command("retry")
@click.argument("run_id", type=click.UUID)
@environment_option
@click.option("--request", "request_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--idempotency-key", required=True)
def retry(run_id: Any, environment_url: str, request_path: Path, idempotency_key: str) -> None:
    """Start from a JSON StartRunRequest, preserving the terminal parent."""
    invoke(environment_url, f"/runs/{run_id}/retry", read_json(request_path), {"Idempotency-Key": idempotency_key})


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise click.ClickException("Request file must contain valid JSON") from None


@click.group()
def incident() -> None:
    """Inspect and resolve scoped incidents."""


@incident.command("list")
@click.argument("run_id", type=click.UUID)
@environment_option
def list_incidents(run_id: Any, environment_url: str) -> None:
    invoke(environment_url, f"/runs/{run_id}/incidents")


@incident.command("resolve")
@click.argument("incident_id", type=click.UUID)
@environment_option
@click.option("--revision", type=click.IntRange(min=1), required=True)
@click.option("--request", "request_path", type=click.Path(exists=True, path_type=Path), required=True)
def resolve(incident_id: Any, environment_url: str, revision: int, request_path: Path) -> None:
    """Submit a JSON IncidentResolution with a stable receipt_id."""
    invoke(environment_url, f"/incidents/{incident_id}/resolve", read_json(request_path), {"If-Match": str(revision)})


@run.command("history")
@click.argument("run_id", type=click.UUID)
@environment_option
@click.option("--cursor")
@click.option("--limit", type=click.IntRange(1, 100), default=100)
def history(run_id: Any, environment_url: str, cursor: str | None, limit: int) -> None:
    """Read one stable page of authorized accepted facts."""
    from urllib.parse import urlencode

    query = {"limit": str(limit)}
    if cursor is not None:
        query["cursor"] = cursor
    invoke(environment_url, f"/runs/{run_id}/history?" + urlencode(query))


@run.command("export")
@click.argument("run_id", type=click.UUID)
@environment_option
@click.option("--limit", type=click.IntRange(1, 1000), default=1000)
def export_history(run_id: Any, environment_url: str, limit: int) -> None:
    """Export safe facts, original source references and completeness metadata."""
    invoke(environment_url, f"/runs/{run_id}/export?limit={limit}")


@run.command("replay")
@click.option("--artifact", "artifact_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--events", "events_path", type=click.Path(exists=True, path_type=Path), required=True)
def recorded_replay(artifact_path: Path, events_path: Path) -> None:
    """Replay a local history export (or event array) with its pinned artifact, offline."""
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.operations.replay import MAX_BYTES, MAX_EVENTS, replay
    from firefly_weave.runtime.models import RuntimeEvent

    try:
        if artifact_path.stat().st_size + events_path.stat().st_size > MAX_BYTES:
            raise ValueError
        artifact = import_artifact(artifact_path.read_bytes())
        raw = read_json(events_path)
        events = raw["events"] if isinstance(raw, dict) else raw
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise ValueError
        report = replay(artifact, tuple(RuntimeEvent.model_validate_json(json.dumps(e)) for e in events))
    except (OSError, ValueError, KeyError, RecursionError):
        raise click.ClickException("WV-REPLAY-INPUT: bounded artifact and recorded events required") from None
    click.echo(report.model_dump_json())
    if report.status != "consistent":
        raise click.exceptions.Exit(1)
