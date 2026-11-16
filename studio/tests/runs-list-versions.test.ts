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
// The Runs list's Version menu offers the workflow's published versions,
// including the ones the platform answers with after the menu was drawn.
import "@angular/compiler";
import { computed, signal } from "@angular/core";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { versionChoices } from "../src/app/editor/runs-tab/runs-list";
afterAll(() => vi.unstubAllGlobals());

/** The shell signed in, its published versions not read yet. */
function signedIn(items: Record<string, unknown>[]) {
  const shell = Object.create(App.prototype) as App;
  Object.assign(shell, {
    versionsScope: null,
    workflowVersions: new Map(),
    workflowVersionsChanged: signal(0),
    model: {
      definition: {
        metadata: { name: "invoice-approval", version: "1.3.0" },
      },
    },
    api: {
      session: { profile: { name: "Test platform" } },
      page: async () => ({ items, next_cursor: null }),
    },
    cdr: { markForCheck: () => undefined },
  });
  return shell as App & { loadWorkflowVersions(): Promise<void> };
}

describe("the Runs list's Version menu", () => {
  it("offers the published versions that load after it was drawn", async () => {
    const shell = signedIn([
      { id: "v-120", name: "invoice-approval", version: "1.2.0" },
      { id: "v-110", name: "invoice-approval", version: "1.1.0" },
      { id: "v-900", name: "another-workflow", version: "9.0.0" },
    ]);
    // The menu as the list draws it: a computed over the shell.
    const versions = computed(() =>
      versionChoices(shell, "invoice-approval", [], null),
    );
    // Drawn before the platform answered: nothing to offer yet.
    expect(versions()).toEqual([]);
    await shell.loadWorkflowVersions();
    expect(versions()).toEqual(["1.2.0", "1.1.0"]);
  });

  it("offers none of the versions read for the workspace left behind", async () => {
    const shell = signedIn([
      { id: "v-120", name: "invoice-approval", version: "1.2.0" },
    ]);
    // What forgetting the platform's state touches besides the versions.
    Object.assign(shell, {
      aiTests: new Map(),
      knownRuns: new Map(),
      contractCache: new Map(),
      closeRecord() {},
      closeSimulation() {},
    });
    const versions = computed(() =>
      versionChoices(shell, "invoice-approval", [], null),
    );
    await shell.loadWorkflowVersions();
    expect(versions()).toEqual(["1.2.0"]);
    // Another workspace, or the account signed out: nothing read stays.
    (shell as unknown as { clearPlatformState(): void }).clearPlatformState();
    expect(versions()).toEqual([]);
  });
});
