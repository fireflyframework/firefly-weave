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
// What the repository's own Studio host answers to Execute workflow, for
// workflows whose transactions are undone, finish or end in doubt.
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import type { Workflow } from "../src/app/model";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import { python, repository } from "./python-path";

const EXECUTE = `
import json, sys, yaml
from pathlib import Path
from firefly_weave.studio.local_contracts import ExecuteRequest
from firefly_weave.studio.simulation import execute
catalog = yaml.safe_load(Path(sys.argv[1]).read_text())
out = {}
for name, request in json.load(sys.stdin.buffer).items():
    body = {"format": "json", "definitions": catalog["definitions"], "catalog": "none",
            "now": "2026-11-24T10:00:00Z", "target": {"mode": "workflow"}, **request}
    answer = execute(ExecuteRequest.model_validate_json(json.dumps(body)), None)
    out[name] = answer.model_dump(mode="json", by_alias=True, exclude_unset=True)
print(json.dumps(out))
`;
const QUANTITY = {
  object: {
    quantity: { ref: "/input/quantity" },
    reference: { ref: "/input/moveId" },
  },
};
const REFERENCE = { object: { reference: { ref: "/input/moveId" } } };
export const hold = {
  id: "hold",
  kind: "action",
  uses: "source-system.hold-quantity@1.0.0",
  with: QUANTITY,
  confirm: {
    kind: "action",
    uses: "source-system.confirm-hold@1.0.0",
    with: { object: { holdId: { ref: "/steps/hold/output/holdId" } } },
  },
  cancel: {
    kind: "action",
    uses: "source-system.release-hold@1.0.0",
    with: {
      object: {
        reference: { ref: "/input/moveId" },
        holdOperation: { ref: "/participant/operationKey" },
      },
    },
  },
};
export const add = {
  id: "add",
  kind: "action",
  uses: "target-system.add-quantity@1.0.0",
  with: QUANTITY,
  compensate: {
    kind: "action",
    uses: "target-system.remove-quantity@1.0.0",
    with: {
      object: {
        reference: { ref: "/input/moveId" },
        addOperation: { ref: "/participant/operationKey" },
      },
    },
  },
};
export const submit = {
  id: "submit",
  kind: "action",
  uses: "orders.submit-order@1.0.0",
  with: REFERENCE,
  irreversible: true,
};
const tell = {
  id: "tell",
  kind: "action",
  uses: "notify.send-message@1.0.0",
  with: { object: { text: { ref: "/input/moveId" } } },
};
export const build = (id: string) => ({
  id,
  kind: "transform",
  value: { object: { done: { literal: true } } },
});
export const move = (steps: object[], extra: object = {}) => ({
  id: "move",
  kind: "transaction",
  businessKey: { ref: "/input/moveId" },
  steps,
  output: { literal: null },
  ...extra,
});
export const each = (...steps: object[]) => ({
  id: "each",
  kind: "forEach",
  items: { ref: "/input/items" },
  maxItems: 5,
  concurrency: 1,
  body: { steps, output: { literal: null } },
});
export const workflow = (...steps: object[]) =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "move-between-systems", version: "1.4.0" },
    spec: {
      inputSchema: {
        type: "object",
        properties: {
          moveId: { type: "string" },
          quantity: { type: "integer" },
          items: { type: "array", items: { type: "integer" } },
        },
        required: ["moveId", "quantity"],
      },
      outputSchema: {},
      steps,
      output: { literal: null },
    },
  }) as unknown as Workflow;

/** A workflow, what its steps return and which of them fail. */
export interface Execution {
  workflow: Workflow;
  errors?: Record<string, object>;
  target?: object;
}
const MOCKS = {
  hold: { holdId: "h-1" },
  add: { id: "a-1" },
  submit: { orderId: "o-1" },
};
/** The host's answer to each execution. A step that fails at every key returns nothing. */
export function executed<N extends string>(
  executions: Record<N, Execution>,
): Record<N, ExecuteResponse> {
  const requests = Object.entries<Execution>(executions).map(
    ([name, { workflow, errors = {}, target }]) => [
      name,
      {
        source: JSON.stringify(workflow),
        input: { moveId: "M-1", quantity: 2, items: [1, 2, 3] },
        mocks: Object.fromEntries(
          Object.entries(MOCKS)
            .filter(
              ([id]) =>
                !(`node:${id}` in errors) &&
                JSON.stringify(workflow).includes(`"id":"${id}"`),
            )
            .map(([id, output]) => [`node:${id}`, output]),
        ),
        errors,
        ...(target ? { target } : {}),
      },
    ],
  );
  return JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        EXECUTE,
        join(repository, "tests/fixtures/transactions/catalog.yaml"),
      ],
      {
        cwd: repository,
        input: JSON.stringify(Object.fromEntries(requests)),
        encoding: "utf8",
        timeout: 180_000,
        maxBuffer: 16 * 1024 * 1024,
      },
    ),
  ) as Record<N, ExecuteResponse>;
}

const REFUSED = { code: "REFUSED", effect: "none" };
const STUCK = { code: "UNAVAILABLE", attempts: 10 };
const MOVE = workflow(move([hold, add, submit]));
const CONTINUED = workflow(
  move([hold, add, submit, build("notify")], { onError: "continue" }),
  build("summarize"),
);
/** Each way a transaction of a simulated run ends. */
export const EXECUTIONS = {
  undone: { workflow: MOVE, errors: { "node:submit": REFUSED } },
  stuck: {
    workflow: MOVE,
    errors: { "node:submit": REFUSED, "node:add#undo": STUCK },
  },
  unknown: {
    workflow: MOVE,
    errors: { "node:submit": { code: "REFUSED", outcome: "unknown" } },
  },
  confirmStuck: { workflow: MOVE, errors: { "node:hold#confirm": STUCK } },
  completed: {
    workflow: workflow(
      build("lookup"),
      move([hold, add, submit, build("notify")]),
      build("summarize"),
    ),
  },
  definite: {
    workflow: MOVE,
    errors: { "node:add": { code: "VALIDATION", effect: "none" } },
  },
  continued: { workflow: CONTINUED, errors: { "node:submit": REFUSED } },
  inLoop: {
    workflow: workflow(
      each(move([hold, add, submit], { onError: "continue" })),
    ),
    errors: { "node:submit[1]": REFUSED },
  },
  loopStopped: {
    workflow: workflow(each(move([hold, add, submit]))),
    errors: { "node:submit[1]": REFUSED },
  },
  // A loop inside the transaction: each item is undone on its own.
  loopInside: {
    workflow: workflow(move([each(add), submit])),
    errors: { "node:submit": REFUSED },
  },
  loopOnly: { workflow: workflow(move([each(add)])) },
  // add may have changed something: it is undone although it failed.
  mayHave: {
    workflow: MOVE,
    errors: { "node:add": { code: "BUSY", outcome: "unknown" } },
  },
  // A step with nothing to undo fails; the transaction continues.
  buildContinued: {
    workflow: workflow(
      move([hold, build("check"), submit], { onError: "continue" }),
      build("summarize"),
    ),
    errors: { "node:check": { code: "NOT_ALLOWED" } },
  },
  nothingDone: {
    workflow: workflow(move([build("check"), submit])),
    errors: { "node:check": { code: "NOT_ALLOWED" } },
  },
  // The run goes on past the transaction, and stops later.
  failsLater: {
    workflow: workflow(
      move([hold, add, submit], { onError: "continue" }),
      tell,
    ),
    errors: { "node:submit": REFUSED, "node:tell": { code: "BUSY" } },
  },
  stopped: { workflow: MOVE, target: { mode: "step", stepId: "add" } },
  nothingReserved: { workflow: workflow(move([add, submit])) },
  buildFails: {
    workflow: workflow(move([hold, build("check"), submit])),
    errors: { "node:check": { code: "NOT_ALLOWED" } },
  },
} satisfies Record<string, Execution>;
