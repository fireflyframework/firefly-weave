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
// What the Studio host checks a workflow's test data against: the workflow,
// the Action documents it calls (published contracts and the actions it
// owns, both in the editor's contract cache), and, for when the host can't
// answer, the schema each entry belongs to as Studio knows it.
import { instanceStep } from "./instance-keys";
import { allSteps } from "../ndv/step-walk";
import type { Json } from "../ndv/registry";
import type { Workflow } from "../../model";
import type { CheckInputs } from "./test-data-check";

const unescape = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");

export function checkInputsOf(
  workflow: Workflow,
  contracts: ReadonlyMap<string, Record<string, unknown>>,
): CheckInputs {
  return {
    source: JSON.stringify(workflow),
    definitions: [...contracts.values()] as Json[],
    schemaOf: (pointer) => {
      if (pointer === "/input")
        return workflow.spec["inputSchema"] as Json | undefined;
      const pin = /^\/pins\/([^/]+)$/.exec(pointer);
      if (!pin) return undefined;
      const id = instanceStep(unescape(pin[1]!));
      const step = allSteps(workflow.spec.steps).find((item) => item.id === id);
      if (step?.kind !== "action") return undefined;
      const spec = contracts.get(String(step["uses"] ?? ""))?.["spec"] as
        | Record<string, unknown>
        | undefined;
      return spec?.["outputSchema"] as Json | undefined;
    },
  };
}
