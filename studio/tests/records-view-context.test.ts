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
// The records view's row context follows the published versions the shell
// holds, even when a change replaces them with as many other versions.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    documentElement: { style: {} },
  }),
);
import type { App } from "../src/app/app";
import { RecordsView } from "../src/app/operations/records-view";
afterAll(() => vi.unstubAllGlobals());

type Versions = Map<string, { name: string; version: string }>;

/** A records view over a shell that holds the given published versions. */
function viewOver(versions: Versions) {
  const shell = {
    identity: null,
    workflowVersions: versions,
    activationsByVersion: new Map(),
    workspaceNames: { environment: "development" },
    libraryCollection: "platform",
    knownRuns: new Map(),
  };
  const view = Object.create(RecordsView.prototype) as RecordsView;
  Object.assign(view, {
    host: () => shell as unknown as App,
    contextCache: { key: [], value: {} },
  });
  return { view, shell };
}

describe("the records view's row context", () => {
  it("reads the published versions that replaced as many others", () => {
    const { view, shell } = viewOver(
      new Map([["v1", { name: "order-intake", version: "1.0.0" }]]),
    );
    expect(view.context().versions).toBe(shell.workflowVersions);

    shell.workflowVersions = new Map([
      ["v2", { name: "order-intake", version: "1.1.0" }],
    ]);

    expect(view.context().versions).toBe(shell.workflowVersions);
    expect([...(view.context().versions as Versions).keys()]).toEqual(["v2"]);
  });
});
