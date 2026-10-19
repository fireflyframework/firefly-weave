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
// A platform draft opened by its link waits for the signed-in account before
// its kept canvas comes back; an edit made meanwhile must not be written over
// what is kept.
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
afterAll(() => vi.unstubAllGlobals());

interface Waiting {
  keepDraftCanvas(): void;
  draftCanvasPending: { draftId: string; opened: number } | null;
  draftCanvasWrite: (() => Promise<unknown>) | null;
  draftCanvasTimer: ReturnType<typeof setTimeout> | null;
}

describe("a platform draft whose kept canvas hasn't come back", () => {
  it("writes nothing for an edit until it has, then keeps the next edit", () => {
    vi.useFakeTimers();
    try {
      const model = new StructuredCanvasAdapter();
      const shell = Object.create(App.prototype) as App & Waiting;
      Object.assign(shell, {
        model,
        draftId: "draft-1",
        draftCanvasFor: "",
        draftCanvasWrite: null,
        draftCanvasTimer: null,
        draftCanvasPending: { draftId: "draft-1", opened: model.opened },
      });
      // A getter on the shell: the platform this draft was opened from.
      Object.defineProperty(shell, "profile", { value: { name: "Acme" } });
      shell.keepDraftCanvas();
      expect(shell.draftCanvasWrite).toBeNull();
      expect(shell.draftCanvasTimer).toBeNull();
      shell.draftCanvasPending = null;
      shell.keepDraftCanvas();
      expect(shell.draftCanvasWrite).not.toBeNull();
      expect(shell.draftCanvasTimer).not.toBeNull();
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });
});
