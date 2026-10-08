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
  claimsLabel,
  filterChoices,
  filterCount,
  filterWorkers,
  filtersFromQuery,
  filtersToQuery,
  noWorkerFilters,
  presenceLabel,
  presenceTone,
  workerBadges,
  workerLoad,
  workerPresence,
  type WorkerRecord,
  type WorkerStatus,
} from "../src/app/operate/workers/workers-model";

const now = Date.parse("2026-10-08T12:00:00Z");
const worker = (overrides: Partial<WorkerStatus> = {}): WorkerStatus => ({
  id: "11111111-1111-4111-8111-111111111111",
  principal_id: "22222222-2222-4222-8222-222222222222",
  release_id: "release-a",
  task_types: ["crm-lookup@1.0.0"],
  capacity: 8,
  revoked: false,
  revision: 2,
  draining: false,
  presence: "recent",
  last_seen_at: "2026-10-08T11:59:50Z",
  presence_expires_at: "2026-10-08T12:00:50Z",
  observed_at: "2026-10-08T12:00:00Z",
  active_leases: 2,
  available_capacity: 6,
  ...overrides,
});

describe("worker presence", () => {
  it("is online while contact is recent and unexpired", () => {
    expect(workerPresence(worker(), now)).toBe("online");
    expect(
      workerPresence(
        worker({ presence_expires_at: "2026-10-08T11:59:59Z" }),
        now,
      ),
    ).toBe("offline");
    expect(workerPresence(worker({ presence: "stale" }), now)).toBe("offline");
    expect(workerPresence(worker({ presence: "unknown" }), now)).toBe(
      "unknown",
    );
  });

  it("uses the health words and tones", () => {
    expect([presenceLabel("online"), presenceTone("online")]).toEqual([
      "Online",
      "success",
    ]);
    expect([presenceLabel("offline"), presenceTone("offline")]).toEqual([
      "Offline",
      "danger",
    ]);
    expect([presenceLabel("unknown"), presenceTone("unknown")]).toEqual([
      "Not seen yet",
      "neutral",
    ]);
    expect(claimsLabel(worker())).toBe("Accepting new tasks");
    expect(claimsLabel(worker({ draining: true }))).toBe("Draining");
    expect(claimsLabel(worker({ revoked: true, draining: true }))).toBe(
      "Revoked",
    );
  });

  it("shows Revoked alone, or the presence and Draining while it drains", () => {
    expect(workerBadges(worker(), "online")).toEqual([
      { label: "Online", tone: "success" },
    ]);
    expect(workerBadges(worker({ draining: true }), "offline")).toEqual([
      { label: "Offline", tone: "danger" },
      { label: "Draining", tone: "warning" },
    ]);
    expect(
      workerBadges(worker({ revoked: true, draining: true }), "online"),
    ).toEqual([{ label: "Revoked", tone: "neutral" }]);
  });

  it("reads the presence for the pills when it isn't given", () => {
    expect(workerBadges(worker({ presence: "unknown" }))).toEqual([
      { label: "Not seen yet", tone: "neutral" },
    ]);
    expect(workerBadges(worker({ presence: "stale", draining: true }))).toEqual(
      [
        { label: "Offline", tone: "danger" },
        { label: "Draining", tone: "warning" },
      ],
    );
  });

  it("measures load against the registered task slots", () => {
    expect(workerLoad(worker())).toEqual({
      active: 2,
      capacity: 8,
      fill: 0.25,
      over: false,
      text: "2 of 8",
    });
    expect(workerLoad(worker({ active_leases: 9 }))).toMatchObject({
      fill: 1,
      over: true,
      text: "9 of 8",
    });
    const unknown = { ...worker() } as Partial<WorkerStatus>;
    delete unknown.active_leases;
    expect(workerLoad(unknown as WorkerStatus).text).toBe("Unknown");
  });
});

describe("worker filters", () => {
  const workers: WorkerRecord[] = [
    worker({ id: "a" }),
    worker({ id: "b", presence: "stale", release_id: "release-b" }),
    worker({ id: "c", draining: true, task_types: ["email-send@1.0.0"] }),
    worker({ id: "d", revoked: true }),
    worker({ id: "e", presence: "unknown" }),
    { id: "f", unavailable: true },
  ];
  const ids = (filters = noWorkerFilters) =>
    filterWorkers(workers, filters, now).map((item) => item.id);

  it("hides revoked workers unless asked, and narrows by every filter", () => {
    expect(ids()).toEqual(["a", "b", "c", "e", "f"]);
    expect(ids({ ...noWorkerFilters, revoked: true })).toEqual([
      "a",
      "b",
      "c",
      "d",
      "e",
    ]);
    expect(ids({ ...noWorkerFilters, presence: "online" })).toEqual(["a", "c"]);
    expect(ids({ ...noWorkerFilters, presence: "offline" })).toEqual(["b"]);
    expect(ids({ ...noWorkerFilters, presence: "unknown" })).toEqual(["e"]);
    expect(ids({ ...noWorkerFilters, draining: true })).toEqual(["c"]);
    expect(ids({ ...noWorkerFilters, release: "release-b" })).toEqual(["b"]);
    expect(ids({ ...noWorkerFilters, taskType: "email-send@1.0.0" })).toEqual([
      "c",
    ]);
  });

  it("round-trips through the address", () => {
    const filters = {
      presence: "offline" as const,
      draining: true,
      release: "release-b",
      taskType: "crm-lookup@1.0.0",
      revoked: true,
    };
    const query = filtersToQuery(filters);
    expect(query).toBe(
      "?presence=offline&draining=true&release=release-b&task_type=crm-lookup%401.0.0&revoked=true",
    );
    expect(filtersFromQuery(query)).toEqual(filters);
    expect(filterCount(filters)).toBe(5);
    expect(filtersToQuery(noWorkerFilters)).toBe("");
    expect(filtersFromQuery("?presence=busy&draining=yes")).toEqual(
      noWorkerFilters,
    );
  });

  it("offers the releases and task types the loaded workers report", () => {
    expect(filterChoices(workers)).toEqual({
      releases: ["release-a", "release-b"],
      taskTypes: ["crm-lookup@1.0.0", "email-send@1.0.0"],
    });
  });
});
