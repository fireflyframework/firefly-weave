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
// Runs the desktop shell's pairing script (embedded in the Rust shell) against
// fakes, so its behavior is checked by a JavaScript engine rather than by
// matching its text. pairing_script() in main.rs fills the same placeholders.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { describe, expect, it } from "vitest";

const ORIGIN = "http://127.0.0.1:32199";
const CODE = "a23456789012345678901234567890123";
const KEY = "weave-desktop-pairing";

function pairingScript(): string {
  const source = readFileSync(
    new URL("../../desktop/src-tauri/src/main.rs", import.meta.url),
    "utf8",
  );
  const template = /const PAIRING_SCRIPT: &str = r#"([\s\S]*?)"#;/.exec(
    source,
  )?.[1];
  if (!template) throw new Error("PAIRING_SCRIPT not found in main.rs");
  return template
    .replace("__WEAVE_ORIGIN__", JSON.stringify(ORIGIN))
    .replace("__WEAVE_BODY__", JSON.stringify(JSON.stringify({ code: CODE })));
}

interface Reply {
  ok: boolean;
  body?: unknown;
}
interface Options {
  origin?: string;
  top?: boolean;
  get?: Reply;
  post?: Reply;
  storage?: Storage | MemoryStorage | typeof throwing;
  navigation?: string;
  clock?: number;
  hostile?: boolean;
}
interface Call {
  path: string;
  method: string;
  body?: string;
}

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.values.set(key, String(value));
  }
}
const throwing = {
  getItem(): never {
    throw new Error("denied");
  },
  setItem(): never {
    throw new Error("denied");
  },
};

async function run(options: Options = {}) {
  const {
    origin = ORIGIN,
    top = true,
    get = { ok: true, body: { paired: false } },
    post = { ok: true },
    storage = new MemoryStorage(),
    navigation = "navigate",
    clock = 1_000_000,
    hostile = false,
  } = options;
  const calls: Call[] = [];
  const observed: string[] = [];
  let reloads = 0;
  // Page code can replace globals and wrap promise callbacks after the
  // document starts; a hostile page records everything it can reach.
  const reach = <T>(promise: Promise<T>): Promise<T> => {
    if (!hostile) return promise;
    const spy = {
      then(ok?: unknown, fail?: unknown) {
        observed.push(String(ok), String(fail));
        return reach(promise.then(ok as never, fail as never));
      },
      catch(fail?: unknown) {
        observed.push(String(fail));
        return reach(promise.catch(fail as never));
      },
    };
    return spy as unknown as Promise<T>;
  };
  const page: Record<string, unknown> = {};
  page.top = top ? page : {};
  page.fetch = (path: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    calls.push({ path, method, body: init.body as string | undefined });
    const reply = method === "POST" ? post : get;
    return reach(
      Promise.resolve({ ok: reply.ok, json: async () => reply.body }),
    );
  };
  const json = {
    parse: JSON.parse,
    stringify(value: unknown) {
      const text = JSON.stringify(value);
      if (hostile) observed.push(text);
      return text;
    },
  };
  // The shell always embeds its own origin; `origin` is where the page is.
  vm.runInNewContext(pairingScript(), {
    window: page,
    location: { origin, reload: () => reloads++ },
    sessionStorage: storage,
    performance: { getEntriesByType: () => [{ type: navigation }] },
    Date: { now: () => clock },
    JSON: json,
  });
  for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 0));
  return { calls, reloads, observed };
}

describe("desktop pairing script", () => {
  it("pairs an unpaired window once and reloads", async () => {
    const storage = new MemoryStorage();
    const { calls, reloads } = await run({ storage });
    expect(calls).toEqual([
      { path: "/studio/session", method: "GET", body: undefined },
      {
        path: "/studio/session",
        method: "POST",
        body: JSON.stringify({ code: CODE }),
      },
    ]);
    expect(reloads).toBe(1);
    const stored = storage.getItem(KEY) ?? "";
    expect(JSON.parse(stored)).toEqual([1_000_000]);
    expect(stored).not.toContain(CODE);
  });

  it("leaves a paired window alone", async () => {
    const { calls, reloads } = await run({
      get: { ok: true, body: { paired: true } },
    });
    expect(calls.map((c) => c.method)).toEqual(["GET"]);
    expect(reloads).toBe(0);
  });

  it("never reloads after a refused code or a failed session read", async () => {
    const storage = new MemoryStorage();
    const refused = await run({ storage, post: { ok: false } });
    expect(refused.calls.map((c) => c.method)).toEqual(["GET", "POST"]);
    expect(refused.reloads).toBe(0);
    expect(storage.getItem(KEY)).toBeNull();
    const unreadable = await run({ get: { ok: false } });
    expect(unreadable.calls.map((c) => c.method)).toEqual(["GET"]);
    expect(unreadable.reloads).toBe(0);
  });

  it("does nothing in another origin or a frame", async () => {
    expect((await run({ origin: "http://127.0.0.1:1" })).calls).toEqual([]);
    expect((await run({ top: false })).calls).toEqual([]);
  });

  it("bounds automatic reloads when the session cookie never sticks", async () => {
    const storage = new MemoryStorage();
    let reloads = 0;
    let posts = 0;
    for (let i = 0; i < 10; i++) {
      const result = await run({ storage, clock: 1_000_000 + i * 1000 });
      reloads += result.reloads;
      posts += result.calls.filter((c) => c.method === "POST").length;
    }
    expect([reloads, posts]).toEqual([3, 3]);
    expect((await run({ storage, clock: 1_061_000 })).reloads).toBe(1);
  });

  it("reloads once per navigation the person started without storage", async () => {
    expect((await run({ storage: throwing })).reloads).toBe(1);
    const again = await run({ storage: throwing, navigation: "reload" });
    expect(again.reloads).toBe(0);
    expect(again.calls.map((c) => c.method)).toEqual(["GET", "POST"]);
  });

  it("ignores malformed or future timestamps", async () => {
    const malformed = new MemoryStorage();
    malformed.setItem(KEY, '{"not":"array"}');
    expect((await run({ storage: malformed })).reloads).toBe(1);
    const future = new MemoryStorage();
    future.setItem(KEY, JSON.stringify([2_000_000, 2_000_000, 2_000_000]));
    expect((await run({ storage: future })).reloads).toBe(1);
  });

  it("keeps the code out of reach of page scripts", async () => {
    const { calls, reloads, observed } = await run({ hostile: true });
    expect(reloads).toBe(1);
    expect(calls[1].body).toBe(JSON.stringify({ code: CODE }));
    // Replaced globals and wrapped callbacks (including their source text)
    // never contain the code; only the request body does.
    expect(observed.length).toBeGreaterThan(0);
    for (const text of observed) expect(text).not.toContain(CODE);
  });
});
