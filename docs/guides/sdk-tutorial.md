<!--
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Use Weave from Python, one step at a time

You will create the same small workflow in YAML and Python, check it without a
server, and then use the Python client to publish, activate, and run it. The
workflow receives `{"message": "Hello from Python"}` and returns that object.

The first five steps run locally. Step 6 needs a running Weave API, a scoped
identity, and permission to publish and run definitions. You do not need a worker
for this example: a `transform` executes inside Weave.

## 1. Choose the Python tool for the job

| You want to… | Use | Where it runs |
| --- | --- | --- |
| Describe a workflow in a text file | YAML | Your editor; the compiler checks it |
| Generate a definition from Python | `WorkflowBuilder` and contract models | Your Python process, offline |
| Publish definitions or start/read runs | `WeaveClient` | Your application calls the API |
| Execute custom business logic | `Worker` with an async handler | A separately admitted worker process |
| Add a reusable integration adapter | A connector package | The operator's native executor |

`WorkflowBuilder` constructs data. Its methods do not call your business functions
while a workflow runs. You will see the actual Python worker boundary at the end.
The [SDK reference](../reference/sdk.md) lists the complete API.

## 2. Prepare an application environment

Use Python 3.12 or later. [Install Weave](../installation.md) in the environment
that will run these scripts; an isolated CLI installation does not make
`firefly_weave` importable by another Python interpreter. For remote API calls,
install the matching release wheel with its `client` extra.

For this source-checkout route, start in the repository root. Select a separate
environment for this lab so another checkout environment is not changed:

```sh
# Keep this lab's interpreter and dependencies together.
export UV_PROJECT_ENVIRONMENT="$PWD/.local/sdk-tutorial-venv"
uv sync --locked --python 3.12 --no-editable --extra client
mkdir -p .local/sdk-tutorial

# These shell functions reuse that interpreter for every command below.
python_sdk() { uv run --locked --no-editable --extra client python "$@"; }
weave_sdk() { uv run --locked --no-editable --extra client weave "$@"; }
python_sdk --version
```

Expected: Python 3.12 or later. Keep this terminal and repository directory for
the chapter. If using an already installed application environment, define
`python_sdk() { python "$@"; }` and `weave_sdk() { weave "$@"; }` there instead,
then create `.local/sdk-tutorial`. The two functions must select the same install.

## 3. Write the YAML version

Save this as `.local/sdk-tutorial/message.workflow.yaml`:

```yaml
apiVersion: weave/v1alpha1 # Select the definition language, not a package version.
kind: Workflow
metadata:
  name: sdk-message
  version: 1.0.0 # Published versions are immutable; change this when behavior changes.
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
  steps:
    - id: echo
      kind: transform # Copy data inside Weave; this needs no external worker.
      value: {ref: /input}
  output: {ref: /steps/echo/output}
```

The schemas describe the accepted input and final output. `required` requires the
message; `additionalProperties: false` catches accidental extra fields. `echo`
is a local step ID. `/input` reads the whole input object, and
`/steps/echo/output` reads that step's result. These are data references.

## 4. Build the equivalent definition in Python

Save this complete script as `.local/sdk-tutorial/build_message.py`:

```python
import json
from pathlib import Path

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import RefExpression, TransformStep
from firefly_weave.sdk.builder import WorkflowBuilder

root = Path(__file__).parent
message_schema = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}

# The builder returns a new value after each change, so retain that return value.
builder = WorkflowBuilder(
    "sdk-message", "1.0.0",
    input_schema=message_schema,
    output_schema=message_schema,
    output=RefExpression(ref="/steps/echo/output"),
).add_step(
    TransformStep(id="echo", kind="transform", value=RefExpression(ref="/input"))
)

# Both formats use the same compiler and the same explicit dependency catalog.
catalog = CatalogSnapshot.empty() # This workflow references no Actions or Connectors.
from_yaml = compile_source(
    (root / "message.workflow.yaml").read_text(), format="yaml", catalog=catalog,
)
from_python = compile_source(builder.to_document(), format="object", catalog=catalog)
for result in (from_yaml, from_python):
    if not result.ok or result.artifact is None:
        for diagnostic in result.diagnostics:
            print(diagnostic.code, diagnostic.path)
        raise SystemExit("Correct the definition before continuing")
assert from_yaml.artifact.digest == from_python.artifact.digest

# JSON is portable definition data; no Python callback is serialized.
(root / "message.workflow.json").write_text(json.dumps(builder.to_document(), indent=2) + "\n")
(root / "message.artifact.json").write_bytes(from_python.artifact.to_bytes())
print("YAML and Python compile to the same executable digest")
print(from_python.artifact.digest)
```

Run it:

```sh
# No server, token, or database is contacted by this script.
python_sdk .local/sdk-tutorial/build_message.py
```

Expected: `YAML and Python compile to the same executable digest`, followed by a
64-character digest. You also get `message.workflow.json` (definition data) and
`message.artifact.json` (compiled data). The builder supplies canonical field
names and validates model structure. Compilation still checks references,
dependencies, and semantic rules.

Try changing the builder's output reference to `/steps/missing/output` and run
again. A compiler diagnostic identifies the missing reference. Restore `echo`
before continuing. Successfully constructing a builder is not proof that the
workflow compiles.

## 5. Execute it in the local simulator

Save `.local/sdk-tutorial/make_simulation.py`:

```python
import json
from pathlib import Path

root = Path(__file__).parent
request = {
    "artifact": json.loads((root / "message.artifact.json").read_text()),
    "input": {"message": "Hello from Python"},
    "mocks": {}, # No task responses are needed for an internal transform.
    "now": "2026-01-01T00:00:00Z", # A fixed clock makes this simulation repeatable.
}
(root / "simulation.json").write_text(json.dumps(request, indent=2) + "\n")
```

```sh
python_sdk .local/sdk-tutorial/make_simulation.py
# The simulator applies one input to the compiled artifact.
weave_sdk workflow simulate .local/sdk-tutorial/simulation.json --output json
```

Expected: `status: "succeeded"`, with
`variables.output` equal to `{"message": "Hello from Python"}`. This is an
in-memory simulation. The next step creates a durable API run.

## 6. Connect your Python application to an API

For a team-operated API, complete [connect to an existing API](connect-to-api.md)
using its supplied-token option. For your own laptop, complete
[the local platform tutorial](local-platform.md) through `weave platform demo`;
use the private-file option below. A team-operated API uses these variables:

| Variable | Meaning |
| --- | --- |
| `WEAVE_BASE_URL` | Exact API origin, for example `https://weave.example.com`; no `/api/v1` suffix |
| `WEAVE_TENANT_ID` | Your tenant UUID |
| `WEAVE_PROJECT_ID` | The project UUID inside that tenant |
| `WEAVE_ENVIRONMENT_ID` | The environment UUID inside that project |
| `WEAVE_ACCESS_TOKEN` | A current access token issued for this API |

Remote origins use HTTPS; loopback HTTP is accepted for local development. Your
identity needs `catalog.read`, `compile`, `definition.publish`,
`release.activate`, `run.start`, and `run.read`. It cannot grant these to itself.
The SDK does not automatically read the CLI's saved login. For browser/device
credentials, use the [OAuth session integration](../reference/sdk.md#device-login-pkce-and-stores).

Save `.local/sdk-tutorial/run_message.py`. The complete script below uses the
supplied-token option. Local-platform users replace the scope/token block using
the small adaptation immediately after it.

```python
import asyncio
import os
from pathlib import Path

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.sdk.client import WeaveClient

scope = Scope.model_validate({
    "tenant_id": os.environ["WEAVE_TENANT_ID"],
    "project_id": os.environ["WEAVE_PROJECT_ID"],
    "environment_id": os.environ["WEAVE_ENVIRONMENT_ID"],
})


def access_token():
    # Read on each request so a host can replace credentials without rebuilding the client.
    # This callback does not acquire or refresh an expired token.
    return os.environ["WEAVE_ACCESS_TOKEN"]


async def main():
    source = (Path(__file__).parent / "message.workflow.yaml").read_text()
    async with WeaveClient(os.environ["WEAVE_BASE_URL"], access_token, scope) as client:
        # Check against this project's catalog before attempting publication.
        catalog = await client.catalog()
        checked = await client.compile(source=source, format="yaml", catalog=catalog, strict=True)
        if not checked.ok:
            for diagnostic in checked.diagnostics:
                print(diagnostic.code, diagnostic.path)
            raise SystemExit("The server rejected the definition")

        version = await client.publish(
            "workflows", source, "yaml", idempotency_key="sdk-message-publish-1",
        )
        activation = await client.activate(
            ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=scope),
            idempotency_key="sdk-message-activate-1",
        )
        run = await client.start_run(
            StartRunRequest(activation_id=activation.id, input={"message": "Hello from Python"}),
            idempotency_key="sdk-message-run-1",
        )
        print("activation_id:", activation.id)
        print("run_id:", run.id)

        # Start returns an admitted run; read its state to observe execution.
        for _ in range(20):
            current = await client.read_run(run.id)
            if current.state.status == "succeeded":
                print("output:", current.state.output)
                return
            if current.state.status in {"failed", "cancelled", "timed_out", "suspended"}:
                raise SystemExit("Inspect run history; state is " + current.state.status)
            await asyncio.sleep(0.5)
        raise SystemExit("Still running; inspect the printed run_id before starting another run")


if __name__ == "__main__":
    asyncio.run(main())
```

**Using the local platform instead?** In terminal 2, restore its nonsecret
session paths and refresh its private token:

```sh
# The API remains running in terminal 1; these commands reuse its installation.
source .local/platform/session.env
weave platform token
export WEAVE_BASE_URL="$WEAVE_API_URL"
```

If you chose a custom installation directory, source its `session.env` and pass
that same directory to `weave platform --directory /absolute/private/path token`.
In `run_message.py`, replace the `scope = ...` assignment and `access_token`
function with this block. Keep the rest of `main` unchanged:

```python
import json

# Reuse real scope IDs from platform demo; do not guess or copy example UUIDs.
platform = Path(os.environ["WEAVE_WORK_DIR"])
scope = Scope.model_validate(json.loads((platform / "first-run.json").read_text())["scope"])

def access_token():
    # Reread the private file so a later platform token command can rotate it.
    return json.loads((platform / "host-token.json").read_text())["access_token"]
```

The local option needs no `WEAVE_ACCESS_TOKEN` environment variable. The
callback reads credentials but does not refresh them itself. Now run the script:

```sh
# This step publishes, activates, and starts one durable run in your chosen scope.
python_sdk .local/sdk-tutorial/run_message.py
```

Expected: two UUIDs and `output: {'message': 'Hello from Python'}`. Keep the
activation ID if you want to try the inbound webhook in the
[custom connector tutorial](custom-connectors-tutorial.md#7-bring-events-in-with-a-signed-webhook).

Read the three mutations as separate decisions:

| Call | What you get | Why the next step needs it |
| --- | --- | --- |
| `publish` | Immutable definition version ID and digest | Activation identifies the exact compiled definition |
| `activate` | Environment activation ID | Starting a run selects a prepared environment binding |
| `start_run` | Run ID | Reads and history follow this particular execution |

The fixed idempotency keys let you retry the **same requests** without intentionally
creating a second execution. Use a new run key for a new input or intentional new
run. Changed source needs a new definition version and publication/activation
keys. If a request times out, its mutation may already have committed; retain its
key and inspect resource state before retrying.

## 7. Submit the Python-built version instead

`publish` accepts YAML or JSON text. To publish the definition generated in step
4, replace the `source` assignment in `run_message.py` with:

```python
# Serialize definition data; the API does not execute this application's Python.
source = (Path(__file__).parent / "message.workflow.json").read_text()
```

Change `format="yaml"` to `format="json"` in `compile` and the `"yaml"`
argument to `"json"` in `publish`. Use new publication and activation idempotency
keys because the request representation changed. The definitions have the same
executable digest. To create a separate named example, change the builder's name
and rebuild before publishing. Continue to select the returned IDs, not guessed
UUIDs or locally calculated version IDs.

## 8. Know where custom Python actually executes

When a step must run business code, publish an Action with a worker task contract
and admit a release implementing it. The complete [worker tutorial](workers.md)
uses `example-record@1.0.0`. Its handler returns an object containing `receipt`
and `customer`. This small handler illustrates the return shape without making
an external request:

```python
async def record(lease):
    # lease.input has already passed the admitted task's input schema.
    return {"receipt": "accepted", "customer": lease.input["customer"]}
```

After authentication and worker registration, that handler is registered by exact
capability name:

```python
from firefly_weave.sdk.worker import Worker

# transport must be the authenticated WorkerTransport for an admitted instance.
worker = Worker(transport, {"example-record@1.0.0": record}, concurrency=1)
await worker.run()
```

This last block is a wiring excerpt, not a standalone program. Use the complete
[worker example](../../examples/worker/main.py) for credentials, registration,
shutdown, and external effects. That example passes `lease.operation_key` to the
receiver for deduplication. A workflow Action names the task contract; it never
contains a Python import path. The SDK manages claims, heartbeat renewal, and
completion around the handler.

## Troubleshoot one boundary at a time

| Symptom | What to check |
| --- | --- |
| `ModuleNotFoundError` | Run the script with the interpreter containing Weave and the `client` extra |
| A missing environment variable | Restore all five variables from step 6 in this terminal |
| Compilation reports a missing Action | Fetch the correct project catalog; publish/admit dependencies before compiling |
| HTTP 401 | Token expiration, issuer, and audience; this example does not refresh tokens |
| HTTP 403 | The identity's current grants in these three scope IDs |
| Idempotency conflict | A prior request used that key with a different body |
| Polling ends without success | Read the printed run ID and history; a receipt is not completion |
| You need to call another system | Continue with [custom connectors](custom-connectors-tutorial.md) or [workers](workers.md) |

The build and simulation steps can be verified offline. Publication, activation,
and worker registration require a live, authorized installation; passing the
local examples does not establish that your installation has the necessary grants.
