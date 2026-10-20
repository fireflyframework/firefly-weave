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

describe("test data across draft switches and sign-outs", () => {
  const SCOPE = {
    profileName: "Acme",
    subject: "user-1",
    tenantId: "7b0c1c43-3f5a-4cde-9f0e-3c2d5a6b7c8d",
    projectId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
  };
  it("keeps the previous draft's waiting change when another draft opens", async () => {
    const { value, store } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(withInput(service.data(), 1, "manual", ISO), [
      "/input",
    ]);
    // Opened before the 800 ms keep: the change still lands next to d-1.
    await service.open({ kind: "local", draftId: "d-2" });
    expect(
      JSON.parse(store.values.get("local:weave.localDraft.d-1.testData")!).input
        .value,
    ).toBe(1);
    expect(store.values.has("local:weave.localDraft.d-2.testData")).toBe(false);
  });
  it("lets the last opening win when two overlap", async () => {
    const { value, store } = host();
    let release!: () => void;
    const slow = new Promise<void>((done) => (release = done));
    const get = store.backend.get;
    store.backend.get = async (key) => {
      if (key.includes("d-1")) await slow;
      return get(key);
    };
    store.values.set(
      "local:weave.localDraft.d-1.testData",
      JSON.stringify(withInput(emptyTestData(ISO), "one", "manual", ISO)),
    );
    const service = new TestDataService(value);
    const first = service.open({ kind: "local", draftId: "d-1" });
    await service.open({ kind: "local", draftId: "d-2" });
    release();
    await first;
    expect(service.place()).toEqual({ kind: "local", draftId: "d-2" });
    expect(service.data().input).toBeUndefined();
  });
  it("refuses a change checked while another draft opened", async () => {
    const { value, store } = host();
    let release!: () => void;
    const held = new Promise<void>((done) => (release = done));
    const request = value.api.request;
    value.api = {
      request: async <T>(...args: Parameters<typeof request>): Promise<T> => {
        await held;
        return request<T>(...args);
      },
    };
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const change = service.change(
      withInput(service.data(), "for d-1", "manual", ISO),
      ["/input"],
    );
    await service.open({ kind: "local", draftId: "d-2" });
    release();
    expect((await change).ok).toBe(false);
    await service.flush();
    expect(service.data().input).toBeUndefined();
    expect(store.values.size).toBe(0);
  });
  it("forgets a signed-out account at once: nothing waiting is written", async () => {
    const { value, store } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    await service.flush();
    await service.change(withInput(service.data(), "late", "manual", ISO), [
      "/input",
    ]);
    service.forget();
    await new Promise((done) => setTimeout(done, 900));
    await service.flush();
    expect(
      [...store.values.keys()].filter((key) => key.startsWith("p")),
    ).toEqual([]);
    expect(service.place()).toEqual({ kind: "memory" });
    expect(service.data().input).toBeUndefined();
    // Undo after the sign-out keeps nothing either.
    service.restore(
      withInput(emptyTestData(ISO), "undo", "manual", ISO),
      service.version(),
    );
    await service.flush();
    expect(
      [...store.values.keys()].filter((key) => key.startsWith("p")),
    ).toEqual([]);
  });
  it("drops a write already queued for the signed-out account", async () => {
    const { value, store } = host();
    let release!: () => void;
    const held = new Promise<void>((done) => (release = done));
    const set = store.backend.set;
    let first = true;
    store.backend.set = async (key, raw) => {
      if (first) {
        first = false;
        await held;
      }
      return set(key, raw);
    };
    const service = new TestDataService(value);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    await service.change(withInput(service.data(), 1, "manual", ISO), [
      "/input",
    ]);
    const flushing = service.flush();
    await service.change(withInput(service.data(), 2, "manual", ISO), [
      "/input",
    ]);
    const second = service.flush();
    service.forget();
    release();
    await flushing;
    await second;
    const kept = [...store.values.entries()].filter(([key]) =>
      key.startsWith("p"),
    );
    // The write that had started lands; the one only queued doesn't.
    expect(kept.map(([, raw]) => JSON.parse(raw).input.value)).toEqual([1]);
  });
});

describe("test data of deleted steps", () => {
  it("comes back when the steps come back, and goes again when they go", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    const data = withPin(
      withPin(emptyTestData(ISO), "lookup", pin({ id: 1 }), ISO),
      "score",
      pin({ tier: "gold" }),
      ISO,
    );
    await service.change(data, []);
    let steps = new Set(["score"]);
    expect(service.prune((id) => steps.has(id))).toEqual(["lookup"]);
    expect(Object.keys(service.data().pins ?? {})).toEqual(["score"]);
    // Undo brings the step back: its pin follows.
    steps = new Set(["score", "lookup"]);
    expect(await service.revive((id) => steps.has(id))).toEqual([
      "/pins/lookup",
    ]);
    expect(service.data().pins?.["lookup"]).toEqual(pin({ id: 1 }));
    // Redo deletes it again; a second revive finds nothing more to bring.
    steps = new Set(["score"]);
    service.prune((id) => steps.has(id));
    expect(service.data().pins?.["lookup"]).toBeUndefined();
    steps = new Set(["score", "lookup"]);
    await service.revive((id) => steps.has(id));
    expect(await service.revive((id) => steps.has(id))).toEqual([]);
    expect(service.data().pins?.["lookup"]).toEqual(pin({ id: 1 }));
  });
  it("never replaces a pin set since, and forgets the deleted on another opening", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "lookup", pin({ id: 1 }), ISO),
      [],
    );
    service.prune(() => false);
    await service.change(
      withPin(service.data(), "lookup", pin({ id: 2 }), ISO),
      [],
    );
    expect(await service.revive(() => true)).toEqual([]);
    expect(service.data().pins?.["lookup"]).toEqual(pin({ id: 2 }));
    service.prune(() => false);
    await service.open({ kind: "local", draftId: "d-2" });
    expect(await service.revive(() => true)).toEqual([]);
  });
});

describe("reviving a deleted step's test data", () => {
  it("brings it back only to the same step, never to another that took its ID", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "notify", pin({ id: 1 }), ISO),
      [],
    );
    service.prune(
      () => false,
      () => "notify as it was",
    );
    // Another step now has the ID: nothing comes back.
    expect(
      await service.revive(
        () => true,
        () => "lookup renamed",
      ),
    ).toEqual([]);
    expect(service.data().pins?.["notify"]).toBeUndefined();
    // That entry is gone for good: another step took the ID.
    expect(
      await service.revive(
        () => true,
        () => "notify as it was",
      ),
    ).toEqual([]);
    // Deleted again, then the same step as it was comes back: so does its pin.
    await service.change(
      withPin(emptyTestData(ISO), "notify", pin({ id: 2 }), ISO),
      [],
    );
    service.prune(
      () => false,
      () => "notify as it was",
    );
    expect(
      await service.revive(
        () => true,
        () => "notify as it was",
      ),
    ).toEqual(["/pins/notify"]);
  });
  it("checks what it brings back, as any change: a value now secret stays out", async () => {
    const { value } = host({ "/pins/notify": "WV-SCHEMA-SECRET_VALUE" });
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "notify", pin({ token: "x" }), ISO),
      [],
    );
    service.prune(() => false);
    expect(await service.revive(() => true)).toEqual([]);
    expect(service.data().pins?.["notify"]).toBeUndefined();
  });
  it("forgets what deleted steps held once the platform's copy replaces this one", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "notify", pin({ id: 1 }), ISO),
      [],
    );
    service.prune(() => false);
    service.syncFrom({
      draft_id: "d-1",
      kind: "test-data",
      revision: 2,
      draft_revision: 1,
      document: emptyTestData(ISO),
      contains_run_data: false,
      source_environment_ids: [],
      updated_at: ISO,
      updated_by: "someone",
    } as never);
    expect(await service.revive(() => true)).toEqual([]);
  });
});

describe("test data set before the account is known", () => {
  const SCOPE = {
    profileName: "P",
    subject: "human",
    tenantId: "7b0c1c43-3f5a-4cde-9f0e-3c2d5a6b7c8d",
    projectId: "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718",
  };
  it("is carried into the account's copy when the account arrives, checked and kept", async () => {
    const { value, store } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "memory" });
    const typed = withInput(
      emptyTestData(ISO),
      { customerId: "typed-early" },
      "manual",
      ISO,
    );
    expect((await service.change(typed, ["/input"])).ok).toBe(true);
    await service.open(
      { kind: "connected", scope: SCOPE, draftId: "d-1" },
      { carry: true },
    );
    const asked: string[][] = [];
    expect(
      await service.carryOver(async (paths) => (asked.push(paths), true)),
    ).toEqual(["/input"]);
    expect(asked).toEqual([]);
    expect(service.data().input?.value).toEqual({ customerId: "typed-early" });
    await service.flush();
    expect(
      [...store.values.values()].some((raw) => raw.includes("typed-early")),
    ).toBe(true);
  });
  it("asks only when the stored copy holds a different value, and keeps the stored one when told to", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    // The account already keeps a pin for lookup.
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "lookup", pin({ id: "stored" }), ISO),
      [],
    );
    await service.flush();
    await service.open({ kind: "memory" });
    await service.change(
      withPin(
        withInput(emptyTestData(ISO), 1, "manual", ISO),
        "lookup",
        pin({ id: "typed" }),
        ISO,
      ),
      [],
    );
    await service.open(
      { kind: "connected", scope: SCOPE, draftId: "d-1" },
      { carry: true },
    );
    const asked: string[][] = [];
    expect(
      await service.carryOver(async (paths) => (asked.push(paths), false)),
    ).toEqual(["/input"]);
    expect(asked).toEqual([["/pins/lookup"]]);
    expect(service.data().pins?.["lookup"]?.output).toEqual({ id: "stored" });
    expect(service.data().input?.value).toBe(1);
  });
  it("carries nothing from another workflow (no carry asked)", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "memory" });
    await service.change(withInput(emptyTestData(ISO), 1, "manual", ISO), [
      "/input",
    ]);
    await service.open({ kind: "connected", scope: SCOPE, draftId: "d-2" });
    expect(await service.carryOver(async () => true)).toEqual([]);
    expect(service.data().input).toBeUndefined();
  });
  it("forgets what deleted steps held when the account signs out", async () => {
    const { value } = host();
    const service = new TestDataService(value);
    await service.open({ kind: "local", draftId: "d-1" });
    await service.change(
      withPin(emptyTestData(ISO), "notify", pin({ id: 1 }), ISO),
      [],
    );
    service.prune(() => false);
    service.forget();
    // An Undo after the sign-out brings nothing of the old account back.
    expect(await service.revive(() => true)).toEqual([]);
    expect(service.data().pins).toBeUndefined();
  });
});
