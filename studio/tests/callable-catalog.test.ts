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
// The workflows a step may call: the project's published versions, then the
// drafts on this computer, each disabled in the compiler's words when it
// doesn't allow this workflow. tests/unit/test_callable_corpus.py checks the
// same corpus against the platform's allows_caller and check_call_target.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  allowsCaller,
  CALLABLE_LIMIT,
  callables,
  type LocalCallee,
} from "../src/app/editor/calls/callable-catalog";
import {
  parseCatalogLock,
  type CatalogLockState,
} from "../src/app/editor/calls/catalog-lock";

interface ListCase {
  name: string;
  caller: string;
  lock: unknown;
  drafts: LocalCallee[];
  items: Record<string, string>[];
}
const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/callable-corpus.json"),
    "utf8",
  ),
) as {
  allows: {
    name: string;
    callable?: unknown;
    caller: string;
    allowed: boolean;
  }[];
  lists: ListCase[];
};
const ready = (lock: unknown): CatalogLockState => ({
  kind: "ready",
  lock: parseCatalogLock(lock)!,
});
const [PROJECT] = corpus.lists;
const DRAFTS = PROJECT.drafts;
/** What the corpus pins of an item. */
const pinned = (item: object) =>
  Object.fromEntries(
    Object.entries(item).filter(([key]) =>
      ["uses", "name", "version", "source", "disabled", "warning"].includes(
        key,
      ),
    ),
  );
const listed = (state: CatalogLockState, query?: string) =>
  callables({ caller: "order-intake", lock: state, drafts: DRAFTS, query });

describe("who may call a workflow version", () => {
  it.each(corpus.allows)("$name", ({ callable, caller, allowed }) => {
    expect(allowsCaller(callable, caller)).toBe(allowed);
  });
});

describe("the callable list", () => {
  it.each(corpus.lists)("$name", ({ caller, lock, drafts, items }) => {
    const list = callables({ caller, lock: ready(lock), drafts });
    expect(list.items.map(pinned)).toEqual(items);
    expect(list.truncated).toBe(false);
    expect(list.note).toBeNull();
  });

  it("carries each version's input and result", () => {
    const { items } = listed(ready(PROJECT.lock));
    const notify = items.find((item) => item.uses === "notify-customer@1.0.0");
    expect(notify?.inputSchema).toMatchObject({
      required: ["customerId"],
      properties: { customerId: { type: "string" } },
    });
    expect(notify?.outputSchema).toMatchObject({
      properties: { sent: { type: "boolean" } },
    });
    const broken = items.find((item) => item.uses === "broken-draft@0.1.0");
    expect(broken?.inputSchema).toBeNull();
    expect(broken?.outputSchema).toBeNull();
  });

  it.each([
    [
      "not connected",
      { kind: "offline" },
      "Connect to a platform to call published workflows.",
    ],
    [
      "without catalog read",
      { kind: "forbidden" },
      "You can't read this project's published workflows here.",
    ],
    [
      "when the read failed",
      {
        kind: "failed",
        message:
          "The platform could not be reached. Check your network connection and try again.",
      },
      "The platform could not be reached. Check your network connection and try again.",
    ],
    ["while the project's workflows load", { kind: "loading" }, null],
  ] as [string, CatalogLockState, string | null][])(
    "lists only the drafts on this computer %s",
    (_, state, note) => {
      const list = listed(state);
      expect(list.note).toBe(note);
      expect(list.items.map((item) => item.uses)).toEqual([
        "archive-order@0.10.0",
        "archive-order@0.2.0",
        "broken-draft@0.1.0",
        "lookup-customer@0.1.0",
        "notify-customer@1.0.0",
      ]);
      expect(list.items.every((item) => item.source === "local")).toBe(true);
    },
  );

  it("narrows to the versions whose name and version hold every word typed", () => {
    const uses = (query: string) =>
      listed(ready(PROJECT.lock), query).items.map((item) => item.uses);
    expect(uses("notify")).toEqual([
      "notify-customer@2.0.0-rc.1",
      "notify-customer@1.10.0",
      "notify-customer@1.9.0",
      "notify-customer@1.0.0",
    ]);
    expect(uses("  NOTIFY  1.1 ")).toEqual(["notify-customer@1.10.0"]);
    expect(uses("order")).toEqual([
      "order-intake@1.1.0",
      "archive-order@0.10.0",
      "archive-order@0.2.0",
    ]);
    expect(uses("nothing-like-this")).toEqual([]);
  });

  it(`stops at ${CALLABLE_LIMIT} versions and says the list was cut`, () => {
    const many: LocalCallee[] = Array.from(
      { length: CALLABLE_LIMIT + 1 },
      (_, index) => ({
        name: `step-${String(index).padStart(4, "0")}`,
        version: "0.1.0",
        document: { spec: { callable: {} } },
      }),
    );
    const all = callables({
      caller: "order-intake",
      lock: { kind: "offline" },
      drafts: many,
    });
    expect(all.items).toHaveLength(CALLABLE_LIMIT);
    expect(all.truncated).toBe(true);
    expect(all.items[0].uses).toBe("step-0000@0.1.0");
    const found = callables({
      caller: "order-intake",
      lock: { kind: "offline" },
      drafts: many,
      query: "step-0500",
    });
    expect(found.items.map((item) => item.uses)).toEqual(["step-0500@0.1.0"]);
    expect(found.truncated).toBe(false);
  });

  it("puts a release before its pre-releases, and orders pre-releases by their labels", () => {
    const list = callables({
      caller: "order-intake",
      lock: { kind: "offline" },
      drafts: ["0.9.0", "1.0.0-beta", "1.0.0-rc.2", "1.0.0", "1.0.0-rc.10"].map(
        (version) => ({
          name: "notify-customer",
          version,
          document: { spec: { callable: {} } },
        }),
      ),
    });
    expect(list.items.map((item) => item.version)).toEqual([
      "1.0.0",
      "1.0.0-rc.10",
      "1.0.0-rc.2",
      "1.0.0-beta",
      "0.9.0",
    ]);
  });

  it("leaves out a draft with no version, which no step can call", () => {
    const list = callables({
      caller: "order-intake",
      lock: { kind: "offline" },
      drafts: [{ name: "untitled-workflow", version: "", document: {} }],
    });
    expect(list.items).toEqual([]);
  });
});
