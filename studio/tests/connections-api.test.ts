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
import { ApiError, type StudioApi } from "../src/app/api";
import {
  platformConnections,
  readBindable,
  readTestPolicy,
} from "../src/app/editor/connections/connections-api";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const CONNECTION = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
const REVISION = "11111111-2222-4333-8444-555555555555";
const item = (over: Record<string, unknown> = {}) => ({
  connection_id: CONNECTION,
  revision_id: REVISION,
  name: "acme-crm",
  connector: "weave-http@2.0.0",
  label: "acme-crm",
  enabled: true,
  endpoint_origin: "https://api.acme.test",
  ...over,
});

function fakeApi(answer: (path: string, method: string) => unknown) {
  const calls: { path: string; method: string }[] = [];
  const api = {
    environment: ENV,
    request: async (path: string, method = "GET") => {
      calls.push({ path, method });
      const value = answer(path, method);
      if (value instanceof Error) throw value;
      return value;
    },
  } as unknown as StudioApi;
  return { api, calls };
}

describe("connections from the platform", () => {
  it("asks for the connector's bindable connections page by page", async () => {
    const { api, calls } = fakeApi((path) =>
      path.includes("cursor=")
        ? { items: [item({ name: "acme-erp" })], next_cursor: null }
        : { items: [item()], next_cursor: "c 2" },
    );
    const items = await platformConnections(api).bindable("weave-http@2.0.0");
    expect(calls.map((call) => call.path)).toEqual([
      `${ENV}/bindable-connections?connector=weave-http%402.0.0&limit=100`,
      `${ENV}/bindable-connections?connector=weave-http%402.0.0&limit=100&cursor=c%202`,
    ]);
    expect(items.map((entry) => entry.name)).toEqual(["acme-crm", "acme-erp"]);
    expect(items[0]).toEqual({
      connectionId: CONNECTION,
      revisionId: REVISION,
      name: "acme-crm",
      connector: "weave-http@2.0.0",
      enabled: true,
      origin: "https://api.acme.test",
    });
  });

  it("lists every bindable connection page by page and keeps any version of the named connector", async () => {
    for (const name of ["weave-kafka", "weave-email"]) {
      const { api, calls } = fakeApi((path) =>
        path.includes("cursor=")
          ? {
              items: [
                item({ name: "second", connector: `${name}@2.0.0` }),
                item({ name: "prefix", connector: `${name}-extra@1.0.0` }),
              ],
              next_cursor: null,
            }
          : {
              items: [
                item({ name: "first", connector: `${name}@1.0.0` }),
                item({ name: "http", connector: "weave-http@2.0.0" }),
              ],
              next_cursor: "c 2",
            },
      );
      const items = await platformConnections(api).bindableNamed(name);
      expect(calls.map((call) => call.path)).toEqual([
        `${ENV}/bindable-connections?limit=100`,
        `${ENV}/bindable-connections?limit=100&cursor=c%202`,
      ]);
      expect(items.map((entry) => entry.name)).toEqual(["first", "second"]);
    }
  });

  it("stops after twenty pages", async () => {
    const { api, calls } = fakeApi(() => ({
      items: [item()],
      next_cursor: "more",
    }));
    await expect(
      platformConnections(api).bindable("weave-http@2.0.0"),
    ).rejects.toThrow(/Too many connections/);
    expect(calls).toHaveLength(20);
  });

  it("reads only typed items", () => {
    const { revision_id: _dropped, ...noRevision } = item();
    expect(
      readBindable({
        items: [
          item(),
          noRevision,
          item({ enabled: "yes" }),
          item({ connection_id: "nope" }),
          item({ endpoint_origin: undefined }),
          "junk",
        ],
      }).map((entry) => [entry.name, entry.origin]),
    ).toEqual([
      ["acme-crm", "https://api.acme.test"],
      ["acme-crm", null],
    ]);
    expect(readBindable({ items: "x" })).toEqual([]);
    expect(readBindable(null)).toEqual([]);
  });

  it("reads the test policy, and an unreadable one says so", async () => {
    expect(readTestPolicy({ test_calls: "enabled", revision: 3 })).toEqual({
      test_calls: "enabled",
      revision: 3,
    });
    expect(readTestPolicy({ test_calls: "on", revision: 3 })).toBeNull();
    expect(readTestPolicy({ test_calls: "disabled", revision: -1 })).toBeNull();

    const readable = fakeApi(() => ({ test_calls: "disabled", revision: 0 }));
    expect(await platformConnections(readable.api).policy()).toEqual({
      test_calls: "disabled",
      revision: 0,
    });
    expect(readable.calls).toEqual([
      { path: `${ENV}/test-policy`, method: "GET" },
    ]);
    const forbidden = fakeApi(
      () => new ApiError(403, { code: "WV-FORBIDDEN", message: "No." }),
    );
    expect(await platformConnections(forbidden.api).policy()).toBe(
      "unreadable",
    );
    const broken = fakeApi(() => ({ test_calls: 1 }));
    await expect(platformConnections(broken.api).policy()).rejects.toThrow();
  });

  it("a connection test keeps the reason", async () => {
    const { api, calls } = fakeApi(() => ({
      ok: false,
      code: "failed",
      reason: "unauthorized",
      encrypted: true,
      job_id: "x",
    }));
    expect(await platformConnections(api).test(REVISION)).toEqual({
      ok: false,
      reason: "unauthorized",
      encrypted: true,
      message: null,
    });
    expect(calls).toEqual([
      { path: `${ENV}/connections/${REVISION}/test`, method: "POST" },
    ]);
    const passed = fakeApi(() => ({ ok: true, code: "ok" }));
    expect(await platformConnections(passed.api).test(REVISION)).toEqual({
      ok: true,
      reason: null,
      encrypted: null,
      message: null,
    });
    const odd = fakeApi(() => ({ ok: false, reason: "teapot" }));
    expect(
      (await platformConnections(odd.api).test(REVISION)).reason,
    ).toBeNull();
  });

  it("a connection the platform can't use says why in its own words", async () => {
    const { api } = fakeApi(
      () =>
        new ApiError(422, {
          code: "WV-CONNECTION-UNREADY",
          message: "The secret handle acme-api-key isn't available in local.",
        }),
    );
    expect(await platformConnections(api).test(REVISION)).toEqual({
      ok: false,
      reason: null,
      encrypted: null,
      message: "The secret handle acme-api-key isn't available in local.",
    });
    const down = fakeApi(
      () => new ApiError(502, { code: "WV-STUDIO-UPSTREAM", message: "x" }),
    );
    await expect(
      platformConnections(down.api).test(REVISION),
    ).rejects.toThrow();
  });

  it("keeps asking the environment it was made for", async () => {
    let environment = ENV;
    const calls: string[] = [];
    const api = {
      get environment() {
        return environment;
      },
      request: async (path: string) => {
        calls.push(path);
        return path.endsWith("/test-policy")
          ? { test_calls: "enabled", revision: 1 }
          : { items: [], next_cursor: null };
      },
    } as unknown as StudioApi;
    const connections = platformConnections(api, ENV);
    // The person switches environment after the port was made.
    environment = "/studio/api/api/v1/tenants/t/projects/p/environments/other";
    await connections.bindable("weave-http@2.0.0");
    await connections.policy();
    await connections.test(REVISION);
    expect(calls.every((path) => path.startsWith(`${ENV}/`))).toBe(true);
    expect(calls).toHaveLength(3);
  });
});
