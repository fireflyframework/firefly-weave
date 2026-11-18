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
// The connections port of one account and environment: once it is replaced
// (the account, environment or platform changed) it sends nothing more, and a
// test policy it couldn't read is asked again only after a pause.
import { describe, expect, it } from "vitest";
import { ApiError, type StudioApi } from "../src/app/api";
import {
  ConnectionsPort,
  POLICY_RETRY_MS,
  type PortHost,
} from "../src/app/editor/connections/connections-port";

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const LOCAL = "6f1c1b7e-8a43-4f0b-9b55-0f6b0d3a1c11";
const binding = {
  connectionId: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  revisionId: "11111111-2222-4333-8444-555555555555",
  name: "acme-crm",
};

function port(answer: (path: string) => unknown = () => ({ ok: true })) {
  const calls: string[] = [];
  let now = 1_000_000;
  const api = {
    environment: ENV,
    request: async (path: string) => {
      calls.push(path);
      const value = answer(path);
      if (value instanceof Error) throw value;
      return value;
    },
  } as unknown as StudioApi;
  const host = {
    api,
    profile: null,
    can: () => true,
    runAccount: null,
    stepDataScope: "s",
    environmentName: "local",
  } as unknown as PortHost;
  const made = new ConnectionsPort("key", host, LOCAL, () => now);
  return {
    port: made,
    calls,
    advance: (ms: number) => (now += ms),
  };
}

describe("the connections port", () => {
  it("a replaced port tests nothing", async () => {
    const { port: made, calls } = port();
    await made.ready;
    await made.setBinding("order-intake", "crm", binding);
    made.forget();
    await made.test("order-intake", "crm", "https://api.acme.test");
    expect(calls).toEqual([]);
  });

  it("a test policy it couldn't read is asked again only after a pause", async () => {
    const {
      port: made,
      calls,
      advance,
    } = port(() => new ApiError(500, { code: "WV-INTERNAL", message: "boom" }));
    made.policyNow();
    await made.policy();
    made.policyNow();
    made.policyNow();
    expect(calls).toHaveLength(1);
    advance(POLICY_RETRY_MS + 1);
    made.policyNow();
    await made.policy();
    expect(calls).toHaveLength(2);
    // The pause doubles while it keeps failing.
    advance(POLICY_RETRY_MS + 1);
    made.policyNow();
    expect(calls).toHaveLength(2);
    advance(POLICY_RETRY_MS);
    made.policyNow();
    expect(calls).toHaveLength(3);
  });

  it("says when a choice couldn't be kept", async () => {
    const { port: kept } = port();
    await kept.ready;
    // Without an account prefix a choice lasts for the session: that's kept as asked.
    expect(await kept.setBinding("order-intake", "crm", binding)).toBe(true);
    expect(
      await kept.setBinding("order-intake", "crm", {
        ...binding,
        revisionId: "not-a-revision",
      }),
    ).toBe(false);
    expect(kept.binding("order-intake", "crm")).toEqual(binding);
  });
});
