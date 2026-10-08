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
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
  LOCAL_PREFIX,
  UI_PREFIX,
  browserBackend,
  connectedPrefix,
  localKey,
  migrateLegacyKeys,
  profileId,
  profilePrefix,
  purgeProfile,
  storeKey,
  uiKey,
  type BrowserStorage,
} from "../src/app/editor/state/browser-store";
import {
  EDITOR_NEXT_KEY,
  editorNextEnabled,
  setEditorNext,
} from "../src/app/editor/state/editor-flag";

class MemoryStorage implements BrowserStorage {
  items = new Map<string, string>();
  get length() {
    return this.items.size;
  }
  key(index: number) {
    return [...this.items.keys()][index] ?? null;
  }
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}
const refusing = (): BrowserStorage => {
  throw new Error("Storage is disabled");
};
const hex16 = (text: string) =>
  createHash("sha256").update(text).digest("hex").slice(0, 16);
const tenant = "0b4a4c1e-5d6f-4a7b-8c9d-0e1f2a3b4c5d";
const project = "1c5b5d2f-6e7a-4b8c-9d0e-1f2a3b4c5d6e";

describe("Studio storage keys", () => {
  it("prefixes connected work with profile, subject, tenant and project", async () => {
    expect(
      await connectedPrefix({
        profileName: "Orders platform",
        subject: "user-123",
        tenantId: tenant,
        projectId: project.toUpperCase(),
      }),
    ).toBe(
      `p${hex16("Orders platform")}:${hex16("user-123")}:${tenant}:${project}:`,
    );
  });

  it("keeps nothing for an account whose subject is unknown", async () => {
    for (const subject of [null, ""])
      expect(
        await connectedPrefix({
          profileName: "Orders platform",
          subject,
          tenantId: tenant,
          projectId: project,
        }),
      ).toBeNull();
  });

  it("refuses scopes and names that would make ambiguous keys", async () => {
    await expect(
      connectedPrefix({
        profileName: "p",
        subject: "s",
        tenantId: "tenant:1",
        projectId: project,
      }),
    ).rejects.toThrow(RangeError);
    expect(() => storeKey(LOCAL_PREFIX, "a:b")).toThrow(RangeError);
    expect(() => storeKey(LOCAL_PREFIX, "draft one")).toThrow(RangeError);
    expect(() => storeKey(LOCAL_PREFIX, "x".repeat(251))).toThrow(RangeError);
    expect(localKey("weave.localDrafts.v1")).toBe("local:weave.localDrafts.v1");
    expect(uiKey("weave.editorNext")).toBe("ui:weave.editorNext");
  });

  it("gives profiles an ID that never equals the local or ui prefix", async () => {
    for (const name of ["local", "ui", "Local"]) {
      const id = await profileId(name);
      expect(id).toMatch(/^p[0-9a-f]{16}$/);
      expect(`${id}:`).not.toBe(LOCAL_PREFIX);
      expect(`${id}:`).not.toBe(UI_PREFIX);
    }
  });
});

describe("purging a platform", () => {
  it("deletes that platform's keys only, never local or ui keys", async () => {
    const storage = new MemoryStorage();
    const backend = browserBackend(() => storage);
    const local = await profilePrefix("local");
    const other = await profilePrefix("staging");
    for (const key of [
      `${local}abc:${tenant}:${project}:weave.testData.1`,
      `${local}def:${tenant}:${project}:weave.testData.2`,
      `${other}abc:${tenant}:${project}:weave.testData.1`,
      "local:weave.localDrafts.v1",
      "ui:weave.editorNext",
    ])
      storage.setItem(key, "{}");
    expect(await purgeProfile(backend, "local")).toBe(2);
    expect([...storage.items.keys()].sort()).toEqual(
      [
        `${other}abc:${tenant}:${project}:weave.testData.1`,
        "local:weave.localDrafts.v1",
        "ui:weave.editorNext",
      ].sort(),
    );
  });

  it("reads refused storage as empty and reports failed writes", async () => {
    const backend = browserBackend(refusing);
    expect(await backend.keys("")).toEqual([]);
    expect(await backend.get("ui:x")).toBeNull();
    expect(await backend.set("ui:x", "1")).toBe(false);
    expect(await backend.remove("ui:x")).toBe(false);
    expect(await purgeProfile(backend, "local")).toBe(0);
  });
});

describe("keys from before prefixes", () => {
  it("moves drafts and pane preferences and deletes run inputs, once", async () => {
    const storage = new MemoryStorage();
    storage.setItem("weave.localDrafts.v1", "[]");
    storage.setItem("weave.localDraft.42", "{}");
    storage.setItem("weave.studio.inspectorWidth", "420");
    storage.setItem("weave-studio-run-input:v1", '{"amount":1}');
    storage.setItem("unrelated", "kept");
    const backend = browserBackend(() => storage);
    expect(await migrateLegacyKeys(backend)).toEqual({
      moved: 3,
      deleted: 1,
      kept: 0,
    });
    expect(Object.fromEntries(storage.items)).toEqual({
      "local:weave.localDrafts.v1": "[]",
      "local:weave.localDraft.42": "{}",
      "ui:weave.studio.inspectorWidth": "420",
      unrelated: "kept",
    });
    expect(await migrateLegacyKeys(backend)).toEqual({
      moved: 0,
      deleted: 0,
      kept: 0,
    });
  });

  it("never overwrites a key that already moved", async () => {
    const storage = new MemoryStorage();
    storage.setItem("local:weave.localDraft.42", "new");
    storage.setItem("weave.localDraft.42", "old");
    expect(await migrateLegacyKeys(browserBackend(() => storage))).toEqual({
      moved: 0,
      deleted: 1,
      kept: 0,
    });
    expect(Object.fromEntries(storage.items)).toEqual({
      "local:weave.localDraft.42": "new",
    });
  });
});

describe("the new editor flag", () => {
  it("is off by default, on with ?editor=next or the stored preference", () => {
    const storage = new MemoryStorage();
    expect(editorNextEnabled("", () => storage)).toBe(false);
    expect(editorNextEnabled("?editor=next", () => storage)).toBe(true);
    expect(setEditorNext(true, () => storage)).toBe(true);
    expect(storage.getItem(EDITOR_NEXT_KEY)).toBe("true");
    expect(EDITOR_NEXT_KEY).toBe("ui:weave.editorNext");
    expect(editorNextEnabled("", () => storage)).toBe(true);
    setEditorNext(false, () => storage);
    expect(editorNextEnabled("", () => storage)).toBe(false);
  });

  it("stays off when storage refuses", () => {
    expect(editorNextEnabled("", refusing)).toBe(false);
    expect(setEditorNext(true, refusing)).toBe(false);
  });
});
