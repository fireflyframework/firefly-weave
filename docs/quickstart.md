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

# 1. Write and simulate your first workflow

In this chapter, you will create a workflow that receives a message and returns
that same message. You will see how Weave checks a definition and how to run it in
the local simulator. No API server, Docker, database, or credentials are needed.

**Result:** a successful simulation with `{"message": "Hello, Weave"}` as its
output. This is a local simulation; chapter 2 will create a durable run in PostgreSQL.

**Prefer a generated starting point?** `weave init my-first-workflow` creates a
working example with its own README. This chapter takes the hands-on route:
you write and explain each file, then reuse those files in the API tutorial.

![Typed message through input validation, transform, and output validation](diagrams/echo-data-flow.svg)

Follow the upper row to understand the YAML you will write. The lower row
distinguishes this local simulation from the durable execution in chapter 2.

[Open diagram at full size](diagrams/echo-data-flow.svg)

## Before you start

[Install the CLI](installation.md), then open a Bash or Zsh terminal in a directory
where you keep your work. The commands below create files beneath
`.local/tutorial/` in that directory. Stay in the same directory for this chapter.
You also need `python3` for a short script that assembles a JSON request.
On Windows, use WSL for these shell examples.

Check your tools:

```sh
weave --version
python3 --version
```

Expected: a Weave version and Python 3.12 or later. If `weave` is not found, follow
the installation guide's PATH instructions before continuing.

**Using a source checkout instead?** From its root, run `uv sync --locked --python 3.12`
and define this function once in the same terminal:

```sh
weave() { uv run --locked weave "$@"; }
```

Then use the same `weave` commands below. This function selects the checkout's
CLI; it lasts only for this shell. Installed-CLI users can skip it.

## See the steps before running them

| Step | What you create | How you recognize success |
| --- | --- | --- |
| Write | A YAML workflow | The file describes one input, one transform, and one output |
| Validate | A check result | `validationOk: true`, `ok: true`, and no errors |
| Compile | Three local artifact files | `ok: true` and `partial: false` |
| Simulate | One in-memory execution | `status: "succeeded"` with `Hello, Weave` in its output |

`workflow` is a command family. `validate`, `compile`, and `simulate` are operations
within it. Ask for help at either level:

```sh
weave help workflow
weave workflow compile --help
```

None of these steps publishes a workflow or starts a server. After this chapter,
you can [connect to an existing API](guides/connect-to-api.md)
or [start your own local installation](guides/standalone.md).

## Create the definition

A **workflow definition** describes input, ordered steps, and output. Save the
following file by copying the entire command into your terminal:

```sh
# Keep the tutorial files together so later commands can reuse them.
mkdir -p .local/tutorial
cat > .local/tutorial/echo.workflow.yaml <<'YAML'
apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: echo
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
  steps:
    - id: echo
      kind: transform
      value: {ref: /input}
  output: {ref: /steps/echo/output}
YAML
```

`.local/` holds your tutorial files. It is ignored by Git in the Weave checkout. Repeating the
command above replaces only this tutorial definition, so save personal edits
before repeating it.

Read the definition from top to bottom:

| Field | What it means in this example |
| --- | --- |
| `apiVersion` | The workflow language being used, not the installed package version |
| `kind` | This document defines a workflow |
| `metadata.name` and `version` | A name and immutable published version for the definition |
| `inputSchema` | The input must be an object with one string field named `message` |
| `outputSchema` | The final output must have that same shape |
| `steps` | One step, with the local ID `echo` |
| `kind: transform` | Compute a value inside Weave; no external worker is needed |
| `value: {ref: /input}` | Read the entire workflow input |
| `output: {ref: /steps/echo/output}` | Return the value produced by the `echo` step |

`ref` is a reference to workflow data, not Python code or a filesystem path.
`/input` means the input object; `/input/message` would mean just its message.
The latter is a string and would not satisfy the object output schema above.

## Validate the file

The compiler needs a **catalog**: the actions, connectors, schemas, and worker
contracts a workflow can use. Echo has no external dependencies, so create an
empty catalog once:

```sh
# This workflow calls no external actions, so an empty dependency catalog is complete.
cat > .local/tutorial/empty-catalog.json <<'JSON'
{"definitions": [], "tasks": [], "adapters": [], "schemas": {}}
JSON
weave workflow validate .local/tutorial/echo.workflow.yaml \
  --catalog .local/tutorial/empty-catalog.json --strict --output json
```

Expected: `validationOk: true`, `ok: true`, and `partial: false`. The definition
and its complete set of dependencies have passed the checks. If a diagnostic
reports a file, line, or path, correct that part of the YAML and repeat validation.

You may encounter examples that omit `--catalog`. Those perform a partial check
while a definition is being edited; `validationOk: true` together with
`partial: true` and `ok: false` is expected for that mode. In this tutorial, supply
the catalog so the result is complete.

## Compile it into an artifact

Compilation checks the complete definition and its dependencies, then produces an
**artifact**: the validated representation that the runtime or simulator consumes.

```sh
# Turn the checked definition into a reusable artifact for simulation or inspection.
weave workflow compile .local/tutorial/echo.workflow.yaml \
  --catalog .local/tutorial/empty-catalog.json --strict \
  --directory .local/tutorial/compiled --output json
```

Expected: `ok: true`, `partial: false`, and three files:

| File | Purpose |
| --- | --- |
| `.local/tutorial/compiled/compiled-artifact.json` | The compiled workflow with its identity and source information |
| `.local/tutorial/compiled/executable.json` | The executable part of the artifact |
| `.local/tutorial/compiled/catalog.lock.json` | The exact dependencies used for this compilation |

The digest identifies executable content; do not edit the generated artifact to
change behavior. Edit the YAML and compile again. The exporter refuses to replace
existing files by default. On an intentional repeat of this tutorial, add
`--force` to the compile command to replace these three local generated files.

## Simulate one execution

The simulator needs the compiled artifact, an input value, and a starting time.
The example has no integration tasks, so there are no external responses to mock.
Create its request:

```sh
# Bundle the compiled workflow and sample input into the simulator request.
python3 - <<'PYCODE'
import json
from pathlib import Path

root = Path('.local/tutorial')
request = {
    'artifact': json.loads((root / 'compiled/compiled-artifact.json').read_text()),
    'input': {'message': 'Hello, Weave'},
    'mocks': {},
    'now': '2026-01-01T00:00:00Z',
}
(root / 'simulation.json').write_text(json.dumps(request, indent=2) + '\n')
PYCODE
weave workflow simulate .local/tutorial/simulation.json --output json
```

Expected: `status: "succeeded"`. The JSON includes the accepted events and
variables for the run; `variables.output` is `{"message": "Hello, Weave"}`.
The fixed timestamp makes local simulations reproducible. It does not change your
computer's clock or schedule a live workflow.

Change `Hello, Weave` in the request-generation command and rerun it, then run
`weave workflow simulate` again. The output changes without changing the definition:
you are executing the same workflow with different input.

## Check what you learned

You now have a definition, a compiled artifact, and a successful simulated
execution. They are different things: the YAML describes behavior, the artifact
records the compiled behavior, and an execution applies it to one input.

| If you see… | What to check |
| --- | --- |
| `weave: command not found` | Reopen your terminal after installing, or follow the installation PATH steps; source users need the function above |
| `WV-CLI-READ` | Check the filename and your current directory |
| `partial: true` during validation | Expected without a catalog; use the compile command with the empty catalog |
| An export error on your second compilation | Use a new output directory, or `--force` for these tutorial-generated files |
| A schema/type diagnostic | Check the expected object/string shape and the diagnostic's file, line, and path |

**Choose your next step:**

- **Your team already runs Weave:** [connect the CLI to that API](guides/connect-to-api.md).
- **You need your own API:** [start the local platform](guides/standalone.md).
- **You want to practice authoring first:** use [the authoring lab](guides/workflow-authoring.md)
  to introduce a mistake, read its diagnostic, and repair it.

Keep this working directory. The CLI tutorial reuses `echo.workflow.yaml` and
`empty-catalog.json`; an existing-API client does not need a source checkout.
