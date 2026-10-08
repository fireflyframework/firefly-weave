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
  adapterIcon,
  runnerLabel,
  runnerLine,
  runnerState,
  runnerTone,
  targetRunner,
} from "../src/app/operate/clusters/cluster-model";
import type { Runner } from "../src/app/operations/deployment-contracts";

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
