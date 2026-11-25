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
// The Clusters badge: the change requests waiting for this person's approval,
// read only while they may approve one, by code that loads then.
import "@angular/compiler";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { App } from "../src/app/app";
import { change, serverNow } from "./change-request-fixture";
beforeEach(() => void vi.useFakeTimers({ toFake: ["Date"], now: serverNow }));
afterAll(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

interface Shell {
  identity: unknown;
  watchApprovals(): Promise<void>;
}
/** An account that may approve changes. */
const APPROVER = {
  principal_id: "person",
  kind: "human",
  workspaces: [],
  truncated: false,
  grants: [
    {
      role: "operator",
      scope: null,
      resources: ["*"],
      capabilities: ["deployment.approve"],
    },
  ],
};
function shell(identity: unknown = APPROVER) {
  const app = Object.create(App.prototype) as App & Shell;
  const asked: string[] = [];
  const told: string[] = [];
  const items = [change()];
  Object.assign(app, {
    approvalWatch: null,
    identity,
    cdr: { markForCheck() {} },
    api: {
      session: { profile: { name: "dev" } },
      environment: "/env/a",
      request: async (path: string) => {
        asked.push(path);
        return { items, next_cursor: null };
      },
    },
    can: () => true,
    notify: (text: string) => told.push(text),
  });
  Object.defineProperty(app, "signInEnded", { value: false });
  return { app, asked, told, items };
}

describe("the Clusters badge", () => {
  it("counts the changes waiting for this person's approval", async () => {
    const { app, asked } = shell();
    expect(app.navBadge("clusters")).toBeNull();
    await app.watchApprovals();
    expect(asked).toHaveLength(1);
    expect(app.navBadge("clusters")).toEqual({
      count: 1,
      critical: false,
      name: "Clusters, 1 change waits for your approval",
    });
  });

  it("says when a change that waits for this person is new since the first read", async () => {
    const { app, told, items } = shell();
    await app.watchApprovals();
    expect(told).toEqual([]);
    items.push(change({ id: "b", title: "Scale workers to 1" }));
    await app.watchApprovals();
    expect(told).toEqual([
      "Plan ready for approval: Scale workers to 1 — expires in 9:42",
    ]);
    expect(app.navBadge("clusters")?.count).toBe(2);
  });

  it("reads nothing for an account that can't approve, and forgets the count of one that could", async () => {
    const { app, asked } = shell({ ...APPROVER, grants: [] });
    await app.watchApprovals();
    expect(asked).toEqual([]);
    Object.assign(app, { identity: APPROVER });
    await app.watchApprovals();
    expect(app.navBadge("clusters")?.count).toBe(1);
    Object.assign(app, { identity: null });
    await app.watchApprovals();
    expect(app.navBadge("clusters")).toBeNull();
    expect(asked).toHaveLength(1);
  });

  it("reads nothing for an account that signed out while the code loaded", async () => {
    const { app, asked } = shell();
    const reading = app.watchApprovals();
    Object.assign(app, { identity: null });
    await reading;
    expect(asked).toEqual([]);
    expect(app.navBadge("clusters")).toBeNull();
  });
});
