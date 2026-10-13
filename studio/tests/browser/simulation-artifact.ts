/*
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
*/
// The simulated workflow the tests open, and the pieces of a simulation they
// answer with: the compiled artifact of a small workflow that calls one
// action, built by the repository's own compiler once per worker, and the
// debug session the host replies with at each point of the run.
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { repository } from "../python-path";
import { hasPython, python } from "./local-authoring";

export const simulatedSource = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: simulated
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [customerId]
    properties:
      customerId: { type: string, minLength: 1 }
  outputSchema: { type: object }
  steps:
    - id: action-1
      kind: action
      uses: onboarding.check-customer@1.0.0
      with:
        object:
          customerId: { ref: /input/customerId }
    - id: wait-1
      kind: wait
      durationSeconds: 60
    - id: approval
      kind: signal
      name: customer-approved
      timeoutSeconds: 3600
      payloadSchema:
        type: object
        additionalProperties: false
        required: [approved]
        properties:
          approved: { type: boolean }
    - id: review
      kind: humanTask
      assignment: reviewers
      title: { literal: Review the customer }
      context: { literal: {} }
      decisions: [approve, reject]
      formSchema: { type: object, properties: { note: { type: string } } }
  output: { literal: {} }
`;

let compiled: Record<string, unknown> | null | undefined;
/** The compiled artifact of `simulatedSource`; null without the repository's Python. */
export function simulationArtifact(): Record<string, unknown> | null {
  if (compiled !== undefined) return compiled;
  compiled = hasPython
    ? JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            `
import json, pathlib, sys
import yaml
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
action = yaml.safe_load(pathlib.Path("examples/definitions/check-customer.action.yaml").read_text())
spec = action["spec"]
catalog = CatalogSnapshot.from_definitions([load_definition(action)], tasks=[{
    "taskType": "onboarding.check-customer", "taskVersion": "1.0.0",
    "inputSchema": spec["inputSchema"], "outputSchema": spec["outputSchema"],
    "sideEffect": "read_only", "timeoutSeconds": 60}])
result = compile_source(sys.stdin.read(), format="yaml", catalog=catalog)
assert result.ok, [d.code for d in result.diagnostics]
print(result.artifact.to_bytes().decode())
`,
          ],
          {
            cwd: repository,
            env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
            encoding: "utf8",
            input: simulatedSource,
            timeout: 30000,
          },
        ),
      )
    : null;
  return compiled ?? null;
}

const now = "2026-10-02T09:00:00.000Z";
/** The simulated clock, `seconds` after the run started. */
export const later = (seconds: number) =>
  new Date(Date.parse(now) + seconds * 1000).toISOString();

/** The host's reply at one point of a simulation: where it is, what it finished and what it waits for. */
export const debugSession = (
  revision: number,
  status: string,
  current: string[],
  finished: string[] = [],
  waits: Record<string, string> = {},
) => ({
  id: "44444444-4444-4444-8444-444444444444",
  revision,
  view: {
    status,
    current_nodes: current,
    active_nodes: current,
    // The steps the run finished, so the canvas draws the live thread.
    variables: {
      waits,
      steps: Object.fromEntries(
        finished.map((id) => [id, { output: { eligible: true } }]),
      ),
    },
    diagnostics: [],
    events: [],
    now,
  },
});
