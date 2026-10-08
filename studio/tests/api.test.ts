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
import { describe, it, expect, vi, afterEach } from "vitest";
import { StudioApi, ApiError } from "../src/app/api";
afterEach(() => vi.unstubAllGlobals());
describe("same-origin credential boundary", () => {
  it("rejects arbitrary destinations before any fetch", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    api.session.csrfToken = "csrf";
    await expect(api.request("https://other.invalid/")).rejects.toThrow(
      "same-origin",
    );
    expect(fetch).not.toHaveBeenCalled();
  });
  it("sends scoped revision and stable receipt identity with only local CSRF", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ revision: 2 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    api.session.csrfToken = "local-csrf";
    await api.mutate(
      "/studio/api/api/v1/tenants/t/projects/p/drafts/d",
      "PUT",
      { document: {} },
      1,
      "stable-command",
    );
    const options = fetch.mock.calls[0][1] as RequestInit;
    expect(options.credentials).toBe("same-origin");
    expect(options.headers).toEqual({
      "X-Weave-CSRF": "local-csrf",
      "Content-Type": "application/json",
      "If-Match": '"1"',
      "Idempotency-Key": "stable-command",
    });
    expect(options.headers).not.toHaveProperty("Authorization");
  });
  it("keeps explicit HTTP failures distinguishable from lost responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ code: "WV-REVISION" }), {
            status: 409,
          }),
      ),
    );
    await expect(
      new StudioApi().request("/studio/session"),
    ).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Network unavailable");
      }),
    );
    await expect(
      new StudioApi().request("/studio/session"),
    ).rejects.toBeInstanceOf(TypeError);
  });
});
describe("capacity rejections", () => {
  const busy = (code = "WV-OPERATION-CAPACITY", retryAfter = "1") =>
    new Response(JSON.stringify({ code, message: "Request unavailable" }), {
      status: 429,
      headers: {
        "Content-Type": "application/json",
        "Retry-After": retryAfter,
      },
    });
  const ok = () =>
    new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  const api = () => {
    const client = new StudioApi();
    client.session.csrfToken = "csrf";
    client.wait = vi.fn(async () => undefined);
    return client;
  };
  it("replays a read after the platform's Retry-After pause", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(busy())
      .mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetch);
    const client = api();
    await expect(client.request("/studio/api/runs")).resolves.toEqual({
      items: [],
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(client.wait).toHaveBeenCalledWith(1000);
  });
  it("replays a change only when it carries an idempotency key", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(busy())
      .mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetch);
    await expect(
      api().mutate("/studio/api/runs", "POST", {}, undefined, "same-key"),
    ).resolves.toEqual({ items: [] });
    const keys = fetch.mock.calls.map(
      ([, init]) => (init as RequestInit).headers as Record<string, string>,
    );
    expect(keys.map((h) => h["Idempotency-Key"])).toEqual([
      "same-key",
      "same-key",
    ]);
    const once = vi.fn().mockResolvedValue(busy());
    vi.stubGlobal("fetch", once);
    await expect(
      api().request("/studio/local/validate", "POST", {}),
    ).rejects.toMatchObject({
      status: 429,
    });
    expect(once).toHaveBeenCalledTimes(1);
  });
  it("gives up after a bounded number of attempts with the plain error", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(async () => busy("WV-OPERATION-CAPACITY", "30"));
    vi.stubGlobal("fetch", fetch);
    const client = api();
    await expect(client.request("/studio/api/runs")).rejects.toMatchObject({
      status: 429,
      message:
        "The platform is busy with other requests. Wait a moment and try again.",
    });
    expect(fetch).toHaveBeenCalledTimes(4);
    expect(client.wait).toHaveBeenCalledWith(5000);
  });
  it("recovers paired session bootstrap after local admission is temporarily full", async () => {
    const session = {
      paired: true,
      csrfToken: "kept",
      version: "1",
      mode: "offline",
      profile: null,
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(busy("WV-STUDIO-BUSY"))
      .mockResolvedValueOnce(busy("WV-STUDIO-BUSY"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(session), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetch);
    const client = api();
    await expect(client.pair()).resolves.toEqual(session);
    expect(client.session.paired).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(client.wait).toHaveBeenCalledTimes(2);
    expect(
      fetch.mock.calls.every(
        ([path, init]) => path === "/studio/session" && init.method === "GET",
      ),
    ).toBe(true);
  });
  it("bounds local admission retries and never replays a pairing mutation", async () => {
    const fetch = vi
      .fn()
      .mockImplementation(async () => busy("WV-STUDIO-BUSY"));
    vi.stubGlobal("fetch", fetch);
    await expect(api().pair()).rejects.toMatchObject({
      status: 429,
      code: "WV-STUDIO-BUSY",
    });
    expect(fetch).toHaveBeenCalledTimes(4);
    fetch.mockClear();
    await expect(api().pair("one-use-code")).rejects.toMatchObject({
      status: 429,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("replays a keyed change the platform refused while confirming compatibility", async () => {
    const restricted = (retryAfter: string) =>
      new Response(
        JSON.stringify({
          code: "WV-COMPATIBILITY",
          message: "Request unavailable",
        }),
        {
          status: 503,
          headers: {
            "Content-Type": "application/json",
            "Retry-After": retryAfter,
          },
        },
      );
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(restricted("2"))
      .mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetch);
    const client = api();
    await expect(
      client.mutate("/studio/api/runs", "POST", {}, undefined, "run-key"),
    ).resolves.toEqual({ items: [] });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(client.wait).toHaveBeenCalledWith(2000);
    const unkeyed = vi.fn().mockResolvedValue(restricted("2"));
    vi.stubGlobal("fetch", unkeyed);
    await expect(
      api().request("/studio/api/runs", "POST", {}),
    ).rejects.toMatchObject({ status: 503, code: "WV-COMPATIBILITY" });
    expect(unkeyed).toHaveBeenCalledTimes(1);
    // A restriction that outlasts the replay window fails at once.
    const lasting = vi.fn().mockResolvedValue(restricted("45"));
    vi.stubGlobal("fetch", lasting);
    const waiting = api();
    await expect(
      waiting.mutate("/studio/api/runs", "POST", {}, undefined, "run-key"),
    ).rejects.toMatchObject({ status: 503, code: "WV-COMPATIBILITY" });
    expect(lasting).toHaveBeenCalledTimes(1);
    expect(waiting.wait).not.toHaveBeenCalled();
    const other = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ code: "WV-INTERNAL" }), { status: 503 }),
      );
    vi.stubGlobal("fetch", other);
    await expect(api().request("/studio/api/runs")).rejects.toMatchObject({
      status: 503,
    });
    expect(other).toHaveBeenCalledTimes(1);
  });
  it("does not replay other 429 answers", async () => {
    const fetch = vi.fn().mockResolvedValue(busy("WV-PAGE-LIMIT"));
    vi.stubGlobal("fetch", fetch);
    await expect(
      api().request("/studio/api/definitions"),
    ).rejects.toMatchObject({
      status: 429,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
