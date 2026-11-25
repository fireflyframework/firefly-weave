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
import { describe, expect, it } from "vitest";
import { propertyFields, supportedOperators } from "../src/app/property-grid";
import { createStep, freshWorkflow, kinds, type Step } from "../src/app/model";
import {
  onErrorFields,
  onErrorKinds,
} from "../src/app/editor/ndv/settings/on-error";
import { undoFields } from "../src/app/editor/transactions/undo-fields";
import { writeUndo } from "../src/app/editor/transactions/undo";
import {
  pendingStepFields,
  readyKinds,
  type LanguageManifest,
} from "../src/app/editor/language-manifest";
import { allParams } from "../src/app/editor/ndv/form-contract";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { workflowSettingsForm } from "../src/app/editor/ndv/kinds/forms/workflow-forms";
import { ndvRegistry, type KindContext } from "../src/app/editor/ndv/registry";
import { loadLanguageManifest } from "./support/language-manifest";

const root = resolve(import.meta.dirname, "../..");

/** The language manifest's entries; Studio compares only `ready` ones. */
interface Marked {
  studio: "ready" | "pending";
  feature?: string;
}
interface Manifest {
  step_kinds: (Marked & { kind: string })[];
  operators: (Marked & { name: string })[];
  workflow_fields: (Marked & { name: string })[];
}
const ready = <T extends Marked>(entries: T[]) =>
  entries.filter((entry) => entry.studio === "ready");

// CI installs the repository environment. Frontend-only contributors can still
// run UI tests; this cross-language contract check explicitly reports a skip.
const available = pythonAvailable();
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
from firefly_weave.contracts.language import language_manifest
models = get_args(get_args(d.Step.__value__)[0])
def fields(model):
    return [f.alias or name for name, f in model.model_fields.items()]
print(json.dumps({
    "steps": {get_args(m.model_fields["kind"].annotation)[0]: fields(m) for m in models},
    "workflow": fields(d.WorkflowSpec), "metadata": fields(d.Metadata),
    "case": fields(d.SwitchCase), "branch": fields(d.Branch),
    "operators": list(get_args(d.OperatorName.__value__)),
    "manifest": language_manifest().model_dump(mode="json"),
}))
`,
        ],
        { cwd: root, encoding: "utf8", timeout: 180_000 },
      ),
    )
  : null;
const manifest = schema?.manifest as Manifest;
/**
 * On error and Result on error: step details' Settings adds them to every
 * kind the language lets choose what its failure does, after its own fields.
 */
const onErrorPaths = (step: Step, ctx: KindContext): string[] =>
  onErrorFields(step, {
    ...ctx,
    features: ["flow.onError"],
    onErrorKinds: onErrorKinds(manifest),
  })
    .filter((spec) => !spec.readOnly)
    .map((spec) => String(spec.path[0]));

/**
 * How it's undone: step details' Settings adds it to every kind the language
 * lets declare it, inside a transaction. Each way shows its own fields.
 */
const undoPaths = (step: Step, ctx: KindContext): string[] =>
  (["reserve", "compensate", "irreversible"] as const).flatMap((way) => {
    const chosen = structuredClone(step) as Record<string, unknown>;
    for (const { path, value } of writeUndo(step, way).patch)
      chosen[String(path[0])] = value;
    return undoFields(chosen as unknown as Step, {
      ...ctx,
      features: ["flow.transaction"],
      manifest: manifest as unknown as LanguageManifest,
      inTransaction: createStep("transform", "move"),
      sideEffect: "non_idempotent",
    }).map((spec) => String(spec.path[0]));
  });

describe.skipIf(!available)(
  "editor coverage of the real Python contracts",
  () => {
    it("lists every Python step kind, operator and workflow field in the language manifest", () => {
      expect(manifest.step_kinds.map((entry) => entry.kind).sort()).toEqual(
        Object.keys(schema.steps).sort(),
      );
      expect(manifest.operators.map((entry) => entry.name).sort()).toEqual(
        [...schema.operators].sort(),
      );
      expect(
        manifest.workflow_fields.map((entry) => entry.name).sort(),
      ).toEqual([...schema.workflow].sort());
    });
    it("covers every ready step kind and its configuration fields", async () => {
      // Kinds the palette doesn't list come with their own descriptor; a kind
      // still marked pending may have one already.
      await loadKindRegistrations();
      const own = ndvRegistry
        .kinds()
        .filter((d) => !(kinds as readonly string[]).includes(d.kind))
        .filter((d) =>
          ready(manifest.step_kinds).some((entry) => entry.kind === d.kind),
        );
      expect(own.map((d) => d.kind).sort()).toEqual([
        "callWorkflow",
        "transaction",
      ]);
      expect([...kinds, ...own.map((d) => d.kind)].sort()).toEqual(
        ready(manifest.step_kinds)
          .map((entry) => entry.kind)
          .sort(),
      );
      // A call shows its input once its workflow is chosen.
      const configured: Record<string, object> = {
        callWorkflow: { uses: "notify-customer@1.0.0" },
      };
      for (const descriptor of own) {
        const step = {
          ...descriptor.create("step"),
          ...configured[descriptor.kind],
        };
        const workflow = freshWorkflow();
        workflow.spec.steps.push(step);
        const ctx: KindContext = {
          workflow,
          features: [],
          actionContract: () => null,
          tableContract: () => null,
          workflowContract: () => null,
        };
        const fields = new Set([
          ...[
            ...allParams(descriptor.form?.(step, ctx), step, ctx),
            ...allParams(descriptor.settings?.(step, ctx), step, ctx),
          ].map(({ spec }) => String(spec.path[0])),
          ...onErrorPaths(step, ctx),
          ...undoPaths(step, ctx),
          // The canvas edits a kind's own steps (a transaction's).
          ...(descriptor.containers?.(step) ?? []).map(({ path }) =>
            String(path[0]),
          ),
        ]);
        fields.add("id");
        fields.add("kind");
        const pending = new Set(
          pendingStepFields(
            manifest as unknown as LanguageManifest,
            descriptor.kind,
          ),
        );
        expect([...fields].sort(), descriptor.kind).toEqual(
          (schema.steps[descriptor.kind] as string[])
            .filter((name) => !pending.has(name))
            .sort(),
        );
      }
      for (const kind of kinds) {
        const step = createStep(kind, "step");
        const workflow = freshWorkflow();
        workflow.spec.steps.push(step);
        const ctx: KindContext = {
          workflow,
          features: [],
          actionContract: () => null,
          tableContract: () => null,
          workflowContract: () => null,
        };
        const fields = new Set([
          ...propertyFields(step).map((f) => f.path[0]),
          ...onErrorPaths(step, ctx),
          ...undoPaths(step, ctx),
        ]);
        // Identity is displayed separately; branch steps are managed on canvas.
        fields.add("id");
        fields.add("kind");
        // Settings Studio does not edit yet (pending step fields) are left to source mode.
        const pending = new Set(
          pendingStepFields(manifest as unknown as LanguageManifest, kind),
        );
        expect([...fields].sort(), kind).toEqual(
          (schema.steps[kind] as string[])
            .filter((name) => !pending.has(name))
            .sort(),
        );
      }
    });
    it("edits every field of a loop in step details once loops are ready", async () => {
      expect(ready(manifest.step_kinds).map((entry) => entry.kind)).toContain(
        "forEach",
      );
      await loadKindRegistrations();
      const loop = ndvRegistry.kind("forEach")!;
      const step = createStep("forEach", "loop");
      const workflow = freshWorkflow();
      const form = loop.form!(step, {
        workflow,
        features: ["flow.forEach"],
        actionContract: () => null,
        tableContract: () => null,
        workflowContract: () => null,
      });
      // Items and the options by their first key; the body by its container.
      const edited = new Set(
        [
          ...form.fields,
          ...(form.options ?? []),
          ...(loop.containers?.(step) ?? []),
        ].map((part) => part.path[0]),
      );
      edited.add("id");
      edited.add("kind");
      expect([...edited].sort()).toEqual(
        (schema.steps["forEach"] as string[]).sort(),
      );
    });
    it("covers ready workflow configuration and metadata without hiding fields in source mode", () => {
      const fields = propertyFields(freshWorkflow());
      const spec = fields
        .filter((f) => f.path[0] === "spec")
        .map((f) => f.path[1]);
      // The workflow settings in step details add what the property grid lacks (callable).
      const settings = [
        ...workflowSettingsForm().fields,
        ...(workflowSettingsForm().options ?? []),
      ]
        .filter((f) => f.scope === "workflow" && f.path[0] === "spec")
        .map((f) => String(f.path[1]));
      // Canvas owns steps, ConnectionSlotList the slots, and LlmInspector profiles.
      expect(
        [
          ...new Set([
            ...spec,
            ...settings,
            "steps",
            "connections",
            "llmProfiles",
          ]),
        ].sort(),
      ).toEqual(
        ready(manifest.workflow_fields)
          .map((entry) => entry.name)
          .sort(),
      );
      const metadata = fields
        .filter((f) => f.path[0] === "metadata")
        .map((f) => f.path[1]);
      expect(metadata.sort()).toEqual(schema.metadata.sort());
    });
    it("covers branch configuration and every ready expression operation", () => {
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
      expect([...supportedOperators].sort()).toEqual(
        ready(manifest.operators)
          .map((entry) => entry.name)
          .sort(),
      );
    });
  },
);

// Step details need a descriptor for every kind the language marks ready;
// kinds still marked pending may lack one.
const registryManifest = loadLanguageManifest();
describe.skipIf(!registryManifest)(
  "step details registry against the language manifest",
  () => {
    it("has a descriptor for every kind marked ready", async () => {
      await loadKindRegistrations();
      const registered = new Set(ndvRegistry.kinds().map((d) => d.kind));
      expect(
        readyKinds(registryManifest!).filter((kind) => !registered.has(kind)),
      ).toEqual([]);
    });
    it("registers only kinds the manifest lists, with their features", async () => {
      await loadKindRegistrations();
      const listed = new Map(
        registryManifest!.step_kinds.map((entry) => [entry.kind, entry]),
      );
      for (const descriptor of ndvRegistry.kinds()) {
        expect(listed.has(descriptor.kind), descriptor.kind).toBe(true);
        expect(descriptor.feature ?? null, descriptor.kind).toBe(
          listed.get(descriptor.kind)?.feature ?? null,
        );
      }
    });
  },
);
