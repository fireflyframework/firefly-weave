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
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
import { stringify } from "yaml";
import { App } from "../src/app/app";
import { StructuredCanvasAdapter } from "../src/app/model";
afterAll(() => vi.unstubAllGlobals());

const source = (kind: string) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-intake", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      steps: [{ id: "check-customer", kind, value: { ref: "/input" } }],
      output: { literal: {} },
    },
  });
const canvasFor = (stepId: string) =>
  JSON.stringify({
    schemaVersion: 2,
    kind: "weave.studio/canvas",
    notes: [
      {
        id: "note-1a2b3c4d",
        text: "Reviewed",
        color: "blue",
        x: 40,
        y: 260,
        width: 240,
        height: 120,
        anchor: { stepId, dx: 0, dy: 168 },
      },
    ],
    stepNotes: { [stepId]: { text: "Uses the CRM sandbox" } },
  });

/** The shell with a workflow opened from `text`, reading a canvas file the way Import does. */
function opened(text: string) {
  const model = new StructuredCanvasAdapter();
  model.setSource(text, "yaml");
  const said: string[] = [];
  const shell = Object.create(App.prototype) as App;
  Object.assign(shell, {
    model,
    notify: (line: string) => void said.push(line),
  });
  const read = (file: string) =>
    (
      shell as unknown as { applyCanvasFile(text: string): void }
    ).applyCanvasFile(file);
  return { model, said, read };
}

describe("a canvas file opened with its workflow", () => {
  it("says which notes it keeps when it mentions steps the workflow doesn't have", () => {
    const { model, said, read } = opened(source("transform"));
    read(canvasFor("other-step"));
    expect(said).toEqual([
      "The canvas file mentions steps this workflow doesn't have. Their step notes were left out, and notes anchored to them stay on the canvas without an anchor.",
    ]);
    expect(model.canvas.stepNotes).toBeUndefined();
    expect(model.canvas.notes?.[0].anchor).toBeUndefined();
  });

  it("keeps its anchors and step notes while the workflow source doesn't read, until it is fixed", () => {
    const { model, said, read } = opened(source("not-a-kind"));
    expect(model.readonly).toBe(true);
    read(canvasFor("check-customer"));
    expect(said).toEqual([]);
    model.setSource(source("transform"), "yaml");
    expect(model.readonly).toBe(false);
    expect(model.canvas.stepNotes).toEqual({
      "check-customer": { text: "Uses the CRM sandbox" },
    });
    expect(model.canvas.notes?.[0].anchor?.stepId).toBe("check-customer");
  });
});

describe("importing a workflow with a canvas file Studio can't read", () => {
  it("opens the workflow and says it opened without its canvas file", async () => {
    const said: string[] = [];
    const shell = Object.create(App.prototype) as App;
    Object.assign(shell, {
      view: "home",
      importGeneration: 0,
      openedFromFiles: new Set<string>(),
      notify: (line: string) => void said.push(line),
      flushLocalSave: () => {},
      openTab: () => {},
      resetValidation: () => {},
      changed: () => {},
      scheduleFit: () => {},
      router: { navigateByUrl: async () => true },
      cdr: { markForCheck: () => {} },
    });
    const workflow = {
      name: "order-intake.yaml",
      size: 200,
      text: async () => source("transform"),
    } as unknown as File;
    const canvas = {
      name: "order-intake.canvas.json",
      size: 200,
      text: () => Promise.reject(new Error("The file was moved.")),
    } as unknown as File;
    await shell.importWorkflow(workflow, canvas);
    expect(said).toEqual([
      "The workflow opened without its canvas file: Studio couldn't read that file.",
    ]);
    expect(shell.model.definition.spec.steps.map((step) => step.id)).toEqual([
      "check-customer",
    ]);
    expect(shell.view).toBe("designer");
  });
});
