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
// The run the Runs tab shows lives in memory only while the tab is open:
// leaving the tab, or opening another workflow from it, forgets it.
import "@angular/compiler";
import { signal } from "@angular/core";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import type { RunViewPort } from "../src/app/editor/runs-tab/run-port";
afterAll(() => vi.unstubAllGlobals());

interface Shell {
  tab: string;
  windowWidth: number;
  runView: ReturnType<typeof signal<RunViewPort | null>>;
  openTab(): void;
}

/** The shell on its Runs tab, showing a run that counts its closes. */
function onRuns() {
  const shell = Object.create(App.prototype) as App & Shell;
  const closed = { count: 0 };
  Object.assign(shell, {
    tab: "Runs",
    windowWidth: 1440,
    outlineNotice: false,
    showInspector: false,
    runView: signal<RunViewPort | null>({
      canvas: () => null,
      data: {} as RunViewPort["data"],
      close: () => void closed.count++,
    }),
    leaveInspector: () => true,
    flushAutosave: async () => undefined,
    cdr: { markForCheck: () => undefined },
  });
  return { shell, closed };
}

describe("the run the Runs tab shows", () => {
  it("is forgotten when another workflow opens from the Runs tab", () => {
    const { shell, closed } = onRuns();
    shell.openTab();
    expect(shell.tab).toBe("Designer");
    expect(closed.count).toBe(1);
    expect(shell.runView()).toBeNull();
  });

  it("is forgotten when another view is chosen", async () => {
    const { shell, closed } = onRuns();
    vi.stubGlobal("window", { innerWidth: 1440 });
    try {
      expect(await shell.selectTab("Outline")).toBe(true);
    } finally {
      vi.unstubAllGlobals();
      vi.stubGlobal("document", {
        addEventListener: vi.fn(),
        documentElement: { style: {} },
      });
    }
    expect(closed.count).toBe(1);
    expect(shell.runView()).toBeNull();
  });
});
