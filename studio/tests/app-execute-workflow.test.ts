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
// Execute workflow runs at once, or as a session the run log steps through
// when a breakpoint is set. Waits always advance in Studio for now, so test
// data that keeps waits from advancing doesn't start a session.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
afterAll(() => vi.unstubAllGlobals());

function shell(breakpoints: string[], autoAdvance?: boolean) {
  const app = Object.create(App.prototype) as App & {
    startSession: () => Promise<void>;
    executeIn: (target: unknown) => Promise<unknown>;
  };
  Object.assign(app, {
    breakpoints: () => new Set(breakpoints),
    testData: {
      data: () => (autoAdvance === undefined ? {} : { waits: { autoAdvance } }),
    },
    startSession: vi.fn(async () => undefined),
    executeIn: vi.fn(async () => undefined),
  });
  return app;
}

describe("Execute workflow", () => {
  it("steps through a session only when a breakpoint is set", () => {
    const marked = shell(["second"]);
    marked.executeWorkflow();
    expect(marked.startSession).toHaveBeenCalledOnce();
    expect(marked.executeIn).not.toHaveBeenCalled();
    const waits = shell([], false);
    waits.executeWorkflow();
    expect(waits.startSession).not.toHaveBeenCalled();
    expect(waits.executeIn).toHaveBeenCalledWith({ mode: "workflow" });
  });
});
