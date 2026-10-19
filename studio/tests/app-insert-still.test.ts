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
// Add a step hands the shell a "still there" check with every common step
// and published action it inserts; the shell must keep asking it, together
// with its own check that the workflow, account and view are the same.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
const captured = vi.hoisted(() => ({
  host: null as null | Record<string, (...args: never[]) => unknown>,
  here: null as null | (() => boolean),
}));
vi.mock("../src/app/editor/add-step/insert", () => ({
  insertEntry: vi.fn(async (host: never) => {
    captured.host = host;
    return "inserted";
  }),
}));
vi.mock("../src/app/integrations/editor-bridge", () => ({
  insertCatalogAction: vi.fn(
    async (
      _host: unknown,
      _choice: unknown,
      _at: unknown,
      _settled: unknown,
      here: () => boolean,
    ) => {
      captured.here = here;
      return here() ? "lookup-1" : "";
    },
  ),
}));
import { App } from "../src/app/app";
import { StructuredCanvasAdapter } from "../src/app/model";
afterAll(() => vi.unstubAllGlobals());

interface Shell {
  insertFromPanel(entry: unknown, request: unknown): Promise<unknown>;
  insertCommon(
    id: string,
    at: unknown,
    current: () => boolean,
    connector?: string,
  ): Promise<string>;
}

async function panelHost(owner: { ok: boolean }) {
  const shell = Object.create(App.prototype) as App & Shell;
  const passed: (() => boolean)[] = [];
  Object.assign(shell, {
    model: new StructuredCanvasAdapter(),
    insertOwner: () => () => owner.ok,
    insertCommon: async (_id: string, _at: unknown, current: () => boolean) => {
      passed.push(current);
      return current() ? "call-api" : "";
    },
  });
  Object.defineProperty(shell, "editingLocked", { value: false });
  await shell.insertFromPanel({}, { at: { owner: "root", index: 0 } });
  const host = captured.host as unknown as {
    insertCommon(
      common: string,
      at: unknown,
      connector: string | undefined,
      still: () => boolean,
    ): Promise<string>;
    insertPublished(
      uses: string,
      id: string,
      at: unknown,
      still: () => boolean,
    ): Promise<string>;
  };
  return { host, passed };
}

describe("Add a step's place check reaches the shell's inserts", () => {
  it("a common step asks both the workflow and the place", async () => {
    const owner = { ok: true };
    const { host, passed } = await panelHost(owner);
    let still = true;
    expect(
      await host.insertCommon(
        "call-api",
        { owner: "root", index: 0 },
        undefined,
        () => still,
      ),
    ).toBe("call-api");
    still = false;
    expect(
      await host.insertCommon(
        "call-api",
        { owner: "root", index: 0 },
        undefined,
        () => still,
      ),
    ).toBe("");
    expect(passed[1]()).toBe(false);
    still = true;
    owner.ok = false;
    expect(passed[1]()).toBe(false);
    owner.ok = true;
    expect(passed[1]()).toBe(true);
  });

  it("a published action asks both, before and after its details load", async () => {
    const owner = { ok: true };
    const { host } = await panelHost(owner);
    let still = true;
    expect(
      await host.insertPublished(
        "crm.lookup@1.0.0",
        "",
        { owner: "root", index: 0 },
        () => still,
      ),
    ).toBe("lookup-1");
    still = false;
    expect(captured.here!()).toBe(false);
    expect(
      await host.insertPublished(
        "crm.lookup@1.0.0",
        "",
        { owner: "root", index: 0 },
        () => still,
      ),
    ).toBe("");
    still = true;
    owner.ok = false;
    expect(captured.here!()).toBe(false);
  });
});
