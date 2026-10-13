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
// A paste that waited (for applied edits or its code) never lands in a
// workflow opened meanwhile, even one whose opening count matches.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
import { App } from "../src/app/app";
import { readClipboard } from "../src/app/editor/state/clipboard";
import { StructuredCanvasAdapter } from "../src/app/model";
afterAll(() => vi.unstubAllGlobals());

describe("a paste while another workflow opens", () => {
  it("pastes nothing into the workflow opened meanwhile", async () => {
    const first = new StructuredCanvasAdapter();
    first.clearHistory();
    const next = new StructuredCanvasAdapter();
    // Opening a file builds a new model and clears its history once, so
    // both count one opening.
    next.clearHistory();
    expect(next.opened).toBe(first.opened);
    const app = Object.create(App.prototype) as App;
    const notify = vi.fn();
    Object.defineProperty(app, "catalogContracts", { value: new Map() });
    Object.assign(app, {
      actionVersions: [],
      perform: (change: () => void) => change(),
      changed() {},
      selectSteps() {},
      focusStep() {},
      model: first,
      cdr: { markForCheck() {} },
      notify,
      async ensureApplied() {
        Object.assign(app, { model: next });
        return true;
      },
    });
    const reading = readClipboard(
      "- {id: pasted, kind: wait, durationSeconds: 1}",
    );
    if (!reading.ok) throw new Error(reading.reason);
    expect(
      await app.pasteSteps(reading.content, { owner: "root", index: 0 }),
    ).toBeNull();
    expect(next.stepIdSet().has("pasted")).toBe(false);
    expect(first.stepIdSet().has("pasted")).toBe(false);
  });
});
