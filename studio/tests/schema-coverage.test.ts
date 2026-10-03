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
import "@angular/compiler";
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { propertyFields, supportedOperators } from "../src/app/property-grid";
import { createStep, freshWorkflow, kinds } from "../src/app/model";

const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
// CI installs the repository environment. Frontend-only contributors can still
// run UI tests; this cross-language contract check explicitly reports a skip.
const available = existsSync(python);
const schema = available
  ? JSON.parse(
      execFileSync(
        python,
        [
          "-c",
          `
import json
from typing import get_args
from firefly_weave.contracts import definitions as d
models = [d.ActionStep, d.TransformStep, d.SwitchStep, d.ParallelStep,
          d.WaitStep, d.SignalStep, d.HumanTaskStep, d.FailStep,
          d.DecisionTableStep, d.LLMStep]
def fields(model):
    return [f.alias or name for name, f in model.model_fields.items()]
print(json.dumps({
    "steps": {get_args(m.model_fields["kind"].annotation)[0]: fields(m) for m in models},
    "workflow": fields(d.WorkflowSpec), "metadata": fields(d.Metadata),
    "case": fields(d.SwitchCase), "branch": fields(d.Branch),
    "operators": list(get_args(d.OperatorName.__value__)),
}))
`,
        ],
        { cwd: root, encoding: "utf8", timeout: 180_000 },
      ),
    )
  : null;

describe.skipIf(!available)(
  "editor coverage of the real Python contracts",
  () => {
    it("covers every step kind and configuration field", () => {
      expect([...kinds].sort()).toEqual(Object.keys(schema.steps).sort());
      for (const kind of kinds) {
        const fields = new Set(
          propertyFields(createStep(kind, "step")).map((f) => f.path[0]),
        );
        // Identity is displayed separately; branch steps are managed on canvas.
        fields.add("id");
        fields.add("kind");
        expect([...fields].sort(), kind).toEqual(schema.steps[kind].sort());
      }
    });
    it("covers workflow configuration and metadata without hiding fields in source mode", () => {
      const fields = propertyFields(freshWorkflow());
      const spec = fields
        .filter((f) => f.path[0] === "spec")
        .map((f) => f.path[1]);
      // Canvas owns steps, ConnectionSlotList the slots, and LlmInspector profiles.
      expect([...spec, "steps", "connections", "llmProfiles"].sort()).toEqual(
        schema.workflow.sort(),
      );
      const metadata = fields
        .filter((f) => f.path[0] === "metadata")
        .map((f) => f.path[1]);
      expect(metadata.sort()).toEqual(schema.metadata.sort());
    });
    it("covers branch configuration and every supported expression operation", () => {
      const decision = propertyFields(createStep("switch", "decision"));
      const caseFields = decision
        .filter((f) => f.path[0] === "cases")
        .map((f) => f.path[2]);
      expect([...caseFields, "steps"].sort()).toEqual(schema.case.sort());
      const branch = propertyFields(createStep("parallel", "parallel"));
      const branchFields = branch
        .filter((f) => f.path[0] === "branches" && f.path[1] === "first")
        .map((f) => f.path[2]);
      expect([...branchFields, "steps"].sort()).toEqual(schema.branch.sort());
      expect([...supportedOperators].sort()).toEqual(schema.operators.sort());
    });
  },
);
