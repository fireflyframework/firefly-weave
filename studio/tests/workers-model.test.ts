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
  filtersFromQuery,
  filtersToQuery,
  noWorkerFilters,
  releaseTaskTypes,
  retireSummary,
  shortDigest,
  workerQuery,
  workerVersion,
  workerWhere,
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
  it("maps human presence and private host filters to the server query", () => {
    const filters = {
      ...noWorkerFilters,
      presence: "offline" as const,
      draining: false,
      hostPrefix: "host-",
    };
    const query = new URLSearchParams(workerQuery(filters, null));
    expect(query.get("presence")).toBe("stale");
    expect(query.get("draining")).toBe("false");
    expect(query.get("state")).toBe("current");
    expect(query.get("host_prefix")).toBe("host-");
    expect(filtersToQuery(filters)).not.toContain("host-");
  });

  it("sends every filter by the platform's name and continues with a cursor", () => {
    const query = new URLSearchParams(
      workerQuery(
        {
          presence: "unknown",
          draining: true,
          release: "release-b",
          taskType: "email-send@1.0.0",
          target: "target-1",
          hostPrefix: "",
          revoked: true,
          order: "registered_desc",
        },
        "next-page",
      ),
    );
    expect(Object.fromEntries(query)).toEqual({
      limit: "50",
      state: "all",
      order: "registered_desc",
      presence: "unknown",
      draining: "true",
      release_id: "release-b",
      task_type: "email-send@1.0.0",
      target_id: "target-1",
      cursor: "next-page",
    });
    const plain = new URLSearchParams(workerQuery(noWorkerFilters, null));
    expect(Object.fromEntries(plain)).toEqual({
      limit: "50",
      state: "current",
      order: "last_seen_desc",
    });
    expect(
      new URLSearchParams(
        workerQuery({ ...noWorkerFilters, presence: "online" }, null),
      ).get("presence"),
    ).toBe("recent");
  });

  it("round-trips through the address without the host prefix", () => {
    const filters = {
      presence: "offline" as const,
      draining: false,
      release: "release-b",
      taskType: "crm-lookup@1.0.0",
      target: "target-1",
      hostPrefix: "",
      revoked: true,
      order: "registered_desc" as const,
    };
    const query = filtersToQuery(filters);
    expect(query).toBe(
      "?presence=offline&draining=false&release=release-b&task_type=crm-lookup%401.0.0&target=target-1&revoked=true&order=registered_desc",
    );
    expect(filtersFromQuery(query)).toEqual(filters);
    expect(filterCount(filters)).toBe(6);
    expect(filterCount({ ...filters, hostPrefix: "db-" })).toBe(7);
    expect(filtersToQuery({ ...filters, hostPrefix: "db-" })).toBe(query);
    expect(filtersFromQuery("?host_prefix=db-").hostPrefix).toBe("");
    expect(filtersToQuery(noWorkerFilters)).toBe("");
    expect(filtersFromQuery("?presence=busy&draining=yes&order=x")).toEqual(
      noWorkerFilters,
    );
    expect(filtersFromQuery("?draining=true").draining).toBe(true);
  });

  it("offers the releases and task types loaded, read and chosen", () => {
    const workers: WorkerRecord[] = [
      worker({ id: "a" }),
      worker({ id: "b", release_id: "release-b", task_types: ["x@1.0.0"] }),
      { id: "f", unavailable: true },
    ];
    expect(filterChoices(workers)).toEqual({
      releases: ["release-a", "release-b"],
      taskTypes: ["crm-lookup@1.0.0", "x@1.0.0"],
    });
    expect(
      filterChoices(workers, [{ id: "release-c", task_types: ["y@2.0.0"] }], {
        release: "release-z",
        taskType: "z@1.0.0",
      }),
    ).toEqual({
      releases: ["release-a", "release-b", "release-c", "release-z"],
      taskTypes: ["crm-lookup@1.0.0", "x@1.0.0", "y@2.0.0", "z@1.0.0"],
    });
  });
});

describe("where a worker runs", () => {
  it("names the target and component, the API process, Unmanaged or Unknown", () => {
    expect(
      workerWhere(
        worker({
          identity: { runtime: "container", host: "abc123" },
          placement: {
            target_id: "t",
            target_name: "eu-west",
            deployment_id: "d",
            component: "workers",
          },
        }),
      ),
    ).toBe("eu-west › workers");
    expect(workerWhere(worker({ identity: { runtime: "embedded" } }))).toBe(
      "In API process",
    );
    expect(workerWhere(worker({ identity: { runtime: "process" } }))).toBe(
      "Unmanaged",
    );
    expect(workerWhere(worker())).toBe("Unknown");
  });
  it("says the SDK and runtime versions it reports, and nothing it doesn't", () => {
    expect(
      workerVersion(
        worker({
          identity: {
            runtime: "process",
            sdk_version: "0.1.0a15",
            language_version: "3.12.4",
          },
        }),
      ),
    ).toBe("SDK 0.1.0a15 · Runtime 3.12.4");
    expect(workerVersion(worker())).toBe("");
  });
});

describe("releases and retirement", () => {
  it("lists a release's task types as workers register them", () => {
    expect(
      releaseTaskTypes({
        id: "r",
        image_digest: "sha256:" + "a".repeat(64),
        capabilities: [
          { taskType: "echo", taskVersion: "1.2.0" },
          { taskType: "crm-lookup", taskVersion: "1.0.0" },
        ],
      }),
    ).toEqual(["echo@1.2.0", "crm-lookup@1.0.0"]);
    expect(shortDigest("sha256:" + "ab".repeat(32))).toBe(
      "sha256:abababababab",
    );
  });
  it("says what a preview matched, the 100-worker batch, and what is left", () => {
    expect(retireSummary({ matched: 0, revoked: 0, dry_run: true })).toContain(
      "No worker matches",
    );
    expect(retireSummary({ matched: 1, revoked: 0, dry_run: true })).toBe(
      "1 worker matches. Retiring revokes it.",
    );
    expect(retireSummary({ matched: 250, revoked: 0, dry_run: true })).toBe(
      "250 workers match. One retirement revokes 100, oldest first; retire again for the rest.",
    );
    expect(retireSummary({ matched: 250, revoked: 100, dry_run: false })).toBe(
      "Revoked 100 workers. 150 workers still match.",
    );
    expect(retireSummary({ matched: 3, revoked: 3, dry_run: false })).toBe(
      "Revoked 3 workers.",
    );
  });
});
