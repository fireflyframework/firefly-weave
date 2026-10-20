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
  MirroredStorage,
  REPAIR_KEY,
  hostBackend,
  recentRepair,
} from "../src/app/editor/state/studio-storage";
import type { StoreBackend } from "../src/app/editor/state/browser-store";

/** A host store in memory, recording calls; `fail` makes writes refuse. */
function fakeHost(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  const calls: string[] = [];
  let fail = false;
  const backend: StoreBackend = {
    keys: async (prefix) =>
      [...values.keys()].filter((key) => key.startsWith(prefix)),
    get: async (key) => values.get(key) ?? null,
    set: async (key, value) => {
      calls.push(`set ${key}`);
      if (fail) return false;
      values.set(key, value);
      return true;
    },
    remove: async (key) => {
      calls.push(`remove ${key}`);
      return values.delete(key);
    },
  };
  return { values, calls, backend, refuse: () => (fail = true) };
}

describe("the desktop's storage mirror", () => {
  it("reads what the host kept, at once", async () => {
    const host = fakeHost({ "local:weave.localDrafts.v1": "[]", "ui:x": "1" });
    const mirror = new MirroredStorage();
    expect(await mirror.hydrate(host.backend)).toBe(2);
    expect(mirror.getItem("local:weave.localDrafts.v1")).toBe("[]");
    expect(mirror.length).toBe(2);
    expect([mirror.key(0), mirror.key(1)].sort()).toEqual([
      "local:weave.localDrafts.v1",
      "ui:x",
    ]);
  });
  it("answers synchronously and writes through in order, the last value of a key once", async () => {
    const host = fakeHost();
    const mirror = new MirroredStorage();
    await mirror.hydrate(host.backend);
    mirror.setItem("local:a", "1");
    mirror.setItem("local:a", "2");
    mirror.setItem("local:b", "3");
    mirror.removeItem("local:b");
    expect(mirror.getItem("local:a")).toBe("2");
    expect(await mirror.flush()).toBe(true);
    expect(host.values.get("local:a")).toBe("2");
    expect(host.values.has("local:b")).toBe(false);
    expect(
      host.calls.filter((call) => call === "set local:a").length,
    ).toBeLessThanOrEqual(2);
  });
  it("says when the host refused a write", async () => {
    const host = fakeHost();
    const mirror = new MirroredStorage();
    await mirror.hydrate(host.backend);
    host.refuse();
    mirror.setItem("local:a", "1");
    expect(await mirror.flush()).toBe(false);
    expect(mirror.failed).toBe(true);
  });
});

describe("the host store backend", () => {
  it("uses the four routes and reads refusals as empty", async () => {
    const seen: string[] = [];
    const api = {
      request: async <T>(
        path: string,
        method = "GET",
        body?: unknown,
      ): Promise<T> => {
        seen.push(
          `${method} ${path}${body === undefined ? "" : ` ${JSON.stringify(body)}`}`,
        );
        if (path === "/studio/local/store?prefix=local%3A")
          return { keys: ["local:a"] } as T;
        if (path === "/studio/local/store/local%3Aa" && method === "GET")
          return { value: "1" } as T;
        if (path.endsWith("missing"))
          throw Object.assign(new Error("Nothing"), { status: 404 });
        return undefined as T;
      },
    };
    const backend = hostBackend(api);
    expect(await backend.keys("local:")).toEqual(["local:a"]);
    expect(await backend.get("local:a")).toBe("1");
    expect(await backend.get("local:missing")).toBeNull();
    expect(await backend.set("local:a", "2")).toBe(true);
    expect(await backend.remove("local:a")).toBe(true);
    expect(seen).toContain('PUT /studio/local/store/local%3Aa {"value":"2"}');
    expect(seen).toContain("DELETE /studio/local/store/local%3Aa");
  });
});

describe("a platform signed out", () => {
  it("has its keys dropped from the mirror, not kept and not reported as a failure", async () => {
    const api = {
      request: async <T>(path: string, method = "GET"): Promise<T> => {
        if (method === "PUT" && path.includes("p0123456789abcdef"))
          throw Object.assign(new Error("Signed out"), {
            status: 409,
            code: "WV-STUDIO-STORE-SIGNED-OUT",
          });
        if (method === "GET") return { keys: [] } as T;
        return undefined as T;
      },
    };
    const mirror = new MirroredStorage();
    await mirror.hydrate(hostBackend(api, (key) => mirror.drop(key)));
    mirror.setItem("p0123456789abcdef:s:t:p:testData.d-1", "{}");
    mirror.setItem("local:a", "1");
    expect(await mirror.flush()).toBe(true);
    expect(mirror.failed).toBe(false);
    expect(mirror.getItem("p0123456789abcdef:s:t:p:testData.d-1")).toBeNull();
    expect(mirror.getItem("local:a")).toBe("1");
  });
  it("says which keys it dropped, so Studio can tell the person", async () => {
    const api = {
      request: async <T>(path: string, method = "GET"): Promise<T> => {
        if (method === "PUT")
          throw Object.assign(new Error("Signed out"), {
            status: 409,
            code: "WV-STUDIO-STORE-SIGNED-OUT",
          });
        if (method === "GET") return { keys: [] } as T;
        return undefined as T;
      },
    };
    const mirror = new MirroredStorage();
    const dropped: string[] = [];
    const stop = mirror.onDropped((key) => dropped.push(key));
    await mirror.hydrate(hostBackend(api, (key) => mirror.drop(key)));
    mirror.setItem("p0123456789abcdef:s:t:p:recovery.d-1", "{}");
    await mirror.flush();
    expect(dropped).toEqual(["p0123456789abcdef:s:t:p:recovery.d-1"]);
    stop();
    mirror.setItem("p0123456789abcdef:s:t:p:recovery.d-2", "{}");
    await mirror.flush();
    expect(dropped).toHaveLength(1);
  });
  it("never reads a store failure as a sign-out", async () => {
    const signedOut: string[] = [];
    for (const [status, code] of [
      [413, "WV-STUDIO-STORE-FULL"],
      [500, "WV-STUDIO-STORE"],
    ] as const) {
      const api = {
        request: async <T>(): Promise<T> => {
          throw Object.assign(new Error("No"), { status, code });
        },
      };
      const backend = hostBackend(api, (key) => signedOut.push(key));
      expect(
        await backend.set("p0123456789abcdef:s:t:p:canvas.d-1", "{}"),
      ).toBe(false);
    }
    expect(signedOut).toEqual([]);
  });
});

describe("the desktop's re-pairing reload", () => {
  it("is kept under a prefixed key, and happens at most once a minute", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => void values.set(key, value),
    };
    expect(REPAIR_KEY).toMatch(/^(local:|ui:|p[0-9a-f]{16}:)/);
    expect(recentRepair(storage, 1_000_000)).toBe(false);
    expect([...values.keys()]).toEqual([REPAIR_KEY]);
    expect(recentRepair(storage, 1_030_000)).toBe(true);
    expect(recentRepair(storage, 1_061_000)).toBe(false);
  });
});
