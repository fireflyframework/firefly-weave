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
// A toast's Undo while a simulation runs: nothing changed since the toast,
// editing is paused, so it says that instead of "the workflow changed since".
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { StructuredCanvasAdapter } from "../src/app/model";
import type { ToastAction } from "../src/app/toast";
afterAll(() => vi.unstubAllGlobals());

const PAUSED = "Editing is paused while the simulation runs.";

function fixture() {
  const app = Object.create(App.prototype) as App;
  const model = new StructuredCanvasAdapter();
  const kept = model.insert("wait");
  const removed = model.insert("wait");
  const toasts: { text: string; action?: ToastAction }[] = [];
  Object.assign(app, {
    model,
    sourceBuffer: model.source,
    cdr: { markForCheck() {} },
    // Source shows the workflow as it is: nothing typed there.
    changed() {
      Object.assign(app, { sourceBuffer: model.source });
    },
    loadInspector() {},
    focusStep() {},
    // No test data: deleting prunes nothing.
    testData: {
      data: () => ({}),
      prune: () => [],
      version: () => 0,
      restore: () => true,
    },
    notify(text: string, action?: ToastAction) {
      toasts.push({ text, action });
    },
  });
  return { app, model, kept: kept.id, removed: removed.id, toasts };
}

describe("a toast's Undo while a simulation runs", () => {
  it("says editing is paused, and undoes nothing", async () => {
    const { app, model, kept, removed, toasts } = fixture();
    expect(
      await (
        app as unknown as { deleteSteps(ids: string[]): Promise<boolean> }
      ).deleteSteps([removed]),
    ).toBe(true);
    const [deleted] = toasts;
    expect(deleted.text).toBe(`Deleted ${removed}.`);
    const ids = () => model.definition.spec.steps.map((step) => step.id);
    expect(ids()).toEqual([kept]);
    // A simulation starts while the toast is still up.
    Object.assign(app, { debugSession: { id: "session", revision: 1 } });
    expect(app.editingLocked).toBe(true);
    await deleted.action!.run();
    expect(toasts.slice(1).map((toast) => toast.text)).toEqual([PAUSED]);
    expect(ids()).toEqual([kept]);
    expect(model.canUndo).toBe(true);
  });
  it("tells its callers it was paused, not that the workflow changed", async () => {
    const { app, toasts } = fixture();
    Object.assign(app, { debugSession: { id: "session", revision: 1 } });
    expect(await app.undoIfUnchanged(() => true)).toBe("locked");
    expect(toasts.map((toast) => toast.text)).toEqual([PAUSED]);
  });
});
