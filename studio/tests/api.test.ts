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
