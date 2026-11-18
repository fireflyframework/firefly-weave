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
// The project's catalog lock, read once per account and environment: a read
// that fails waits before it is asked again, an answer for an account or
// environment Studio has left is dropped, and a refusal says the person can't
// read the published workflows.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { computed } from "@angular/core";
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import {
  CatalogLockReader,
  LOCK_RETRY_MAX_MS,
  LOCK_RETRY_MS,
  parseCatalogLock,
  type CatalogLockHost,
} from "../src/app/editor/calls/catalog-lock";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/callable-corpus.json"),
    "utf8",
  ),
) as { lists: { lock: unknown }[] };
const LOCK = corpus.lists[0].lock;

interface Pending {
  resolve(value: unknown): void;
  reject(error: unknown): void;
}

/** A reader whose platform answers at once, or holds its answers until the test settles them. */
function reader(answer: () => unknown = () => LOCK) {
  const host = { scope: "acme/local" as string | null, canRead: true };
  const fetched: string[] = [];
  const held: Pending[] = [];
  let hold = false;
  let now = 1_000_000;
  const lockHost: CatalogLockHost = {
    scope: () => host.scope,
    canRead: () => host.canRead,
    fetch: () => {
      fetched.push(host.scope ?? "");
      if (hold)
        return new Promise((resolve, reject) => held.push({ resolve, reject }));
      const value = answer();
      return value instanceof Error
        ? Promise.reject(value)
        : Promise.resolve(value);
    },
  };
  const made = new CatalogLockReader(lockHost, () => now);
  return {
    reader: made,
    host,
    fetched,
    held,
    holdAnswers: () => (hold = true),
    advance: (ms: number) => (now += ms),
  };
}
const settle = () => new Promise((done) => setTimeout(done, 0));
const failure = () => new ApiError(503, { code: "WV-UNAVAILABLE" });

describe("the catalog lock", () => {
  it("keeps the project's workflow versions by reference and nothing else", () => {
    const lock = parseCatalogLock(LOCK);
    expect([...lock!.workflows.keys()].sort()).toEqual([
      "customer-profile@2.0.0",
      "notify-customer@1.0.0",
      "notify-customer@1.10.0",
      "notify-customer@1.9.0",
      "notify-customer@2.0.0-rc.1",
      "order-intake@1.1.0",
    ]);
    const notify = lock!.workflows.get("notify-customer@1.10.0")!;
    expect(notify.name).toBe("notify-customer");
    expect(notify.version).toBe("1.10.0");
    expect(notify.document["spec"]).toMatchObject({
      callable: { allowedCallers: ["order-intake"] },
    });
  });

  it("refuses an answer that isn't a catalog lock", () => {
    expect(parseCatalogLock(null)).toBeNull();
    expect(parseCatalogLock({ definitions: {} })).toBeNull();
    expect(parseCatalogLock([LOCK])).toBeNull();
  });

  it("is read once per account and environment", async () => {
    const { reader: lock, fetched } = reader();
    expect(lock.current()).toEqual({ kind: "loading" });
    lock.current();
    const answers = await Promise.all([lock.read(), lock.read()]);
    lock.current();
    await lock.read();
    expect(fetched).toEqual(["acme/local"]);
    expect(answers[0].kind).toBe("ready");
    const state = lock.current();
    expect(state.kind === "ready" && state.lock.workflows.size).toBe(6);
  });

  it("waits 5 s after a failed read, doubling to a minute while it keeps failing", async () => {
    const { reader: lock, fetched, advance } = reader(failure);
    const pauses: number[] = [];
    let waited = 0;
    lock.current();
    await settle();
    while (pauses.length < 6) {
      expect(lock.current()).toEqual({
        kind: "failed",
        message:
          "The platform could not be reached. Check your network connection and try again.",
      });
      const before = fetched.length;
      advance(1_000);
      waited += 1_000;
      lock.current();
      await lock.read();
      if (fetched.length > before) {
        pauses.push(waited);
        waited = 0;
      }
    }
    expect(pauses).toEqual([
      LOCK_RETRY_MS,
      2 * LOCK_RETRY_MS,
      4 * LOCK_RETRY_MS,
      8 * LOCK_RETRY_MS,
      LOCK_RETRY_MAX_MS,
      LOCK_RETRY_MAX_MS,
    ]);
    expect([LOCK_RETRY_MS, LOCK_RETRY_MAX_MS]).toEqual([5_000, 60_000]);
  });

  it("drops an answer that arrives after the account or environment changed", async () => {
    const { reader: lock, host, fetched, held, holdAnswers } = reader();
    holdAnswers();
    const first = lock.read();
    host.scope = "acme/production";
    held[0].resolve(LOCK);
    await first;
    expect(lock.current()).toEqual({ kind: "loading" });
    // Back on the first environment, its dropped answer isn't used: it reads again.
    host.scope = "acme/local";
    expect(lock.current()).toEqual({ kind: "loading" });
    expect(fetched).toEqual(["acme/local", "acme/production", "acme/local"]);
    // The production answer arrives late too, and changes nothing here.
    held[1].resolve(LOCK);
    await settle();
    expect(lock.current()).toEqual({ kind: "loading" });
    held[2].resolve({ definitions: [], tasks: [], adapters: [], schemas: {} });
    await settle();
    const state = lock.current();
    expect(state.kind === "ready" && state.lock.workflows.size).toBe(0);
  });

  it("drops a failure that arrives after the account changed", async () => {
    const { reader: lock, host, fetched, held, holdAnswers } = reader();
    holdAnswers();
    const first = lock.read();
    host.scope = "other/local";
    held[0].reject(failure());
    await first;
    lock.current();
    expect(fetched).toEqual(["acme/local", "other/local"]);
  });

  it("answers can't read when the platform refuses the read, and doesn't ask again", async () => {
    const {
      reader: lock,
      fetched,
      advance,
    } = reader(
      () => new ApiError(403, { code: "WV-DENIED", message: "denied" }),
    );
    expect(await lock.read()).toEqual({ kind: "forbidden" });
    advance(10 * LOCK_RETRY_MAX_MS);
    expect(lock.current()).toEqual({ kind: "forbidden" });
    expect(fetched).toHaveLength(1);
  });

  it("asks nothing without catalog read or a platform", async () => {
    const { reader: lock, host, fetched } = reader();
    host.canRead = false;
    expect(lock.current()).toEqual({ kind: "forbidden" });
    expect(await lock.read()).toEqual({ kind: "forbidden" });
    host.canRead = true;
    host.scope = null;
    expect(lock.current()).toEqual({ kind: "offline" });
    expect(await lock.read()).toEqual({ kind: "offline" });
    expect(fetched).toEqual([]);
  });

  it("treats an answer that isn't a catalog lock as a failed read", async () => {
    const { reader: lock, fetched } = reader(() => ({ definitions: "none" }));
    expect(await lock.read()).toEqual({
      kind: "failed",
      message: "Studio couldn't read this project's published workflows.",
    });
    lock.current();
    expect(fetched).toHaveLength(1);
  });

  it("reads again on Refresh, even during a pause", async () => {
    let fail = true;
    const { reader: lock, fetched } = reader(() => (fail ? failure() : LOCK));
    await lock.read();
    fail = false;
    expect((await lock.refresh()).kind).toBe("ready");
    expect((await lock.refresh()).kind).toBe("ready");
    expect(fetched).toHaveLength(3);
  });

  it("can be read from a computed, which follows the answer", async () => {
    const { reader: lock } = reader();
    const kind = computed(() => lock.current().kind);
    expect(kind()).toBe("loading");
    await settle();
    expect(kind()).toBe("ready");
  });

  it("can be read from a computed when the platform can't be reached at once", async () => {
    const broken = new CatalogLockReader(
      {
        scope: () => "acme/local",
        canRead: () => true,
        fetch: () => {
          throw new TypeError("Failed to fetch");
        },
      },
      () => 0,
    );
    const kind = computed(() => broken.current().kind);
    expect(kind()).toBe("loading");
    await settle();
    expect(kind()).toBe("failed");
  });
});
