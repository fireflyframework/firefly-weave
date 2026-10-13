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
import { describe, expect, it } from "vitest";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import { emptyCanvas } from "../src/app/editor/state/canvas-sidecar";
import {
  DraftCanvases,
  draftCanvasKey,
} from "../src/app/editor/state/draft-canvas";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const scope = {
  profileName: "Acme",
  subject: "human",
  tenantId: tenant,
  projectId: project,
};
const draft = "6a1f8c52-1d2e-4f3a-9b8c-7d6e5f4a3b2c";
function memory(): StoreBackend & { values: Map<string, string> } {
  const values = new Map<string, string>();
  return {
    values,
    keys: async (prefix) =>
      [...values.keys()].filter((key) => key.startsWith(prefix)),
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
}
const noted = { ...emptyCanvas(), stepNotes: { check: { text: "Ask Ana" } } };

describe("a platform draft's canvas on this computer", () => {
  it("keeps a draft's canvas per account, workspace and draft", async () => {
    const key = await draftCanvasKey(scope, draft);
    expect(key).toMatch(
      new RegExp(
        `^p[0-9a-f]{16}:[0-9a-f]{16}:${tenant}:${project}:canvas\\.${draft}$`,
      ),
    );
    expect(
      await draftCanvasKey({ ...scope, subject: "someone-else" }, draft),
    ).not.toBe(key);
    expect(
      await draftCanvasKey({ ...scope, projectId: tenant }, draft),
    ).not.toBe(key);
    const backend = memory();
    const kept = new DraftCanvases(backend);
    expect(await kept.write(scope, draft, noted)).toBe("computer");
    expect(await kept.read(scope, draft)).toEqual(noted);
    expect(
      await kept.read({ ...scope, subject: "someone-else" }, draft),
    ).toBeNull();
  });

  it("keeps nothing it can't attribute, and forgets an empty canvas", async () => {
    const backend = memory();
    const kept = new DraftCanvases(backend);
    expect(await draftCanvasKey({ ...scope, subject: null }, draft)).toBeNull();
    expect(
      await draftCanvasKey({ ...scope, tenantId: "tenant" }, draft),
    ).toBeNull();
    expect(await kept.write({ ...scope, subject: null }, draft, noted)).toBe(
      "memory",
    );
    expect(backend.values.size).toBe(0);
    await kept.write(scope, draft, noted);
    expect(backend.values.size).toBe(1);
    expect(await kept.write(scope, draft, emptyCanvas())).toBe("computer");
    expect(backend.values.size).toBe(0);
  });

  it("reads nothing from text that isn't JSON, and says memory when storage refuses", async () => {
    const backend = memory();
    const kept = new DraftCanvases(backend);
    backend.values.set((await draftCanvasKey(scope, draft))!, "{");
    expect(await kept.read(scope, draft)).toBeNull();
    const refusing = { ...memory(), set: async () => false };
    expect(await new DraftCanvases(refusing).write(scope, draft, noted)).toBe(
      "memory",
    );
  });

  it("forgets the older canvas when a newer one can't be written, so it never comes back in its place", async () => {
    const backend = memory();
    const kept = new DraftCanvases(backend);
    await kept.write(scope, draft, noted);
    backend.set = async () => false;
    const changed = { ...noted, stepNotes: { check: { text: "Ask Bo" } } };
    expect(await kept.write(scope, draft, changed)).toBe("memory");
    expect(await kept.read(scope, draft)).toBeNull();
  });

  it("keeps the 50 most recently changed canvases of an account, within 2 MiB", async () => {
    const backend = memory();
    let clock = 0;
    const kept = new DraftCanvases(backend, () => ++clock);
    const id = (n: number) =>
      `6a1f8c52-1d2e-4f3a-9b8c-${String(n).padStart(12, "0")}`;
    for (let n = 0; n < 50; n++) await kept.write(scope, id(n), noted);
    // Changing the oldest makes it the newest.
    await kept.write(scope, id(0), { ...noted, collapsed: ["check"] });
    await kept.write(scope, id(50), noted);
    expect(await kept.read(scope, id(1))).toBeNull();
    expect(await kept.read(scope, id(0))).not.toBeNull();
    expect(await kept.read(scope, id(50))).toEqual(noted);
    expect(backend.values.size).toBe(50);
    // Another account's canvases are its own.
    const other = { ...scope, subject: "someone-else" };
    await kept.write(other, id(1), noted);
    expect(await kept.read(scope, id(2))).toEqual(noted);
    // Large canvases: the oldest go until the account's total fits 2 MiB.
    const big = {
      ...emptyCanvas(),
      stepNotes: Object.fromEntries(
        Array.from({ length: 120 }, (_, n) => [
          `s${n}`,
          { text: "x".repeat(1900) },
        ]),
      ),
    };
    for (let n = 100; n < 112; n++) await kept.write(scope, id(n), big);
    const own = (await draftCanvasKey(scope, draft))!.replace(
      /canvas\..*$/,
      "",
    );
    const sizes = [...backend.values.entries()]
      .filter(([key]) => key.startsWith(own))
      .map(([, value]) => value.length);
    expect(sizes.reduce((sum, size) => sum + size, 0)).toBeLessThanOrEqual(
      2 * 1024 * 1024,
    );
    expect(await kept.read(scope, id(111))).toEqual(big);
    expect(await kept.read(scope, id(100))).toBeNull();
  });

  it("forgets every kept canvas of a platform when it signs out or is removed", async () => {
    const backend = memory();
    const kept = new DraftCanvases(backend);
    await kept.write(scope, draft, noted);
    await kept.write({ ...scope, profileName: "Globex" }, draft, noted);
    expect(await kept.forgetPlatform("Acme")).toBe(1);
    expect(await kept.read(scope, draft)).toBeNull();
    expect(await kept.read({ ...scope, profileName: "Globex" }, draft)).toEqual(
      noted,
    );
  });
});
