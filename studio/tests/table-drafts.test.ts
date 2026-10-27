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
  profilePrefix,
  purgeProfile,
  type StoreBackend,
} from "../src/app/editor/state/browser-store";
import {
  MAX_TABLE_COPIES,
  TableDrafts,
  copyKey,
  draftKey,
  freshTable,
  newTableName,
  nextDraftVersion,
  readEntry,
  type TableDocument,
} from "../src/app/editor/state/table-drafts";

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

const published = (
  version = "1.2.0",
  name = "payment-policy",
): TableDocument => ({
  ...freshTable(name, version),
  spec: {
    inputSchema: {
      type: "object",
      properties: { amount: { type: "number" } },
    },
    outputSchema: {
      type: "object",
      properties: { approve: { type: "boolean" } },
    },
    hitPolicy: "first",
    rules: [
      {
        id: "big",
        when: {
          op: {
            name: "gt",
            args: [{ ref: "/input/amount" }, { literal: 1000 }],
          },
        },
        output: { literal: { approve: false } },
      },
    ],
    defaultOutput: { literal: { approve: true } },
  },
});

describe("table versions and names", () => {
  it("starts the next free patch version above every listed one", () => {
    expect(nextDraftVersion("1.2.0", ["1.2.0", "1.2.1", "1.1.9"])).toBe(
      "1.2.2",
    );
    expect(nextDraftVersion("1.2.0", [])).toBe("1.2.1");
    expect(nextDraftVersion("1.2.0", ["2.0.0", "1.2.0"])).toBe("2.0.1");
    expect(nextDraftVersion("1.2.0", ["1.10.0", "1.9.3"])).toBe("1.10.1");
    let seed = 7;
    const next = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let round = 0; round < 200; round++) {
      const listed = Array.from(
        { length: 1 + next(8) },
        () => `${next(3)}.${next(4)}.${next(5)}`,
      );
      const version = nextDraftVersion(listed[next(listed.length)], listed);
      expect(listed).not.toContain(version);
      expect(version).toMatch(/^\d+\.\d+\.\d+$/);
    }
  });

  it("names a new table after the workflow and the step", () => {
    expect(newTableName("orders", "score", new Set())).toBe("orders.score");
    expect(newTableName("orders", "score", new Set(["orders.score"]))).toBe(
      "orders.score-2",
    );
    const fresh = freshTable("orders.score", "1.0.0");
    expect(fresh.metadata).toEqual({ name: "orders.score", version: "1.0.0" });
    expect(fresh.spec.rules).toEqual([
      { id: "rule-1", when: { literal: true }, output: { literal: {} } },
    ]);
    expect(fresh.spec.hitPolicy).toBe("first");
  });
});

describe("table drafts on this computer", () => {
  it("keeps drafts under the account's prefix and forgets them on sign-out", async () => {
    const backend = memoryBackend();
    const prefix = `${await profilePrefix("staging")}abc:`;
    const drafts = new TableDrafts(backend, prefix);
    await drafts.ready();
    const entry = await drafts.startDraft(published(), ["1.2.0"]);
    expect(entry).toMatchObject({
      kind: "draft",
      name: "payment-policy",
      version: "1.2.1",
      base: "1.2.0",
    });
    expect(entry.document.metadata.version).toBe("1.2.1");
    expect([...backend.data.keys()]).toEqual([
      draftKey(prefix, "payment-policy"),
    ]);
    expect(draftKey(prefix, "payment-policy")).toBe(
      `${prefix}weave.tableDraft.payment-policy`,
    );
    await drafts.copy(published());
    expect(backend.data.has(copyKey(prefix, "payment-policy", "1.2.0"))).toBe(
      true,
    );
    // Another session of the same account reads them back.
    const again = new TableDrafts(backend, prefix);
    await again.ready();
    expect(again.draft("payment-policy")?.version).toBe("1.2.1");
    expect(again.copyOf("payment-policy@1.2.0")?.metadata.version).toBe(
      "1.2.0",
    );
    await purgeProfile(backend, "staging");
    expect(
      [...backend.data.keys()].filter((k) => k.includes("weave.table")),
    ).toEqual([]);

    const memory = memoryBackend();
    const unattributed = new TableDrafts(memory, null);
    await unattributed.ready();
    await unattributed.newDraft("orders", "score", new Set());
    expect(memory.data.size).toBe(0);
    expect(unattributed.draft("orders.score")?.version).toBe("1.0.0");
  });

  it("reads only typed entries", () => {
    const good = {
      kind: "draft",
      name: "payment-policy",
      version: "1.2.1",
      base: "1.2.0",
      document: published("1.2.1"),
      savedAt: "2026-10-27T10:00:00.000Z",
    };
    expect(readEntry(JSON.stringify(good))).toEqual(good);
    for (const bad of [
      { ...good, version: "1.2" },
      { ...good, version: "latest" },
      { ...good, extra: 1 },
      { ...good, document: { ...good.document, kind: "Workflow" } },
      { ...good, document: published("1.2.0") },
      { ...good, name: "other" },
      { ...good, base: 3 },
      { ...good, kind: "copy" },
      { ...good, document: { ...good.document, spec: { rules: [] } } },
    ])
      expect(readEntry(JSON.stringify(bad)), JSON.stringify(bad)).toBeNull();
    expect(readEntry("not json")).toBeNull();
    const copy = {
      kind: "copy",
      name: "payment-policy",
      version: "1.2.0",
      document: published(),
      readAt: "2026-10-27T10:00:00.000Z",
    };
    expect(readEntry(JSON.stringify(copy))).toEqual(copy);
    expect(readEntry(JSON.stringify({ ...copy, base: null }))).toBeNull();
  });

  it("deletes stored entries it can't read", async () => {
    const backend = memoryBackend();
    backend.data.set("local:weave.tableDraft.broken", "{}");
    const drafts = new TableDrafts(backend, "local:");
    await drafts.ready();
    expect(drafts.entries()).toEqual([]);
    expect(backend.data.size).toBe(0);
  });

  it("refuses a draft past 1 MB and a 51st draft", async () => {
    const drafts = new TableDrafts(memoryBackend(), "local:");
    await drafts.ready();
    const entry = await drafts.newDraft("orders", "score", new Set());
    const huge = {
      ...entry.document.spec,
      rules: [
        {
          id: "rule-1",
          when: { literal: "x".repeat(1_100_000) },
          output: { literal: {} },
        },
      ],
    };
    await expect(drafts.save(entry, huge)).rejects.toThrow(
      "This table is too large to keep on this computer (1 MB at most).",
    );
    expect(drafts.draft("orders.score")?.document.spec.rules[0].id).toBe(
      "rule-1",
    );
    for (let i = 2; i <= 50; i++)
      await drafts.newDraft("orders", `step${i}`, new Set());
    await expect(
      drafts.newDraft("orders", "one-too-many", new Set()),
    ).rejects.toThrow(
      "Studio keeps 50 table drafts on this computer; publish or delete one first.",
    );
    await drafts.remove("orders.step2");
    await drafts.newDraft("orders", "one-too-many", new Set());
    expect(drafts.draft("orders.one-too-many")).not.toBeNull();
  });

  it("saves the table into the draft and keeps its name and version", async () => {
    const backend = memoryBackend();
    const drafts = new TableDrafts(backend, "local:");
    await drafts.ready();
    const entry = await drafts.startDraft(published(), ["1.2.0"]);
    const spec = { ...entry.document.spec, hitPolicy: "unique" as const };
    const saved = await drafts.save(entry, spec);
    expect(saved.document.spec.hitPolicy).toBe("unique");
    expect(saved.document.metadata).toEqual({
      name: "payment-policy",
      version: "1.2.1",
    });
    const stored = JSON.parse(
      backend.data.get("local:weave.tableDraft.payment-policy")!,
    );
    expect(stored.document.spec.hitPolicy).toBe("unique");
  });

  it("moves a draft to the next version when its version was taken", async () => {
    const backend = memoryBackend();
    const drafts = new TableDrafts(backend, "local:");
    await drafts.ready();
    const entry = await drafts.startDraft(published(), ["1.2.0"]);
    const moved = await drafts.moveDraft(entry, "1.2.2");
    expect(moved.document.metadata).toEqual({
      name: "payment-policy",
      version: "1.2.2",
    });
    expect(drafts.draft("payment-policy")?.version).toBe("1.2.2");
    expect(
      readEntry(backend.data.get("local:weave.tableDraft.payment-policy")!)
        ?.version,
    ).toBe("1.2.2");
  });

  it("keeps the 200 most recently read copies", async () => {
    const backend = memoryBackend();
    const drafts = new TableDrafts(backend, "local:");
    await drafts.ready();
    for (let i = 0; i < MAX_TABLE_COPIES + 5; i++)
      await drafts.copy(published(`1.0.${i}`));
    const copies = drafts.entries().filter((entry) => entry.kind === "copy");
    expect(copies).toHaveLength(MAX_TABLE_COPIES);
    expect(drafts.copyOf("payment-policy@1.0.0")).toBeNull();
    expect(drafts.copyOf("payment-policy@1.0.4")).toBeNull();
    expect(drafts.copyOf("payment-policy@1.0.5")).not.toBeNull();
    expect(
      [...backend.data.keys()].filter((k) => k.includes("tableCopy")),
    ).toHaveLength(MAX_TABLE_COPIES);
    // Reading a copy again makes it recent.
    await drafts.copy(published("1.0.5"));
    await drafts.copy(published("1.0.999"));
    expect(drafts.copyOf("payment-policy@1.0.5")).not.toBeNull();
    expect(drafts.copyOf("payment-policy@1.0.6")).toBeNull();
  });

  it("lists definitions for the versions a workflow uses, drafts first", async () => {
    const drafts = new TableDrafts(memoryBackend(), "local:");
    await drafts.ready();
    await drafts.copy(published("1.2.0"));
    await drafts.copy(published("1.0.0", "limits"));
    await drafts.startDraft(published("1.2.0"), ["1.2.0"]);
    const found = drafts.definitionsFor([
      "payment-policy@1.2.1",
      "limits@1.0.0",
      "payment-policy@1.2.0",
      "payment-policy@1.2.1",
      "unknown@1.0.0",
    ]);
    expect(
      found.map((d) => `${d.metadata.name}@${d.metadata.version}`),
    ).toEqual(["payment-policy@1.2.1", "limits@1.0.0", "payment-policy@1.2.0"]);
  });
});
