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
  INBOX_LIMIT,
  adapterIcon,
  countdown,
  inboxCandidates,
  intentLabel,
  riskName,
  runnerLabel,
  runnerLine,
  runnerState,
  runnerTone,
  targetRunner,
} from "../src/app/operate/clusters/cluster-model";
import {
  APPROVAL_READS_AT_ONCE,
  ApprovalsInbox,
  settleEach,
} from "../src/app/operate/clusters/approvals-inbox";
import type { Plan, Runner } from "../src/app/operations/deployment-contracts";

const now = Date.parse("2026-10-08T12:00:00Z");
const runner = (overrides: Partial<Runner> = {}): Runner => ({
  id: "77777777-7777-4777-8777-777777777777",
  principal_id: "44444444-4444-4444-8444-444444444444",
  target_id: "11111111-1111-4111-8111-111111111111",
  adapter: "docker-compose",
  adapter_version: "1",
  capabilities: ["observe"],
  last_seen: "2026-10-08T11:59:30Z",
  expires_at: "2026-10-08T12:01:00Z",
  revoked: false,
  ...overrides,
});

describe("runner presence", () => {
  it("is online until the registration's contact expires", () => {
    expect(runnerState(runner(), now)).toBe("online");
    expect(
      runnerState(runner({ expires_at: "2026-10-08T12:00:00Z" }), now),
    ).toBe("offline");
    expect(runnerState(runner({ revoked: true }), now)).toBe("revoked");
  });

  it("uses the health tones with a word for each state", () => {
    expect([runnerLabel(runner(), now), runnerTone(runner(), now)]).toEqual([
      "Online",
      "success",
    ]);
    const stale = runner({ expires_at: "2026-10-08T11:00:00Z" });
    expect([runnerLabel(stale, now), runnerTone(stale, now)]).toEqual([
      "Offline",
      "danger",
    ]);
    const revoked = runner({ revoked: true });
    expect([runnerLabel(revoked, now), runnerTone(revoked, now)]).toEqual([
      "Revoked",
      "neutral",
    ]);
  });

  it("writes a target card's runner line", () => {
    expect(runnerLine(null, now)).toBe("No runner has registered");
    expect(runnerLine(runner(), now)).toBe("Runner online");
    expect(
      runnerLine(
        runner({
          last_seen: "2026-10-08T11:56:00Z",
          expires_at: "2026-10-08T11:57:30Z",
        }),
        now,
      ),
    ).toBe("Runner offline · last contact 4 min ago");
  });

  it("picks a target's newest live registration from the fleet", () => {
    const older = runner({ id: "a", last_seen: "2026-10-08T10:00:00Z" });
    const newer = runner({ id: "b", last_seen: "2026-10-08T11:00:00Z" });
    const revoked = runner({
      id: "c",
      last_seen: "2026-10-08T11:59:00Z",
      revoked: true,
    });
    const elsewhere = runner({ id: "d", target_id: "other" });
    expect(
      targetRunner([older, revoked, newer, elsewhere], older.target_id)?.id,
    ).toBe("b");
    expect(targetRunner([revoked], older.target_id)).toBeNull();
  });

  it("draws each adapter with its own icon", () => {
    expect(adapterIcon("docker-compose")).toBe("compose");
    expect(adapterIcon("kubernetes")).toBe("kubernetes");
    expect(adapterIcon("azure-container-apps")).toBe("cloud");
  });
});

const plan = (id: string, expiresIn: number): Plan => ({
  id,
  scope: { tenant_id: "tenant", project_id: "project", environment_id: "env" },
  target_id: "11111111-1111-4111-8111-111111111111",
  target_revision: 1,
  deployment_id: "22222222-2222-4222-8222-222222222222",
  deployment_revision: 3,
  adapter: "docker-compose",
  adapter_version: "1",
  intent: "scale_workers",
  observation_id: "55555555-5555-4555-8555-555555555555",
  observation_digest: "b".repeat(64),
  steps: [],
  risks: ["worker_drain"],
  created_at: new Date(now - 60_000).toISOString(),
  expires_at: new Date(now + expiresIn * 1000).toISOString(),
  digest: id.repeat(64).slice(0, 64),
});

describe("approval countdown", () => {
  it("is neutral, then warns from 2 min, then turns danger from 30 s", () => {
    const at = (seconds: number) =>
      countdown(new Date(now + seconds * 1000).toISOString(), now);
    expect(at(582)).toEqual({
      text: "9:42",
      note: "",
      tone: "neutral",
      expired: false,
    });
    expect(at(121).tone).toBe("neutral");
    expect(at(120)).toEqual({
      text: "2:00",
      note: "Expires soon",
      tone: "warning",
      expired: false,
    });
    expect(at(31).tone).toBe("warning");
    expect(at(30)).toEqual({
      text: "0:30",
      note: "Expires soon",
      tone: "danger",
      expired: false,
    });
    expect(at(0.4).text).toBe("0:01");
    expect(at(0)).toEqual({
      text: "Expired",
      note: "",
      tone: "neutral",
      expired: true,
    });
    expect(countdown("not a date", now).expired).toBe(true);
  });

  it("names intents and risks in words", () => {
    expect(intentLabel("scale_workers")).toBe("Scale workers");
    expect(riskName("worker_drain")).toBe("Worker drain");
  });
});

describe("approvals inbox", () => {
  it("keeps open plans soonest first, then plans that just expired", () => {
    const plans = [
      plan("a", 300),
      plan("b", 60),
      plan("c", -60),
      plan("d", -20 * 60),
      plan("e", -5),
    ];
    expect(inboxCandidates(plans, now).map((item) => item.id)).toEqual([
      "b",
      "a",
      "e",
      "c",
    ]);
    const many = Array.from({ length: 60 }, (_, i) => plan(`p${i}`, 100 + i));
    expect(inboxCandidates(many, now)).toHaveLength(INBOX_LIMIT);
  });

  it("lists plans without an approval and counts the ones it could not check", async () => {
    const pages: Record<string, unknown> = {
      "env/deployment-plans?limit=100": {
        items: [plan("a", 300), plan("b", 60)],
        next_cursor: "next",
      },
      "env/deployment-plans?limit=100&cursor=next": {
        items: [plan("c", 90)],
        next_cursor: null,
      },
      "env/deployment-plans/b/approval": {
        plan_id: "b",
        digest: "b".repeat(64),
        principal_id: "human",
        approved_at: new Date(now).toISOString(),
      },
      "env/deployment-plans/a/approval": null,
    };
    const inbox = new ApprovalsInbox(async <T>(path: string) => {
      if (!(path in pages)) throw new Error("unavailable");
      return pages[path] as T;
    });
    await inbox.load("env", now);
    expect(inbox.rows.map((item) => item.id)).toEqual(["a"]);
    expect(inbox.unchecked).toBe(1);
    expect(inbox.truncated).toBe(false);
    expect(inbox.loaded).toBe(true);
  });

  it("reads at most four approvals at once and keeps each answer in order", async () => {
    let open = 0;
    let most = 0;
    let started = 0;
    const releases: (() => void)[] = [];
    const run = (item: number) =>
      new Promise<number>((resolve, reject) => {
        open++;
        started++;
        most = Math.max(most, open);
        releases.push(() => {
          open--;
          if (item % 3 === 0) reject(new Error(`no ${item}`));
          else resolve(item * 10);
        });
      });
    const settled = settleEach(
      Array.from({ length: 10 }, (_, i) => i),
      APPROVAL_READS_AT_ONCE,
      run,
    );
    // Answers arrive out of order; a new read starts only as one finishes.
    while (started < 10 || open) {
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(open).toBeLessThanOrEqual(4);
      releases.pop()?.();
    }
    const results = await settled;
    expect(APPROVAL_READS_AT_ONCE).toBe(4);
    expect(most).toBe(4);
    expect(
      results.map((result) =>
        result.status === "fulfilled"
          ? result.value
          : (result.reason as Error).message,
      ),
    ).toEqual(["no 0", 10, 20, "no 3", 40, 50, "no 6", 70, 80, "no 9"]);
    expect(await settleEach([], 4, run)).toEqual([]);
  });

  it("never has more than four approval reads in flight", async () => {
    const plans = Array.from({ length: 12 }, (_, i) => plan(`p${i}`, 100 + i));
    let open = 0;
    let most = 0;
    const inbox = new ApprovalsInbox(async <T>(path: string) => {
      if (!path.includes("/approval"))
        return { items: plans, next_cursor: null } as T;
      open++;
      most = Math.max(most, open);
      await new Promise((resolve) => setTimeout(resolve, 1));
      open--;
      return (path.includes("/p3/") ? { plan_id: "p3" } : null) as T;
    });
    await inbox.load("env", now);
    expect(most).toBe(4);
    expect(inbox.rows.map((item) => item.id)).toEqual(
      plans.filter((item) => item.id !== "p3").map((item) => item.id),
    );
  });

  it("stops after 1,000 plans and reports a failed plan read", async () => {
    let reads = 0;
    const inbox = new ApprovalsInbox(async <T>(path: string) => {
      if (path.includes("/approval")) return null as T;
      reads++;
      return { items: [], next_cursor: "more" } as T;
    });
    await inbox.load("env", now);
    expect(reads).toBe(10);
    expect(inbox.truncated).toBe(true);
    const failing = new ApprovalsInbox(async () => {
      throw Object.assign(new Error("Denied"), {
        plain: { message: "Denied", code: "WV-DENIED", status: 403 },
      });
    });
    await expect(failing.load("env", now)).rejects.toThrow("Denied");
    expect(failing.error).toEqual({
      message: "Denied",
      code: "WV-DENIED",
      status: 403,
    });
    expect(failing.loaded).toBe(false);
  });
});
