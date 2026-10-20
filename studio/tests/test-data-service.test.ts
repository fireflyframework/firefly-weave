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
  TestDataService,
  type TestDataHost,
} from "../src/app/editor/state/test-data-service";
import { CHECK_WORDS } from "../src/app/editor/state/test-data-check";
import {
  emptyTestData,
  withInput,
  withPin,
} from "../src/app/editor/state/test-data-ops";
import type { StoreBackend } from "../src/app/editor/state/browser-store";
import type { PinEntry } from "../src/app/editor/state/test-data";

const NOW = new Date("2026-10-20T10:00:00Z");
const ISO = NOW.toISOString();
const pin = (output: unknown): PinEntry => ({
  output: output as PinEntry["output"],
  source: "manual",
  pinnedAt: ISO,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
});
const memory = () => {
  const values = new Map<string, string>();
  const backend: StoreBackend = {
    keys: async (prefix) =>
      [...values.keys()].filter((key) => key.startsWith(prefix)),
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => (values.set(key, value), true),
    remove: async (key) => values.delete(key),
  };
  return { values, backend };
};
/** A host whose check answers from `codes` by pointer; `down` makes the host unreachable. */
function host(
  codes: Record<string, string> = {},
  down = false,
  schema: unknown = { type: "object" },
) {
  const store = memory();
  const sent: unknown[] = [];
  const value: TestDataHost = {
    api: {
      request: async <T>(
        path: string,
        _method?: string,
        body?: unknown,
      ): Promise<T> => {
        sent.push(body);
        if (down) throw Object.assign(new Error("offline"), { status: 0 });
        expect(path).toBe("/studio/local/testdata/check");
        const data = (body as { testData: Record<string, unknown> }).testData;
        const paths = [
          ...(data["input"] ? ["/input"] : []),
          ...Object.keys((data["pins"] as object) ?? {}).map(
            (key) => `/pins/${key}`,
          ),
        ];
        return {
          entries: paths.map((path) =>
            codes[path]
              ? { path, ok: false, code: codes[path] }
              : { path, ok: true },
          ),
        } as T;
      },
    },
    backend: store.backend,
    now: () => NOW,
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => schema as never,
    }),
  };
  return { value, store, sent };
}

describe("a workflow's test data", () => {
  it("keeps a checked change next to its local draft", async () => {
    const { value, store } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const next = withPin(emptyTestData(ISO), "lookup", pin({ id: 1 }), ISO);
    const outcome = await service.change(next, ["/pins/lookup"]);
    expect(outcome).toEqual({ ok: true, previous: emptyTestData(ISO) });
    await service.flush();
    expect(
      JSON.parse(store.values.get("local:weave.localDraft.d-1.testData")!),
    ).toEqual(next);
    const reopened = new TestDataService(value);
    await reopened.open({ kind: "local", draftId: "d-1" });
    expect(reopened.data()).toEqual(next);
  });
  it("never keeps a value the check refuses as secret, in memory or in storage", async () => {
    const { value, store } = host({ "/pins/token": "WV-SCHEMA-SECRET_VALUE" });
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const before = service.data();
    const outcome = await service.change(
      withPin(before, "token", pin({ token: "abc" }), ISO),
      ["/pins/token"],
    );
    expect(outcome).toEqual({
      ok: false,
      message: CHECK_WORDS["WV-SCHEMA-SECRET_VALUE"],
      pointer: "/pins/token",
    });
    expect(service.data()).toBe(before);
    await service.flush();
    expect([...store.values.values()].join("")).not.toContain("abc");
  });
  it("refuses any value under a schema that mentions a secret when the host can't answer", async () => {
    const secretSchema = {
      type: "object",
      properties: { a: { type: "string", writeOnly: true } },
    };
    const { value } = host({}, true, secretSchema);
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const outcome = await service.change(
      withInput(service.data(), { b: 1 }, "manual", ISO),
      ["/input"],
    );
    expect(outcome.ok).toBe(false);
    const plain = host({}, true, { type: "object" });
    const kept = new TestDataService(plain.value);
    await kept.open({ kind: "local", draftId: "d-1" });
    expect(
      (
        await kept.change(withInput(kept.data(), { b: 1 }, "manual", ISO), [
          "/input",
        ])
      ).ok,
    ).toBe(true);
  });
  it("keeps one workflow's data out of another's", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(withInput(service.data(), 1, "manual", ISO), [
      "/input",
    ]);
    await service.open({ kind: "local", draftId: "d-2" });
    expect(service.data().input).toBeUndefined();
  });
  it("keeps data nobody can attribute in memory only", async () => {
    const { value, store } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "memory" });
    await service.change(withInput(service.data(), 1, "manual", ISO), [
      "/input",
    ]);
    await service.flush();
    expect(store.values.size).toBe(0);
    expect(service.data().input?.value).toBe(1);
  });
  it("follows renames and deletions, and Undo restores only an unchanged document", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(withPin(service.data(), "send", pin(1), ISO), [
      "/pins/send",
    ]);
    service.rename("send", "notify");
    expect(Object.keys(service.data().pins ?? {})).toEqual(["notify"]);
    const before = service.data();
    const version = service.version();
    expect(service.prune((id) => id !== "notify")).toEqual(["notify"]);
    expect(service.data().pins).toBeUndefined();
    expect(service.restore(before, version + 1)).toBe(true);
    expect(service.data()).toEqual(before);
    service.rename("notify", "send");
    expect(service.restore(before, version + 1)).toBe(false);
  });
  it("refuses a document over 1 MB with words", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const outcome = await service.change(
      withInput(service.data(), "é".repeat(524_288), "manual", ISO),
      ["/input"],
    );
    expect(outcome).toEqual({
      ok: false,
      message: "Test data is limited to 1 MB. Unpin some output to make room.",
    });
  });
  it("starts empty and says so when what was kept can't be read", async () => {
    const { value, store } = host();
    store.values.set(
      "local:weave.localDraft.d-1.testData",
      '{"schemaVersion":9}',
    );
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    expect(service.data()).toEqual(emptyTestData(ISO));
    expect(service.notice()).toBe(
      "Studio couldn't read the test data kept for this workflow, so it starts empty.",
    );
  });
});
