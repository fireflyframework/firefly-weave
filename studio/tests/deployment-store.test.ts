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
import { OperationsStore } from "../src/app/operations/deployment-store";

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const page = (name: string) => ({
  items: [{ id: name, name }],
  next_cursor: null,
});
describe("Operations scoped transport", () => {
  it("discards an old environment response and clears prior target data", async () => {
    const old = pending<unknown>();
    const store = new OperationsStore({
      request: async <T>(path: string) =>
        (path.startsWith("/first/") ? await old.promise : page("second")) as T,
    });
    store.setScope("/first", true);
    const loading = store.load("targets");
    store.setScope("/second", true);
    await store.load("targets");
    old.resolve(page("first"));
    await loading;
    expect(store.targets.map((t) => t.name)).toEqual(["second"]);
    store.setScope("", false);
    expect(store.targets).toEqual([]);
  });
  it("clears state when authority changes in the same environment", async () => {
    const store = new OperationsStore({
      request: async <T>() => page("prior-user") as T,
    });
    store.setScope("/same", true, "principal-a");
    await store.load("targets");
    store.setScope("/same", true, "principal-b");
    expect(store.targets).toEqual([]);
  });
  it("clears old target observations immediately when the filter changes", async () => {
    const next = pending<unknown>();
    let calls = 0;
    const store = new OperationsStore({
      request: async <T>() =>
        (++calls === 1 ? page("old") : await next.promise) as T,
    });
    store.setScope("/same", true);
    await store.load("observations", false, { target_id: "first" });
    const loading = store.load("observations", false, { target_id: "second" });
    expect(store.observations).toEqual([]);
    next.resolve(page("new"));
    await loading;
  });
  it("says a collection has loaded only for the filter it loaded with", async () => {
    const next = pending<unknown>();
    let calls = 0;
    const store = new OperationsStore({
      request: async <T>() =>
        (++calls === 1 ? page("all") : await next.promise) as T,
    });
    store.setScope("/same", true);
    expect(store.loaded("runners")).toBe(false);
    await store.load("runners");
    expect(store.loaded("runners")).toBe(true);
    expect(store.loaded("runners", { target_id: "first" })).toBe(false);
    // A different filter empties the page: nothing is known until it loads.
    const loading = store.load("runners", false, { target_id: "first" });
    expect(store.loaded("runners")).toBe(false);
    expect(store.loaded("runners", { target_id: "first" })).toBe(false);
    next.resolve(page("one"));
    await loading;
    expect(store.loaded("runners", { target_id: "first" })).toBe(true);
    expect(store.loaded("runners")).toBe(false);
    store.setScope("/other", true);
    expect(store.loaded("runners", { target_id: "first" })).toBe(false);
  });
  it("keeps what a failed read says, and no claim of having loaded", async () => {
    let fail = true;
    const store = new OperationsStore({
      request: async <T>() => {
        if (fail)
          throw {
            plain: {
              message: "Denied here",
              code: "WV-DENIED",
              status: 403,
            },
          };
        return page("ok") as T;
      },
    });
    store.setScope("/same", true);
    await store.load("runners");
    expect(store.errors.runners).toBe("Denied here");
    expect(store.errorInfo.runners).toEqual({ code: "WV-DENIED", status: 403 });
    expect(store.loaded("runners")).toBe(false);
    fail = false;
    const loading = store.load("runners");
    expect(store.errors.runners).toBeUndefined();
    expect(store.errorInfo.runners).toBeUndefined();
    await loading;
    expect(store.loaded("runners")).toBe(true);
    // A failed refresh empties the rows, so it stops claiming to have them.
    fail = true;
    await store.load("runners");
    expect(store.runners).toEqual([]);
    expect(store.loaded("runners")).toBe(false);
  });
  it("does not request a collection without read authority", async () => {
    let calls = 0;
    const store = new OperationsStore({
      request: async <T>() => {
        calls++;
        return page("leak") as T;
      },
    });
    store.setScope("/scope", false);
    await store.load("targets");
    expect(calls).toBe(0);
    expect(store.targets).toEqual([]);
  });
  it("preserves incomplete pagination and sends an encoded scoped cursor", async () => {
    const paths: string[] = [];
    const store = new OperationsStore({
      request: async <T>(path: string) => {
        paths.push(path);
        return {
          items: [{ id: paths.length, name: "target" }],
          next_cursor: paths.length === 1 ? "opaque+/=" : null,
        } as T;
      },
    });
    store.setScope("/scope", true);
    await store.load("targets");
    expect(store.cursors.targets).toBe("opaque+/=");
    await store.load("targets", true);
    expect(paths[1]).toBe(
      "/scope/deployment-targets?limit=50&cursor=opaque%2B%2F%3D",
    );
    expect(store.targets).toHaveLength(2);
  });
  it("does not retry an ambiguous mutation or publish its old-scope result", async () => {
    let calls = 0;
    const result = pending<unknown>();
    const store = new OperationsStore({
      request: async <T>() => {
        calls++;
        return (await result.promise) as T;
      },
    });
    store.setScope("/first", true);
    const saving = store.mutate("deployment-targets", { name: "owned" });
    store.setScope("/second", true);
    result.resolve({ id: "old-target" });
    expect(await saving).toBeNull();
    expect(calls).toBe(1);
    expect(store.mutating).toBe(false);
  });
  it("sends exact reviewed plan digest with a stable request key and no blind retry", async () => {
    const requests: unknown[][] = [];
    const store = new OperationsStore({
      request: async <T>(...args: unknown[]) => {
        requests.push(args);
        throw Error("Request outcome unavailable");
      },
    });
    store.setScope("/scope", true);
    const digest = "a".repeat(64);
    expect(
      await store.mutate(
        "deployment-plans/plan/apply",
        { digest },
        "POST",
        undefined,
        "fixed-key",
      ),
    ).toBeNull();
    expect(requests).toHaveLength(1);
    expect(requests[0]).toEqual([
      "/scope/deployment-plans/plan/apply",
      "POST",
      { digest },
      { "Idempotency-Key": "fixed-key" },
    ]);
    expect(store.mutationError).toContain("Request outcome unavailable");
  });
  it.each(["deployments", "deployment-plans/plan/apply"])(
    "recovers a lost successful %s response only on explicit retry",
    async (path) => {
      const saved = new Map<string, { id: string }>();
      const keys: string[] = [];
      let lose = true;
      const store = new OperationsStore({
        request: async <T>(_path, _method, _body, headers) => {
          const key = headers!["Idempotency-Key"];
          keys.push(key);
          if (!saved.has(key)) saved.set(key, { id: "resource-" + saved.size });
          if (lose) {
            lose = false;
            throw new TypeError("Failed to fetch");
          }
          return saved.get(key) as T;
        },
      });
      store.setScope("/scope", true, "principal");
      expect(
        await store.mutate(path, { name: "owned", nested: { b: 2, a: 1 } }),
      ).toBeNull();
      expect(keys).toHaveLength(1);
      expect(
        store.canRecover(path, { nested: { a: 1, b: 2 }, name: "owned" }),
      ).toBe(true);
      expect(store.canRecover(path, { name: "changed" })).toBe(false);
      expect(
        await store.mutate(path, { nested: { a: 1, b: 2 }, name: "owned" }),
      ).toEqual({ id: "resource-0" });
      expect(keys[1]).toBe(keys[0]);
      expect(saved.size).toBe(1);
      await store.mutate(path, { name: "owned", nested: { a: 1, b: 2 } });
      expect(keys[2]).not.toBe(keys[1]);
    },
  );
  it("changes retry identity after revision, cancellation, authority or definitive rejection", async () => {
    const keys: string[] = [];
    let failure: unknown = new TypeError("Network error");
    const store = new OperationsStore({
      request: async <T>(_path, _method, _body, headers) => {
        keys.push(headers!["Idempotency-Key"]);
        throw failure;
      },
    });
    store.setScope("/scope", true, "a");
    await store.mutate("deployment-targets/id", {}, "PUT", 1);
    await store.mutate("deployment-targets/id", {}, "PUT", 2);
    expect(keys[1]).not.toBe(keys[0]);
    store.cancelMutation();
    await store.mutate("deployment-targets/id", {}, "PUT", 2);
    expect(keys[2]).not.toBe(keys[1]);
    store.setScope("/scope", true, "b");
    await store.mutate("deployment-targets/id", {}, "PUT", 2);
    expect(keys[3]).not.toBe(keys[2]);
    failure = {
      plain: { message: "Changed", status: 409, code: "WV-REVISION" },
    };
    await store.mutate("deployment-targets/id", {}, "PUT", 2);
    expect(keys[4]).toBe(keys[3]);
    await store.mutate("deployment-targets/id", {}, "PUT", 2);
    expect(keys[5]).not.toBe(keys[4]);
  });
});
