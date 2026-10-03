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
import {
  LocalDrafts,
  draftKey,
  indexKey,
  localKeepLabel,
  type DraftStorage,
} from "../src/app/local-drafts";

class MemoryStorage implements DraftStorage {
  items = new Map<string, string>();
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
const document = (source: string, savedAt: string) => ({
  source,
  format: "yaml" as const,
  layout: { positions: {} },
  savedAt,
});

describe("drafts on this computer", () => {
  it("keeps an index, newest first, and one document per draft", () => {
    const storage = new MemoryStorage();
    const drafts = new LocalDrafts(() => storage);
    expect(drafts.list()).toEqual([]);
    const a = {
      id: "a",
      name: "first",
      version: "1.0.0",
      savedAt: "2026-10-01T10:00:00Z",
    };
    const b = {
      id: "b",
      name: "second",
      version: "1.0.0",
      savedAt: "2026-10-02T10:00:00Z",
    };
    expect(drafts.save(a, document("kind: A", a.savedAt))).toBe(true);
    expect(drafts.save(b, document("kind: B", b.savedAt))).toBe(true);
    expect(drafts.list()!.map((e) => e.id)).toEqual(["b", "a"]);
    expect(drafts.latest()!.name).toBe("second");
    expect(drafts.read("a")!.source).toBe("kind: A");
    expect(storage.items.has(indexKey)).toBe(true);
    expect(storage.items.has(draftKey("b"))).toBe(true);
    // Saving again replaces the entry instead of adding one.
    const again = { ...a, savedAt: "2026-10-03T10:00:00Z" };
    drafts.save(again, document("kind: A2", again.savedAt));
    expect(drafts.list()!.map((e) => e.id)).toEqual(["a", "b"]);
    expect(drafts.read("a")!.source).toBe("kind: A2");
    expect(drafts.failed).toBe(false);
  });

  it("removes a draft and puts it back for Undo", () => {
    const storage = new MemoryStorage();
    const drafts = new LocalDrafts(() => storage);
    const a = {
      id: "a",
      name: "first",
      version: "1.0.0",
      savedAt: "2026-10-01T10:00:00Z",
    };
    drafts.save(a, document("kind: A", a.savedAt));
    const removed = drafts.remove("a")!;
    expect(drafts.list()).toEqual([]);
    expect(drafts.read("a")).toBeNull();
    expect(drafts.remove("missing")).toBeNull();
    expect(drafts.restore(removed)).toBe(true);
    expect(drafts.read("a")!.source).toBe("kind: A");
  });

  it("reports storage that throws instead of crashing", () => {
    const broken: DraftStorage = {
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("full", "QuotaExceededError");
      },
      removeItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    };
    const drafts = new LocalDrafts(() => broken);
    expect(drafts.list()).toBeNull();
    expect(drafts.read("a")).toBeNull();
    expect(
      drafts.save(
        { id: "a", name: "a", version: "1.0.0", savedAt: "" },
        document("x", ""),
      ),
    ).toBe(false);
    expect(drafts.failed).toBe(true);
    expect(new LocalDrafts(() => null).list()).toBeNull();
  });

  it("ignores damaged entries", () => {
    const storage = new MemoryStorage();
    storage.setItem(
      indexKey,
      JSON.stringify([{ name: "no id" }, 3, { id: "ok" }]),
    );
    storage.setItem(draftKey("ok"), "{not json");
    const drafts = new LocalDrafts(() => storage);
    expect(drafts.list()!.map((e) => [e.id, e.name])).toEqual([
      ["ok", "untitled-workflow"],
    ]);
    expect(drafts.read("ok")).toBeNull();
  });
});

describe("where local work lives", () => {
  it("names the desktop app's limit and the browser's", () => {
    expect(localKeepLabel(true)).toBe("Kept until you quit");
    expect(localKeepLabel(false)).toBe("Kept on this computer");
  });
});
