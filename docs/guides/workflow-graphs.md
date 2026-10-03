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

# See your workflow as a graph

Turn a workflow file into a picture you can read in the terminal, open in a
browser, share in a review, or paste into project documentation. This page is
for anyone who reviews a workflow: process designers, developers, and approvers.
It takes about 10 minutes. You need only the [installed `weave` CLI](../installation.md):
no platform, account, or network connection. Drawing a graph never calls a
connector and never changes a saved workflow.

**Already drawing in Studio?** Studio's **Designer** tab shows the same flow
while you edit, and its **Outline** tab lists it as text; see the
[Studio guide](studio.md). Use the commands on this page when you start from a
YAML or JSON file, for example in a code review or a build pipeline.

**How it works.** The `weave workflow graph` command reads a *compiled
artifact*: the checked, versioned form of a workflow that the compiler produces.
So you always draw in two steps: compile the file, then draw the artifact. You
can draw it as text, as a standalone SVG image, or as Mermaid text.

## 1. Create a small example

A starter project gives you a valid workflow to practice on.

```sh
# Create a new folder with a valid workflow and its dependency catalog.
weave init graph-example
# Work inside the new folder for the rest of this page.
cd graph-example
```

Expected: `Created offline starter. From the destination directory, run:`
followed by suggested commands. The folder holds `workflow.yaml`,
`catalog.lock.json`, `input.json`, `simulation.json`, and a `README.md`. This
starter simply returns its input, so you can learn the commands before you draw
a larger process.

## 2. Compile the definition

The graph shows what the compiler accepted, so compile first. The *catalog*
lists the published actions and other contracts the workflow may use; the
starter uses none, so its empty catalog is complete.

```sh
# Check the workflow against its catalog and write the compiled artifact to build/.
weave workflow compile workflow.yaml --catalog catalog.lock.json --strict --directory build
```

Expected: `Compilation passed:` followed by the artifact's digest, and a new
`build/compiled-artifact.json`. If the command reports errors instead, fix them
in `workflow.yaml` and compile again; the [quickstart](../quickstart.md)
explains how to read compiler diagnostics.

**Compile again after every change.** The graph reads the compiled artifact,
not your source file, so it always shows the version you last compiled.

## 3. Read the flow in your terminal

Text output is the quickest check and works over SSH.

```sh
# Print every node and its possible outgoing paths without opening a browser.
weave workflow graph build/compiled-artifact.json
```

Expected:

```text
hello-weave @ 1.0.0
Static flow (arrows show possible paths, not live execution)

[@start] Start with input
  +-- next --> [@end]
[@end] Return the result
```

Each line in brackets is a node: its ID, then what it does. The indented lines
are the arrows that leave it, with their label and target. Nodes whose IDs start
with `@` are added by the compiler: `@start` and `@end` here, and branch and
join nodes in larger workflows.

## 4. Export a picture

An SVG file opens in any browser, works without scripts or internet access, and
can be attached to a review.

```sh
# Save a standalone SVG image of the compiled flow in the diagrams/ folder.
weave workflow graph build/compiled-artifact.json --format svg --directory diagrams
```

Expected: `Saved workflow.svg. Open it to explore the compiled flow; no actions
were executed.`

Open `diagrams/workflow.svg` in your browser and read it from top to bottom:

- **Green boxes** are the start (your input) and the end (the result).
- **Amber boxes** choose a branch, run branches, or join them again.
- **White boxes** are individual steps, plus the compiler's branch-result nodes.
- **Arrow labels** name each possible route, such as `next` or `case: case:0`.
- **The number in each box** is its position in the compiled artifact, not the
  order in which steps run. Follow the arrows for the order.
- **Hover over a box** to see its full ID, what it does, and its source path.

**The export never overwrites a file.** When `diagrams/workflow.svg` already
exists, the command stops with `WV-CLI-EXPORT`. After you recompile on purpose,
add `--force` to replace the previous picture, or choose a new `--directory` to
keep both.

## 5. Export Mermaid for another documentation tool

Many documentation tools, including GitHub Markdown, render Mermaid text as a
diagram.

```sh
# Save Mermaid text in the mermaid/ folder.
weave workflow graph build/compiled-artifact.json --format mermaid --directory mermaid
```

Expected: `Saved workflow.mmd. Open it to explore the compiled flow; no actions
were executed.` Copy the contents of `mermaid/workflow.mmd` into a Mermaid code
block in the destination tool. How it looks depends on that tool; the Weave
export itself loads no remote renderer.

**For scripts, add `--output json`.** The command then prints one JSON object
with `ok`, `format`, `content` (the drawing itself), and `files` (the names it
saved, empty without `--directory`).

## 6. Draw a workflow with a decision

The starter has no branches. This short workflow sends urgent requests straight
through and makes the others wait one minute, so you can see how decisions and
joins are drawn. Save it as `routing.yaml` in the same folder:

```yaml
# A Decision (switch) with one case and a default; the default branch waits 60 seconds.
apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: urgent-routing
  version: 1.0.0
spec:
  inputSchema:
    type: object
    properties:
      urgent: {type: boolean}
    required: [urgent]
    additionalProperties: false
  outputSchema:
    type: object
  steps:
    - id: route
      kind: switch
      cases:
        - when:
            op:
              name: eq
              args:
                - {ref: /input/urgent}
                - {literal: true}
          steps: []
          output: {literal: {lane: fast}}
      default:
        steps:
          - id: cool-down
            kind: wait
            durationSeconds: 60
        output: {literal: {lane: normal}}
  output: {ref: /steps/route/output}
```

```sh
# Compile the new workflow into its own folder, so the starter's artifact stays as it is.
weave workflow compile routing.yaml --catalog catalog.lock.json --strict --directory build-routing
# Print the graph of the new artifact.
weave workflow graph build-routing/compiled-artifact.json
```

Expected: `Compilation passed:` with the new artifact's digest, then this graph:

```text
urgent-routing @ 1.0.0
Static flow (arrows show possible paths, not live execution)

[@start] Start with input
  +-- next --> [route]
[@end] Return the result
[@branch:route:0] Collect branch result
  +-- join: case:0 --> [@join:route]
[cool-down] Wait for a duration
  +-- next --> [@branch:route:1]
[@branch:route:1] Collect branch result
  +-- join: default --> [@join:route]
[@join:route] Continue after branches
  +-- next --> [@end]
[route] Choose a branch
  +-- case: case:0 --> [@branch:route:0]
  +-- default: default --> [cool-down]
```

Follow the arrows from `@start`: `route` chooses a branch. The first case
(`case:0`) has no steps, so it goes straight to its branch result. The default
runs `cool-down` first. Both branch results then meet at `@join:route`, which
continues to `@end`. The lines are listed in the artifact's order, not in run
order. Export it with `--format svg` as in step 4 to see the same routes as
boxes and arrows.

## What the graph tells you

**It shows every possible path, not what happened.** The graph is the static
control flow of one compiled version. To see which path a particular run took,
read its [history](../reference/history-and-replay.md) or open the run in
Studio's **Runs** view.

- **Branch-result and join nodes come from the compiler.** A join means that
  the selected branch of a Decision, or every branch of a Parallel, must finish
  before the workflow continues.
- **A failure ends its path.** A **Fail** step does not connect to the next
  visible box.
- **Unreachable boxes can appear.** The compiler keeps steps that no path
  reaches, such as steps after a Fail; they have no incoming arrow.
- **What is left out.** The graph omits input values, expressions, and
  credentials. It does include the workflow name and every step ID, so review
  those names before you share the picture.

**SVG is limited to 200 nodes and 400 arrows.** For a larger workflow, use the
text or Mermaid format.

**The artifact must be untouched.** The command re-checks the artifact's format,
graph, and content hashes, so an edited or unsupported file is refused. Compile
the source again instead of repairing compiled JSON by hand.

## If something goes wrong

| What you see | Why | What to do |
| --- | --- | --- |
| `WV-CLI-READ: Cannot read the requested local input file.` | The artifact path is wrong, or you have not compiled yet | Run `weave workflow compile` and pass the `compiled-artifact.json` it wrote |
| `WV-CLI-GRAPH: Use a valid compiled Workflow artifact; SVG supports up to 200 nodes.` | You passed a source file instead of an artifact, the artifact was edited, or the SVG would be too large | Compile the source again; for a very large workflow, use `--format text` or `--format mermaid` |
| `WV-CLI-EXPORT: Export targets already exist or are unsafe; use --force for files.` | The output file already exists | Add `--force` to replace it, or choose another `--directory` |
| `Validation or local operation failed; no executable produced.` from `weave workflow compile` | The workflow or its catalog has errors | Read the diagnostics under the message, fix the source, and compile again |

## Next steps

- [Write and simulate your first workflow](../quickstart.md): define inputs,
  validate, compile, and simulate one run.
- [Draw the same workflows in Studio](studio.md): a visual editor that checks
  your workflow as you type.
- [Build a workflow with the Python SDK](sdk-tutorial.md) or
  [write a custom connector](custom-connectors-tutorial.md), then draw the
  result with these commands.
