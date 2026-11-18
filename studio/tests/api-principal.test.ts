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
// Every change Studio sends to the platform names the person Studio shows as
// signed in, and a change the platform refuses for someone else isn't sent again.
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, StudioApi } from "../src/app/api";
afterEach(() => vi.unstubAllGlobals());

describe("the person a change is for", () => {
  const PRINCIPAL = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
  const ok = () =>
    vi.fn(
      async () =>
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
  const sentHeaders = (fetch: ReturnType<typeof ok>, call = 0) =>
    (fetch.mock.calls[call] as unknown as [string, RequestInit])[1]
      .headers as Record<string, string>;

  it("every change names the person Studio shows", async () => {
    const fetch = ok();
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    api.expectedPrincipal = () => PRINCIPAL;
    await api.mutate(
      "/studio/api/api/v1/tenants/t/projects/p/drafts/d",
      "PUT",
      { document: {} },
    );
    await api.request(
      "/studio/api/api/v1/tenants/t/projects/p/environments/e/step-tests",
      "POST",
      {},
      { "Idempotency-Key": "k" },
    );
    expect(sentHeaders(fetch, 0)["X-Weave-Principal"]).toBe(PRINCIPAL);
    expect(sentHeaders(fetch, 1)["X-Weave-Principal"]).toBe(PRINCIPAL);
  });

  it("only changes to the platform name the person, not reads or host calls", async () => {
    const fetch = ok();
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    api.expectedPrincipal = () => PRINCIPAL;
    await api.request("/studio/api/api/v1/tenants/t/projects/p/actions");
    await api.request("/studio/local/validate", "POST", { source: "" });
    await api.request(
      "/studio/api/api/v1/tenants/t/projects/p/drafts/d",
      "PUT",
      {},
    );
    expect(sentHeaders(fetch, 0)).not.toHaveProperty("X-Weave-Principal");
    expect(sentHeaders(fetch, 1)).not.toHaveProperty("X-Weave-Principal");
    expect(sentHeaders(fetch, 2)["X-Weave-Principal"]).toBe(PRINCIPAL);
  });

  it("names the person only once Studio knows who is signed in", async () => {
    const fetch = ok();
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    await api.request("/studio/api/api/v1/x", "POST", {});
    api.expectedPrincipal = () => null;
    await api.request("/studio/api/api/v1/x", "POST", {});
    api.expectedPrincipal = () => "not-a-uuid";
    await api.request("/studio/api/api/v1/x", "POST", {});
    api.expectedPrincipal = () => PRINCIPAL;
    await api.request("/studio/api/api/v1/x", "POST", {});
    for (const call of [0, 1, 2])
      expect(sentHeaders(fetch, call)).not.toHaveProperty("X-Weave-Principal");
    expect(sentHeaders(fetch, 3)["X-Weave-Principal"]).toBe(PRINCIPAL);
  });

  it("a change refused for another person isn't sent again and says why", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            code: "WV-PRINCIPAL-CHANGED",
            message:
              "This request was made for another person; nothing was changed.",
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetch);
    const api = new StudioApi();
    api.expectedPrincipal = () => PRINCIPAL;
    const error = await api
      .mutate("/studio/api/api/v1/x", "POST", {}, undefined, "key")
      .catch((e: unknown) => e);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe(
      "You're now signed in as someone else, so Studio sent nothing. Check the account in Settings.",
    );
  });
});
