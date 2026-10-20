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
  ApprovalWatch,
  inboxEntries,
} from "../src/app/operate/clusters/approvals-inbox";
import { approvableCount } from "../src/app/operate/clusters/change-request-model";
import type { Plan } from "../src/app/operations/deployment-contracts";
import type { ChangeRequest } from "../src/app/operations/deployment-contracts";
import { at, change as base, serverNow as now } from "./change-request-fixture";

const change = (overrides: Partial<ChangeRequest> = {}) =>
  base({ target_id: "t1", ...overrides });

function plan(id: string, expires: number): Plan {
  return {
    id,
    target_id: "t1",
    deployment_id: "d1",
    intent: "scale_workers",
    digest: id.padEnd(64, "0"),
    expires_at: at(expires),
    risks: [],
  } as unknown as Plan;
}

describe("approvals inbox order", () => {
  it("sorts change requests and plain plans by when they must be approved", () => {
    const rows = inboxEntries(
      [
        change({ id: "c-late", plan_id: "p-late", plan_expires_at: at(600) }),
        change({
          id: "c-drain",
          plan_id: "p-drain",
          plan_expires_at: at(900),
          // 900 - (300 + 120) = 480 s to approve.
          drain: { mode: "drain_first", timeout_seconds: 300 },
          drain_set: [{ worker_id: "w1", host: "h1", active_leases: 1 }],
        }),
      ],
      [plan("p-plain", 300), plan("p-late", 600)],
      now,
    );
    expect(rows.map((row) => row.id)).toEqual(["p-plain", "c-drain", "c-late"]);
  });

  it("drops plans a change request already lists, including ended ones", () => {
    const rows = inboxEntries(
      [
        change({ id: "c1", plan_id: "p1", plan_expires_at: at(600) }),
        change({ id: "c2", plan_id: "p2", state: "declined" }),
      ],
      [plan("p1", 600), plan("p2", 600), plan("p3", 600)],
      now,
    );
    expect(rows.map((row) => row.id)).toEqual(["c1", "p3"]);
  });

  it("keeps a change that expired in the last 15 minutes after the open ones", () => {
    const rows = inboxEntries(
      [
        change({ id: "gone", plan_id: "p1", plan_expires_at: at(-60) }),
        change({ id: "old", plan_id: "p2", plan_expires_at: at(-3600) }),
        change({ id: "open", plan_id: "p3", plan_expires_at: at(60) }),
      ],
      [],
      now,
    );
    expect(rows.map((row) => row.id)).toEqual(["open", "gone"]);
  });
});

describe("Clusters badge", () => {
  const can = (capability: string, target: string) =>
    capability === "deployment.approve" && target === "t1";
  it("counts what the viewer may approve, never their own request", () => {
    const rows = [
      change({ id: "a", requested_by: "other", plan_expires_at: at(600) }),
      change({ id: "b", requested_by: "me", plan_expires_at: at(600) }),
      change({
        id: "c",
        requested_by: "other",
        planned_by: "me",
        plan_expires_at: at(600),
      }),
      change({
        id: "d",
        requested_by: "other",
        target_id: "t2",
        plan_expires_at: at(600),
      }),
      change({ id: "e", requested_by: "other", plan_expires_at: at(-1) }),
    ];
    expect(approvableCount(rows, can, "me", () => true, now)).toBe(1);
  });
  it("counts the viewer's own request where one person may approve", () => {
    const rows = [
      change({
        id: "b",
        requested_by: "me",
        plan_expires_at: at(600),
        approval_policy: { distinct_approver: false },
      }),
    ];
    expect(approvableCount(rows, can, "me", () => false, now)).toBe(1);
    expect(approvableCount(rows, can, "me", () => true, now)).toBe(0);
  });
});

describe("approval notice", () => {
  function server(pages: () => unknown[]) {
    return async <T>(path: string) => {
      expect(path).toContain("/deployment-change-requests?");
      expect(path).toContain("state=awaiting_approval");
      return { items: pages(), next_cursor: null } as T;
    };
  }
  it("says nothing on the first read, then once per new change", async () => {
    let items = [change({ id: "a", plan_expires_at: at(600) })];
    const watch = new ApprovalWatch(server(() => items));
    const yes = () => true;
    expect(await watch.load("/env", yes, now)).toBeNull();
    expect(watch.count).toBe(1);
    expect(await watch.load("/env", yes, now)).toBeNull();
    items = [
      ...items,
      change({
        id: "b",
        title: "Scale workers to 1",
        plan_expires_at: at(125),
      }),
    ];
    const notice = await watch.load("/env", yes, now);
    expect(notice?.text).toBe(
      "Plan ready for approval: Scale workers to 1 — expires in 2:05",
    );
    expect(notice?.id).toBe("b");
    expect(watch.count).toBe(2);
    expect(await watch.load("/env", yes, now)).toBeNull();
  });
  it("starts again silently in another environment", async () => {
    const items = [change({ id: "a", plan_expires_at: at(600) })];
    const watch = new ApprovalWatch(server(() => items));
    await watch.load("/env", () => true, now);
    expect(await watch.load("/other", () => true, now)).toBeNull();
  });
  it("never announces a change the viewer can't approve", async () => {
    let items = [change({ id: "a", plan_expires_at: at(600) })];
    const watch = new ApprovalWatch(server(() => items));
    await watch.load("/env", () => true, now);
    items = [...items, change({ id: "b", plan_expires_at: at(600) })];
    expect(await watch.load("/env", (row) => row.id !== "b", now)).toBeNull();
    expect(watch.count).toBe(1);
  });
});
