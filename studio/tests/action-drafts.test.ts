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
  ActionDrafts,
  actionDraftKey,
  readActionEntry,
} from "../src/app/editor/state/action-drafts";
import {
  profilePrefix,
  purgeProfile,
  type StoreBackend,
} from "../src/app/editor/state/browser-store";

function memoryBackend(): StoreBackend & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    keys: async (prefix) =>
      [...data.keys()].filter((k) => k.startsWith(prefix)),
    get: async (key) => data.get(key) ?? null,
    set: async (key, value) => {
      data.set(key, value);
      return true;
    },
    remove: async (key) => data.delete(key),
  };
}
const action = (version = "1.0.0") => ({
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "onboarding.check-customer", version },
  spec: {
    implementation: { kind: "http", method: "GET", url: "https://example.com" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    retry: { maxAttempts: 3, initialDelaySeconds: 1, maxDelaySeconds: 30 },
  },
});

describe("action drafts on this computer", () => {
  it("starts the next free patch version and keeps it under the account", async () => {
    const backend = memoryBackend();
    const prefix = `${await profilePrefix("staging")}abc:`;
    const drafts = new ActionDrafts(backend, prefix);
    await drafts.ready();
    const entry = await drafts.startDraft(action(), ["1.0.0", "1.0.1"]);
    expect(entry.version).toBe("1.0.2");
    expect(entry.base).toBe("1.0.0");
    expect(entry.document.metadata.version).toBe("1.0.2");
    expect([...backend.data.keys()]).toEqual([
      actionDraftKey(prefix, "onboarding.check-customer"),
    ]);
    expect(actionDraftKey(prefix, "a")).toBe(`${prefix}weave.actionDraft.a`);
    const saved = await drafts.save(entry, {
      ...entry.document,
      spec: { ...entry.document.spec, timeoutSeconds: 10 },
    });
    expect(saved.document.spec["timeoutSeconds"]).toBe(10);
    const again = new ActionDrafts(backend, prefix);
    await again.ready();
    expect(again.draft("onboarding.check-customer")?.version).toBe("1.0.2");
    expect(
      again.definitionsFor(["onboarding.check-customer@1.0.2", "x@1.0.0"]),
    ).toHaveLength(1);
    await purgeProfile(backend, "staging");
    expect(backend.data.size).toBe(0);
  });

  it("reads only typed entries", () => {
    const good = {
      kind: "draft",
      name: "onboarding.check-customer",
      version: "1.0.1",
      base: "1.0.0",
      document: action("1.0.1"),
      savedAt: "2026-10-27T10:00:00.000Z",
    };
    expect(readActionEntry(JSON.stringify(good))).toEqual(good);
    for (const bad of [
      { ...good, extra: 1 },
      { ...good, document: { ...good.document, kind: "DecisionTable" } },
      { ...good, document: action("1.0.0") },
      { ...good, version: "next" },
    ])
      expect(readActionEntry(JSON.stringify(bad))).toBeNull();
  });

  it("keeps 50 drafts at most, and a draft keeps its name", async () => {
    const drafts = new ActionDrafts(memoryBackend(), "local:");
    await drafts.ready();
    for (let i = 0; i < 50; i++)
      await drafts.startDraft(
        { ...action(), metadata: { name: `a${i}`, version: "1.0.0" } },
        [],
      );
    await expect(
      drafts.startDraft(
        { ...action(), metadata: { name: "b", version: "1.0.0" } },
        [],
      ),
    ).rejects.toThrow(
      "Studio keeps 50 action drafts on this computer; publish or delete one first.",
    );
    const entry = drafts.draft("a1")!;
    await expect(
      drafts.save(entry, {
        ...entry.document,
        metadata: { name: "other", version: "1.0.1" },
      }),
    ).rejects.toThrow("Keep the action's name: a1.");
  });
});
