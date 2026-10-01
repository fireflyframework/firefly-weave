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

**Goal:** turn a checked workflow into a picture you can read in a browser, share
in a review, or include in project documentation. This is an offline operation:
it never calls a connector or changes a saved workflow.

## 1. Create a small example

Use an installed CLI. From a directory where you keep your projects:

```sh
# Create a new folder with a valid workflow and its dependency catalog.
weave init graph-example
cd graph-example
```

This starter simply returns its input. It is deliberately small so you can learn
the commands before drawing a larger process.

## 2. Compile the definition

```sh
# Resolve the workflow against the catalog before drawing its actual compiled flow.
weave workflow compile workflow.yaml --catalog catalog.lock.json --strict --directory build
```

Expected: compilation passes and creates `build/compiled-artifact.json`.
The catalog lists the actions and other contracts available to the workflow.
The starter needs none, so its empty catalog is complete.

When you edit YAML or build the definition with the Python SDK, compile again.
The graph reads the compiled artifact, so it represents that version of the
workflow. It does not silently recompile a changed source file.

## 3. Read the flow in your terminal

```sh
# Print every node and its possible outgoing paths without opening a browser.
weave workflow graph build/compiled-artifact.json
```

The starter shows a start node pointing to an end node. A real workflow also
shows actions, transformations, waits, choices, parallel branches, and joins.
The arrow labels explain which path leads to the next node.

## 4. Export a picture

```sh
# Save a standalone SVG that works in a browser without scripts or internet access.
weave workflow graph build/compiled-artifact.json --format svg --directory diagrams
```

Open `diagrams/workflow.svg` in your browser or editor. Read from top to bottom.
Green endpoints mark input and result; amber boxes mark branch selection or
joining; white boxes describe individual steps. Hover over a box for its full
compiled ID and source path. Branch labels show the possible routes.

The export refuses to overwrite existing files. After intentionally recompiling,
add `--force` to replace the previous diagram. For a separate revision, use a new
output directory instead.

## 5. Use Mermaid in another documentation tool

```sh
# Export text for tools that render Mermaid diagrams, such as GitHub Markdown.
weave workflow graph build/compiled-artifact.json --format mermaid --directory mermaid
```

Copy `mermaid/workflow.mmd` into a Mermaid code block in the destination tool.
Rendering depends on that tool; the Weave export itself does not load a remote
renderer. For scripts, add `--output json` to receive the format, content, and
exported filename as one JSON object.

## What the graph tells you

The graph shows **possible control flow**, including compiler-generated branch
result and join nodes. A join means a selected branch, or all required parallel
branches, must finish before continuing. A failure node stops that path; it does
not automatically reconnect to the next visible box. The compiler may retain
unreachable source tails: those boxes have no executable incoming path.

This is not a live execution dashboard. To see which path a particular run
actually took, read its [history](../reference/history-and-replay.md). The graph
omits input values, expressions, and credentials; it includes workflow and step
names, so review those names before sharing the diagram.

SVG layout supports up to 200 nodes and 400 edges. Larger artifacts can use text
or Mermaid. The importer checks the artifact's format, graph, and content digest;
an edited or unsupported artifact is rejected. Compile the source again instead
of repairing compiled JSON by hand.

Continue with the [Python SDK tutorial](sdk-tutorial.md) or
[custom connectors](custom-connectors-tutorial.md) to build a more useful workflow.
