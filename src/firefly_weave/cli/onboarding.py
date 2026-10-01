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

"""Local starter files and opt-in links to published documentation."""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path

import click

from firefly_weave.cli import EXIT_USAGE, LocalError, emit_result, error_result, export_files
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.values import JsonObject

DOCS_URL = "https://fireflyframework.github.io/firefly-weave/"
DOCS_TOPICS = {
    "quickstart": "quickstart/",
    "platform": "guides/platform-overview/",
    "cli": "reference/cli/",
    "workers": "guides/workers/",
    "deploy": "operations/deployment/",
    "configuration": "operations/configuration/",
}

_WORKFLOW = """# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
# Licensed under Apache-2.0: https://www.apache.org/licenses/LICENSE-2.0
apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: hello-weave
  version: 1.0.0
spec:
  inputSchema:
    type: object
    properties:
      message: {type: string}
    required: [message]
    additionalProperties: false
  outputSchema:
    type: object
    properties:
      message: {type: string}
    required: [message]
    additionalProperties: false
  steps: []
  output: {ref: /input}
"""

_README = r"""# Your first Firefly Weave workflow

This starter returns its input as output. It runs entirely offline: no platform,
worker, network, credentials, database, or Docker is needed. The empty pinned
catalog is sufficient because this workflow has no action dependencies.

From this directory, validate, compile, and simulate:

```sh
weave workflow validate workflow.yaml --catalog catalog.lock.json --strict
weave workflow compile workflow.yaml --catalog catalog.lock.json --strict --directory build
weave workflow simulate simulation.json
```

`simulation.json` initially contains the compiled starter and `input.json`.
After editing the workflow or input, recompile (use `--force` explicitly to
replace an existing build), then refresh the simulation request with Python 3:

```sh
weave workflow compile workflow.yaml --catalog catalog.lock.json --strict --directory build --force
python3 - <<'PYTHON'
import json
from pathlib import Path
request = {
    "artifact": json.loads(Path("build/compiled-artifact.json").read_text()),
    "input": json.loads(Path("input.json").read_text()),
    "mocks": {},
    "now": "2026-01-01T00:00:00Z",
}
Path("simulation.json").write_text(json.dumps(request, indent=2) + "\n")
PYTHON
weave workflow simulate simulation.json --output json
```

Simulation uses a fixed virtual clock and has no external effects. Workflows
with actions need a catalog containing their contracts and explicit simulation
mocks. `weave help workflow simulate` explains the request format.

Next steps:

- `weave docs quickstart`: learn the workflow lifecycle.
- `weave docs platform`: understand and start the platform services.
- `weave docs workers`: connect workers to an existing platform.
- `weave docs deploy`: deploy and operate the platform.

`weave worker deploy` deploys a worker; platform startup is a separate step.
`weave docs` prints a URL; add `--open` only when you want a browser.

Copyright 2026 Firefly Software Foundation. Author: Firefly Software Foundation.
Starter material is licensed under the Apache License, Version 2.0:
https://www.apache.org/licenses/LICENSE-2.0
"""


@click.command("init")
@click.argument("directory", type=click.Path(path_type=Path))
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def init(directory: Path, output: str) -> None:
    """Create an offline starter in DIRECTORY without overwriting files.

    Includes a workflow, pinned catalog, sample input, simulation, and README.
    No platform, worker, Docker, or network connection is needed.
    """
    catalog: JsonObject = {"definitions": [], "tasks": [], "adapters": [], "schemas": {}}
    sample: JsonObject = {"message": "Hello from Firefly Weave!"}
    compiled = compile_source(_WORKFLOW, format="yaml", catalog=CatalogSnapshot.empty(), strict=True)
    if not compiled.ok or compiled.artifact is None:
        emit_result(error_result("WV-CLI-INIT", "Cannot compile the bundled starter."), output)
        raise SystemExit(EXIT_USAGE)
    request: JsonObject = {
        "artifact": json.loads(compiled.artifact.to_bytes()),
        "input": sample,
        "mocks": {},
        "now": "2026-01-01T00:00:00Z",
    }
    files = {
        "workflow.yaml": _WORKFLOW.encode(),
        "catalog.lock.json": canonical_bytes(catalog) + b"\n",
        "input.json": canonical_bytes(sample) + b"\n",
        "simulation.json": canonical_bytes(request) + b"\n",
        "README.md": _README.encode(),
    }
    try:
        export_files(directory, files, force=False)
    except LocalError:
        emit_result(
            error_result(
                "WV-CLI-INIT", "Cannot create starter; use a writable directory with no starter file conflicts."
            ),
            output,
        )
        raise SystemExit(EXIT_USAGE) from None
    if output == "json":
        click.echo(canonical_bytes({"ok": True, "files": list(files)}).decode())
    else:
        click.echo("Created offline starter. From the destination directory, run:")
        click.echo("  weave workflow validate workflow.yaml --catalog catalog.lock.json --strict")
        click.echo("  weave workflow compile workflow.yaml --catalog catalog.lock.json --directory build")
        click.echo("  weave workflow simulate simulation.json")
        click.echo("See README.md to edit inputs and refresh the simulation. Explore: weave docs quickstart")


@click.command("docs")
@click.argument("topic", type=click.Choice(list(DOCS_TOPICS)), required=False)
@click.option("--open", "open_browser", is_flag=True, help="Also open the URL in your default browser.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def docs(topic: str | None, open_browser: bool, output: str) -> None:
    """Print published documentation links; opening a browser is opt-in.

    Use platform for service startup, workers for worker setup, and deploy for
    platform deployment and operations.
    """
    url = DOCS_URL + (DOCS_TOPICS[topic] if topic else "")
    if open_browser:
        try:
            if not webbrowser.open(url):
                raise webbrowser.Error("Browser unavailable")
        except (webbrowser.Error, OSError):
            emit_result(
                error_result("WV-CLI-DOCS", "Cannot open a browser; run docs without --open to get the URL."), output
            )
            raise SystemExit(EXIT_USAGE) from None
    if output == "json":
        click.echo(canonical_bytes({"url": url}).decode())
    else:
        click.echo(url)
