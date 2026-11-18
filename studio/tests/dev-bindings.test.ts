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
  DEV_BINDINGS_MAX_BYTES,
  DEV_BINDINGS_MAX_ENTRIES,
  DEV_BINDINGS_NAME,
  DevBindings,
  followRevision,
  readDevBindings,
  writeDevBindings,
  type DevBinding,
} from "../src/app/editor/connections/dev-bindings";
import {
  storeKey,
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
const PREFIX = "0123456789abcdef:fedcba9876543210:t:p:";
const LOCAL = "6f1c1b7e-8a43-4f0b-9b55-0f6b0d3a1c11";
const STAGING = "0b8a3f5e-2d6c-4c7a-8e1f-5a9d2c4b7e30";
const CONNECTION = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
const REVISION = "11111111-2222-4333-8444-555555555555";
const NEWER = "66666666-7777-4888-9999-aaaaaaaaaaaa";
const binding = (over: Partial<DevBinding> = {}): DevBinding => ({
  connectionId: CONNECTION,
  revisionId: REVISION,
  name: "acme-crm",
  ...over,
});

describe("dev bindings on this computer", () => {
  it("keeps a binding per environment, workflow and slot under the account's prefix", async () => {
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, PREFIX);
    await bindings.load();
    expect(await bindings.set(LOCAL, "order-intake", "crm", binding())).toBe(
      true,
    );
    expect(bindings.get(LOCAL, "order-intake", "crm")).toEqual(binding());
    expect(bindings.get(STAGING, "order-intake", "crm")).toBeNull();
    expect(bindings.get(LOCAL, "other", "crm")).toBeNull();
    expect(bindings.get(LOCAL, "order-intake", "mail")).toBeNull();
    expect([...backend.data.keys()]).toEqual([
      storeKey(PREFIX, "weave.devBindings.v1"),
    ]);
    expect(DEV_BINDINGS_NAME).toBe("weave.devBindings.v1");

    const reopened = new DevBindings(backend, PREFIX);
    await reopened.load();
    expect(reopened.get(LOCAL, "order-intake", "crm")).toEqual(binding());
  });

  it("holds IDs and names only", async () => {
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, PREFIX);
    await bindings.load();
    const extra = {
      ...binding(),
      secret: "s3cr3t-value",
      origin: "https://api.acme.test",
    } as DevBinding;
    expect(await bindings.set(LOCAL, "order-intake", "crm", extra)).toBe(true);
    const stored = backend.data.get(storeKey(PREFIX, DEV_BINDINGS_NAME))!;
    expect(stored).not.toContain("s3cr3t-value");
    expect(stored).not.toContain("api.acme.test");
    expect(bindings.get(LOCAL, "order-intake", "crm")).toEqual(binding());

    expect(
      await bindings.set(
        LOCAL,
        "order-intake",
        "crm",
        binding({ revisionId: "not-a-uuid" }),
      ),
    ).toBe(false);
    expect(
      await bindings.set(
        "local",
        "order-intake",
        "crm",
        binding({ connectionId: CONNECTION }),
      ),
    ).toBe(false);
    expect(bindings.get(LOCAL, "order-intake", "crm")).toEqual(binding());
  });

  it("a malformed or oversized document reads as empty", () => {
    expect(readDevBindings(null)).toEqual([]);
    expect(readDevBindings("{not json")).toEqual([]);
    expect(readDevBindings(JSON.stringify({ v: 1, bindings: "x" }))).toEqual(
      [],
    );
    const entry = {
      environmentId: LOCAL,
      workflow: "order-intake",
      slot: "crm",
      ...binding({ tested: { ok: false, at: "2026-11-17T10:00:00.000Z" } }),
    };
    expect(readDevBindings(writeDevBindings([entry]))).toEqual([entry]);
    const document = (row: unknown[]) =>
      JSON.stringify({
        v: 1,
        bindings: { [LOCAL]: { "order-intake": { crm: row } } },
      });
    expect(readDevBindings(document([CONNECTION, REVISION, "a"]))).toHaveLength(
      1,
    );
    expect(readDevBindings(document([CONNECTION, 7, "a"]))).toEqual([]);
    expect(
      readDevBindings(document([CONNECTION, REVISION, "a", "yes", "x"])),
    ).toEqual([]);
    expect(
      readDevBindings(document([CONNECTION, REVISION, "a", "extra"])),
    ).toEqual([]);
    expect(
      readDevBindings(
        JSON.stringify({ v: 2, bindings: JSON.parse(document([])).bindings }),
      ),
    ).toEqual([]);
    const huge = document([
      CONNECTION,
      REVISION,
      "n".repeat(DEV_BINDINGS_MAX_BYTES),
    ]);
    expect(readDevBindings(huge)).toEqual([]);
  });

  it("refuses more than 500 bindings or 64 KiB, keeping what it had", async () => {
    expect(DEV_BINDINGS_MAX_ENTRIES).toBe(500);
    expect(DEV_BINDINGS_MAX_BYTES).toBe(65_536);
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, PREFIX);
    await bindings.load();
    for (let index = 0; index < 500; index++)
      expect(await bindings.set(LOCAL, `w${index}`, "crm", binding())).toBe(
        true,
      );
    const before = backend.data.get(storeKey(PREFIX, DEV_BINDINGS_NAME));
    expect(await bindings.set(LOCAL, "w500", "crm", binding())).toBe(false);
    expect(bindings.get(LOCAL, "w500", "crm")).toBeNull();
    expect(backend.data.get(storeKey(PREFIX, DEV_BINDINGS_NAME))).toBe(before);
    // Replacing one that's there still works at the limit.
    expect(
      await bindings.set(LOCAL, "w0", "crm", binding({ name: "acme-erp" })),
    ).toBe(true);

    const small = new DevBindings(memoryBackend(), PREFIX);
    await small.load();
    expect(
      await small.set(LOCAL, "w", "crm", binding({ name: "n".repeat(70_000) })),
    ).toBe(false);
    expect(small.get(LOCAL, "w", "crm")).toBeNull();
  });

  it("without an account prefix nothing is kept on this computer", async () => {
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, null);
    await bindings.load();
    expect(await bindings.set(LOCAL, "order-intake", "crm", binding())).toBe(
      false,
    );
    expect(bindings.get(LOCAL, "order-intake", "crm")).toEqual(binding());
    expect(backend.data.size).toBe(0);
  });

  it("renaming a slot moves its binding", async () => {
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, PREFIX);
    await bindings.load();
    await bindings.set(LOCAL, "order-intake", "crm", binding());
    await bindings.set(STAGING, "order-intake", "crm", binding());
    const version = bindings.version();
    expect(
      await bindings.renameSlot(LOCAL, "order-intake", "crm", "customers"),
    ).toBe(true);
    expect(bindings.get(LOCAL, "order-intake", "crm")).toBeNull();
    expect(bindings.get(LOCAL, "order-intake", "customers")).toEqual(binding());
    expect(bindings.get(STAGING, "order-intake", "crm")).toEqual(binding());
    expect(bindings.version()).toBeGreaterThan(version);

    const reopened = new DevBindings(backend, PREFIX);
    await reopened.load();
    expect(reopened.get(LOCAL, "order-intake", "customers")).toEqual(binding());
  });

  it("clearing a binding forgets it", async () => {
    const backend = memoryBackend();
    const bindings = new DevBindings(backend, PREFIX);
    await bindings.load();
    await bindings.set(LOCAL, "order-intake", "crm", binding());
    expect(await bindings.set(LOCAL, "order-intake", "crm", null)).toBe(true);
    expect(bindings.get(LOCAL, "order-intake", "crm")).toBeNull();
    const reopened = new DevBindings(backend, PREFIX);
    await reopened.load();
    expect(reopened.get(LOCAL, "order-intake", "crm")).toBeNull();
  });

  it("a newer revision clears the test result", () => {
    const tested = binding({
      tested: { ok: true, at: "2026-11-17T10:00:00.000Z" },
    });
    expect(
      followRevision(tested, { revisionId: REVISION, name: "acme-crm" }),
    ).toBe(tested);
    expect(
      followRevision(tested, { revisionId: NEWER, name: "acme-crm" }),
    ).toEqual(binding({ revisionId: NEWER }));
    expect(
      followRevision(tested, { revisionId: REVISION, name: "acme-crm-eu" }),
    ).toEqual({ ...tested, name: "acme-crm-eu" });
  });
});
