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
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { python, pythonAvailable } from "./python-path";
import { parse } from "yaml";
import { describe, expect, it } from "vitest";
import { StructuredCanvasAdapter, type Step } from "../src/app/model";
import {
  PLACEHOLDER_ACTION,
  workflowTemplates,
} from "../src/app/templates/catalog";
import { loadWorkflowTemplates } from "../src/app/templates/template-gallery";
import { stepKindLabels } from "../src/app/designer/step-picker";

const root = resolve(import.meta.dirname, "../..");
const available = pythonAvailable();

/** Step kinds in document order, descending into cases, default and branches. */
function stepKinds(steps: Step[]): string[] {
  return steps.flatMap((step) => {
    const nested: Step[][] = [];
    if (step.kind === "switch") {
      for (const c of step["cases"] as { steps: Step[] }[])
        nested.push(c.steps);
      nested.push((step["default"] as { steps: Step[] }).steps);
    }
    if (step.kind === "parallel")
      for (const b of Object.values(
        step["branches"] as Record<string, { steps: Step[] }>,
      ))
        nested.push(b.steps);
    return [step.kind, ...nested.flatMap(stepKinds)];
  });
}

describe("workflow template catalog", () => {
  it("offers five templates with unique identities", () => {
    expect(workflowTemplates).toHaveLength(5);
    const ids = workflowTemplates.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    const names = workflowTemplates.map(
      (t) => (parse(t.yaml) as { metadata: { name: string } }).metadata.name,
    );
    expect(new Set(names).size).toBe(names.length);
    for (const name of names) expect(name).toMatch(/^[a-z][a-z0-9-]*$/);
    for (const t of workflowTemplates) {
      expect(t.title.trim()).not.toBe("");
      expect(t.description.length).toBeGreaterThan(20);
      expect(t.description).not.toMatch(/WV-/);
    }
  });

  it("summarizes the step kinds each template really contains", () => {
    for (const t of workflowTemplates) {
      const definition = parse(t.yaml) as { spec: { steps: Step[] } };
      expect(t.kinds, t.id).toEqual(stepKinds(definition.spec.steps));
      expect(t.placeholder, t.id).toBe(t.yaml.includes(PLACEHOLDER_ACTION));
    }
  });

  it("opens every template in the visual designer without errors", () => {
    for (const t of workflowTemplates) {
      const model = new StructuredCanvasAdapter();
      model.setSource(t.yaml, "yaml");
      expect(model.error, t.id).toBe("");
      expect(model.readonly, t.id).toBe(false);
      expect(model.nodes().map((n) => n.step.kind)).toEqual(
        expect.arrayContaining(t.kinds),
      );
      expect(model.nodes()).toHaveLength(t.kinds.length);
    }
  });

  it("loads the same documents through the gallery's lazy import", async () => {
    expect(await loadWorkflowTemplates()).toBe(workflowTemplates);
    for (const t of workflowTemplates)
      for (const kind of t.kinds) expect(stepKindLabels[kind]).toBeTruthy();
  });

  it("tells the author about every action a new project may not have", () => {
    const uses = (steps: Step[]): string[] =>
      steps.flatMap((step) => [
        ...(typeof step["uses"] === "string" ? [step["uses"]] : []),
        ...(step.kind === "switch"
          ? [
              ...(step["cases"] as { steps: Step[] }[]).flatMap((c) =>
                uses(c.steps),
              ),
              ...uses((step["default"] as { steps: Step[] }).steps),
            ]
          : []),
        ...(step.kind === "parallel"
          ? Object.values(
              step["branches"] as Record<string, { steps: Step[] }>,
            ).flatMap((b) => uses(b.steps))
          : []),
      ]);
    for (const t of workflowTemplates) {
      const definition = parse(t.yaml) as { spec: { steps: Step[] } };
      const real = uses(definition.spec.steps).filter(
        (reference) => reference !== PLACEHOLDER_ACTION,
      );
      // A real reference only compiles once that action is published.
      if (real.length) expect(t.note, t.id).toMatch(/action/);
      if (t.note) expect(t.note, t.id).not.toMatch(/WV-|@/);
    }
  });

  it("never embeds secrets, hosts or credentials", () => {
    for (const t of workflowTemplates) {
      expect(t.yaml, t.id).not.toMatch(/https?:\/\//i);
      expect(t.yaml, t.id).not.toMatch(/secret|password|token|apiKey/i);
    }
  });
});

// The Python compiler is the authority. Authoring validation must report no
// errors, and once each placeholder is swapped for a compatible stand-in
// action the template must compile completely.
const results = available
  ? (JSON.parse(
      execFileSync(
        python,
        [
          "-c",
          `
import json, pathlib, sys
import yaml
from firefly_weave.compiler import api
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

templates = json.loads(sys.stdin.read())
validate = getattr(api, "validate_authoring", None)
validator = "validate_authoring" if validate else "validate_source"
validate = validate or api.validate_source

manifest = HTTP_PROFILE_DESCRIPTOR.manifest.value
def action(implementation, input_schema, output_schema, **extra):
    spec = {"implementation": implementation, "sideEffect": "read_only", "inputSchema": input_schema,
            "outputSchema": output_schema, **extra}
    return {"apiVersion": "weave/v1alpha1", "kind": "Action",
            "metadata": {"name": "your-action", "version": "1.0.0"}, "spec": spec}
http_action = action(
    {"kind": "connector", "uses": "weave-http@2.0.0", "action": "read",
     "config": {"profileVersion": "2.0.0", "method": "GET", "path": "/records/{id}", "sideEffect": "read_only",
                "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
                "statuses": [200], "emptyStatuses": []}},
    {"type": "object", "additionalProperties": False, "required": ["path"],
     "properties": {"path": {"type": "object", "additionalProperties": False, "required": ["id"],
                             "properties": {"id": {"type": "string"}}}}},
    {"type": "object", "required": ["status", "body"],
     "properties": {"status": {"const": 200}, "body": {"type": "object"}}},
    timeoutSeconds=30, connection={"connector": "weave-http@2.0.0"})
worker_action = action({"kind": "worker", "taskType": "your-action", "taskVersion": "1.0.0"},
                       {"type": "object"}, {"type": "object"}, timeoutSeconds=60)
def task(name, definition):
    spec = definition["spec"]
    return {"taskType": name, "taskVersion": "1.0.0", "inputSchema": spec["inputSchema"],
            "outputSchema": spec["outputSchema"], "sideEffect": "read_only", "timeoutSeconds": 60}
sample = yaml.safe_load(pathlib.Path("examples/definitions/check-customer.action.yaml").read_text())
sample_task = task("onboarding.check-customer", sample)
http = CatalogSnapshot.from_definitions(
    [load_definition(manifest), load_definition(http_action), load_definition(sample)],
    tasks=[sample_task], adapters=[manifest["spec"]["adapter"]])
worker = CatalogSnapshot.from_definitions(
    [load_definition(worker_action), load_definition(sample)],
    tasks=[task("your-action", worker_action), sample_task])
validators = {HTTP_PROFILE_DESCRIPTOR.manifest.digest: HTTP_PROFILE_DESCRIPTOR.validate_action_config}

def issues(result):
    return [{"code": d.code, "severity": d.severity, "path": d.path} for d in result.diagnostics]

out = {"validator": validator, "templates": {}}
for t in templates:
    authoring = validate(t["yaml"], format="yaml")
    catalog = http if "weave-http@2.0.0" in t["yaml"] else worker
    complete = api.compile_source(t["yaml"], format="yaml", catalog=catalog, action_validators=validators)
    out["templates"][t["id"]] = {
        "authoringOk": authoring.validation_ok, "authoring": issues(authoring),
        "completeOk": complete.ok, "complete": issues(complete)}
print(json.dumps(out))
`,
        ],
        {
          cwd: root,
          encoding: "utf8",
          timeout: 180_000,
          env: { ...process.env, PYTHONPATH: resolve(root, "src") },
          input: JSON.stringify(
            workflowTemplates.map(({ id, yaml }) => ({ id, yaml })),
          ),
        },
      ),
    ) as {
      validator: string;
      templates: Record<
        string,
        {
          authoringOk: boolean;
          authoring: { code: string; severity: string; path: string }[];
          completeOk: boolean;
          complete: { code: string; severity: string; path: string }[];
        }
      >;
    })
  : null;

describe.skipIf(!available)("templates against the Python compiler", () => {
  it.each(workflowTemplates.map((t) => [t.id]))(
    "%s passes authoring validation with no errors",
    (id) => {
      const result = results!.templates[id];
      expect(
        result.authoring.filter((d) => d.severity === "error"),
        `${id} (${results!.validator})`,
      ).toEqual([]);
      expect(result.authoringOk).toBe(true);
    },
  );
  it.each(workflowTemplates.map((t) => [t.id]))(
    "%s compiles completely once its actions resolve",
    (id) => {
      const result = results!.templates[id];
      expect(
        result.complete.filter((d) => d.severity === "error"),
        id,
      ).toEqual([]);
      expect(result.completeOk).toBe(true);
    },
  );
});
