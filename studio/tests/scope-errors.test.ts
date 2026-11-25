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
// What a field can read around a step that handles its failures, offline,
// as the compiler's scope_at says it in the shared scope cases: a step's
// error after it (it may be missing) and its output after Continue (it may
// be empty), and inside its error path the error first and always there,
// with no output of its own. Loop roots come from the Studio host and are
// left out here.
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { locateDiagnostic } from "../src/app/designer/diagnostic-location";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";
import { referenceScope, visibleRefs } from "../src/app/forms/core/scope";
import {
  referenceHint,
  referenceView,
} from "../src/app/forms/ui/reference-combobox";

interface Entry {
  ref: string;
  source: string;
  stepId?: string;
  label: string;
  breadcrumb: string;
  types: string[];
  typeLabel: string;
  optional: boolean;
}
const { cases } = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/language/scope.json"),
    "utf8",
  ),
) as {
  cases: {
    name: string;
    document: unknown;
    path: string;
    result: { found: boolean; entries: Entry[] };
  }[];
};
/** The cases about steps that handle their failures. */
const handled = cases.filter(({ document }) =>
  /"onError"/.test(JSON.stringify(document)),
);
const shown = ({
  ref,
  source,
  stepId,
  label,
  breadcrumb,
  types,
  typeLabel,
  optional,
}: Entry) => ({
  ref,
  source,
  stepId,
  label,
  breadcrumb,
  types,
  typeLabel,
  optional,
});

describe("error fields in the data picker, as the compiler offers them", () => {
  for (const { name, document, path, result } of handled)
    it(name, () => {
      const at = locateDiagnostic(path, document);
      expect(at.exact).toBe(true);
      const scope = referenceScope(document, at.stepId, at.fieldPath);
      expect(scope.found).toBe(true);
      expect(scope.entries.map((entry) => shown(entry as Entry))).toEqual(
        result.entries
          .filter(
            (entry) => entry.source !== "item" && entry.source !== "index",
          )
          .map(shown),
      );
    });

  it("groups a failed step's error as Error of the step in the data picker, before the steps", () => {
    const inside = handled.find(({ name }) =>
      name.startsWith("a step inside an error path"),
    )!.document;
    const entries = visibleRefs(inside, "tell", "/value");
    const view = referenceView(entries, "");
    expect(
      view.groups.map(({ label, kind }) => ({ label, kind: kind ?? "" })),
    ).toEqual([
      // Plain values before groups and lists, as the picker always ranks them.
      { label: "Error of total", kind: "warning" },
      { label: "note", kind: "transform" },
      { label: "Workflow input", kind: "" },
      { label: "lookup", kind: "transform" },
    ]);
    expect(view.groups[0].tone).toBe("danger");
    expect(view.groups[0].options.map(({ option }) => option.label)).toEqual([
      "code",
      "message",
      "outcome",
      "attempts",
      "instanceKey",
      "childRunId",
      // The whole error object, a group, after its fields.
      "Error",
    ]);
    // A reference to the error reads as one of the suggestions.
    expect(referenceHint("/steps/total/error/code", entries)).toEqual({
      tone: "info",
      message: "Text",
    });
  });

  it("keeps the failed step's error in what the Studio host says a field in a loop body reads", async () => {
    const looped = handled.find(({ name }) =>
      name.startsWith("an error path inside a loop body"),
    )!;
    const model = new StructuredCanvasAdapter();
    model.definition = structuredClone(looped.document) as Workflow;
    const host = {
      model,
      editingLocked: false,
      profile: null,
      catalogContracts: new Map(),
      decisionContracts: new Map(),
      actionVersions: [],
      diagnostics: null,
      diagnosticsDefinition: null,
      label: (kind: string) => kind,
      can: () => true,
      notify: () => undefined,
      refreshView: () => undefined,
      perform: (edit: () => void) => edit(),
      api: { request: async () => looped.result },
    } as unknown as StepDetailsHost;
    const session = new FormSession(
      host,
      new StepDetailsController(host),
      openRequest("flag"),
      { confirm: async () => true, announce: () => undefined },
    );
    const spec = { id: "value", label: "Value", path: ["value"] } as ParamSpec;
    session.completionScope(spec);
    await new Promise((settle) => setTimeout(settle, 0));
    expect(
      session.completionScope(spec).entries.map((entry) => entry.ref),
    ).toEqual([
      "/input",
      "/input/invoices",
      "/input/batches",
      "/steps/check/error",
      "/steps/check/error/code",
      "/steps/check/error/message",
      "/steps/check/error/outcome",
      "/steps/check/error/attempts",
      "/steps/check/error/instanceKey",
      "/steps/check/error/childRunId",
    ]);
  });
});
