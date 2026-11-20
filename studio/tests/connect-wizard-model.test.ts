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
// The rules of connecting a local kind or k3d cluster from Clusters: the
// names and commands Studio shows, and how it recognizes the new target.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CONNECT_PATIENCE_MS,
  DEFAULT_INPUT,
  commandArguments,
  connectCommand,
  connectView,
  createCommand,
  clusterNameProblem,
  detect,
  keepWaiting,
  namespaceProblem,
  observe,
  observedLine,
  startWatch,
  targetName,
  type ConnectInput,
  type Detection,
} from "../src/app/operate/clusters/kubernetes-connect-model";
import type {
  Observation,
  Runner,
  Target,
} from "../src/app/operations/deployment-contracts";

interface NameRow {
  provider: "kind" | "k3d";
  cluster: string;
  namespace: string;
  target: string | null;
  cluster_problem: string | null;
  namespace_problem: string | null;
}
// The table the command line's own name rules are tested with.
const rows: NameRow[] = JSON.parse(
  readFileSync(
    resolve(
      import.meta.dirname,
      "../../tests/fixtures/kubernetes/target-names.json",
    ),
    "utf8",
  ),
);

const now = Date.parse("2026-11-19T12:00:00Z");
const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
const target = (overrides: Partial<Target> = {}): Target => ({
  id: "11111111-1111-4111-8111-111111111111",
  name: "kind-weave-test",
  adapter: "kubernetes",
  external_identity: "6f1c7e1a-4b55-4a8e-9a7e-0d6c1f0a2b3c",
  boundary: "weave-workers",
  runner_principal_id: "44444444-4444-4444-8444-444444444444",
  capabilities: ["observe", "update", "scale_workers", "restart_workers"],
  approval_policy: { distinct_approver: false },
  scope,
  revision: 1,
  disabled: false,
  created_at: "2026-11-19T11:59:00Z",
  ...overrides,
});
const runner = (overrides: Partial<Runner> = {}): Runner => ({
  id: "71111111-7777-4777-8777-777777777777",
  principal_id: "55555555-5555-4555-8555-555555555555",
  target_id: "11111111-1111-4111-8111-111111111111",
  adapter: "kubernetes",
  adapter_version: "2",
  capabilities: ["observe", "update", "scale_workers", "restart_workers"],
  last_seen: new Date(now - 5_000).toISOString(),
  expires_at: new Date(now + 85_000).toISOString(),
  revoked: false,
  ...overrides,
});
const observation = (overrides: Partial<Observation> = {}): Observation => ({
  id: "91111111-9999-4999-8999-999999999999",
  target_id: "11111111-1111-4111-8111-111111111111",
  target_revision: 1,
  observed_at: new Date(now - 12_000).toISOString(),
  expires_at: new Date(now + 300_000).toISOString(),
  digest: "sha256:0",
  resources: [],
  complete: true,
  settled: true,
  ...overrides,
});

describe("names", () => {
  it("follow the table the command line is tested with", () => {
    expect(rows.length).toBeGreaterThan(10);
    for (const row of rows) {
      const input: ConnectInput = row;
      const why = JSON.stringify(row);
      // Studio sets the provider with its own control, so its sentence leaves
      // out the command line's --provider wording.
      expect(clusterNameProblem(row.cluster), why).toBe(
        row.cluster_problem?.replace(
          "Use --provider kind or k3d and a cluster name",
          "Use a cluster name",
        ) ?? null,
      );
      expect(namespaceProblem(row.namespace), why).toBe(row.namespace_problem);
      if (row.target) expect(targetName(input), why).toBe(row.target);
    }
  });
});

describe("commands", () => {
  it("start from kind, weave-test and weave-workers", () => {
    expect(connectCommand(DEFAULT_INPUT)).toBe(
      "weave platform cluster connect --provider kind --name weave-test --namespace weave-workers",
    );
    expect(createCommand(DEFAULT_INPUT)).toBe(
      "weave platform cluster create --provider kind --name weave-test",
    );
  });

  it("name the provider, cluster and namespace they are given", () => {
    const input: ConnectInput = {
      provider: "k3d",
      cluster: "edge-2",
      namespace: "team-a",
    };
    expect(connectCommand(input)).toBe(
      "weave platform cluster connect --provider k3d --name edge-2 --namespace team-a",
    );
    expect(createCommand(input)).toBe(
      "weave platform cluster create --provider k3d --name edge-2",
    );
    expect(targetName(input)).toBe("k3d-edge-2.team-a");
  });

  it("wrap only between arguments, each flag with its value", () => {
    expect(commandArguments(connectCommand(DEFAULT_INPUT))).toEqual([
      "weave",
      "platform",
      "cluster",
      "connect",
      "--provider kind",
      "--name weave-test",
      "--namespace weave-workers",
    ]);
  });
});

describe("detection", () => {
  const input = DEFAULT_INPUT;

  it("waits, then sees the registered target, then its runner online", () => {
    expect(detect([], [], input, now)).toEqual({ state: "waiting" });
    const registered = target();
    expect(detect([registered], [], input, now)).toEqual({
      state: "registered",
      target: registered,
    });
    const online = runner();
    expect(detect([registered], [online], input, now)).toEqual({
      state: "connected",
      target: registered,
      runner: online,
    });
  });

  it("keeps a target registered while its runner's contact has expired or it is revoked", () => {
    const expired = runner({
      last_seen: new Date(now - 200_000).toISOString(),
      expires_at: new Date(now - 110_000).toISOString(),
    });
    expect(detect([target()], [expired], input, now).state).toBe("registered");
    expect(
      detect([target()], [runner({ revoked: true })], input, now).state,
    ).toBe("registered");
    // A runner of another target says nothing about this one.
    expect(
      detect(
        [target()],
        [runner({ target_id: "22222222-2222-4222-8222-222222222222" })],
        input,
        now,
      ).state,
    ).toBe("registered");
  });

  it("waits while the only target with the name is disabled, as disconnect leaves it", () => {
    expect(
      detect([target({ disabled: true })], [runner()], input, now),
    ).toEqual({ state: "waiting" });
  });

  it("reports a target with the same name and another adapter or boundary, never connected", () => {
    const compose = target({ adapter: "docker-compose" });
    expect(detect([compose], [runner()], input, now)).toEqual({
      state: "clash",
      target: compose,
    });
    const elsewhere = target({ boundary: "team-a" });
    expect(detect([elsewhere], [runner()], input, now)).toEqual({
      state: "clash",
      target: elsewhere,
    });
  });

  it("finds nothing for these inputs in a list that holds another cluster's target", () => {
    const other = target({
      name: "kind-weave-test.team-a",
      boundary: "team-a",
    });
    expect(detect([other], [runner()], input, now)).toEqual({
      state: "waiting",
    });
  });
});

describe("what the wizard says", () => {
  const registered = target();
  const connected: Detection = {
    state: "connected",
    target: registered,
    runner: runner(),
  };

  it("says already connected when the first answer finds the target connected", () => {
    const watch = observe(startWatch(now), connected, now + 1_000);
    expect(connectView(watch)).toBe("already");
  });

  it("says connected when the target connects while the wizard waits", () => {
    let watch = observe(startWatch(now), { state: "waiting" }, now + 5_000);
    expect(connectView(watch)).toBe("waiting");
    watch = observe(
      watch,
      { state: "registered", target: registered },
      now + 10_000,
    );
    expect(connectView(watch)).toBe("registered");
    watch = observe(watch, connected, now + 15_000);
    expect(connectView(watch)).toBe("connected");
  });

  it("runs out of patience after 15 minutes of waiting, and Keep waiting starts again", () => {
    let watch = startWatch(now);
    watch = observe(
      watch,
      { state: "waiting" },
      now + CONNECT_PATIENCE_MS - 1_000,
    );
    expect(watch.paused).toBe(false);
    watch = observe(watch, { state: "waiting" }, now + CONNECT_PATIENCE_MS);
    expect(watch.paused).toBe(true);
    expect(connectView(watch)).toBe("patience");
    watch = keepWaiting(watch, now + CONNECT_PATIENCE_MS + 60_000);
    expect(watch.paused).toBe(false);
    expect(connectView(watch)).toBe("waiting");
    watch = observe(watch, { state: "waiting" }, now + 2 * CONNECT_PATIENCE_MS);
    expect(watch.paused).toBe(false);
  });

  it("keeps its patience for a target that is registered", () => {
    const watch = observe(
      startWatch(now),
      { state: "registered", target: registered },
      now + 2 * CONNECT_PATIENCE_MS,
    );
    expect(watch.paused).toBe(false);
    expect(connectView(watch)).toBe("registered");
  });
});

describe("observedLine", () => {
  const id = "11111111-1111-4111-8111-111111111111";

  it("reads the newest observation of the target", () => {
    expect(
      observedLine(
        [
          observation({ observed_at: new Date(now - 300_000).toISOString() }),
          observation({ observed_at: new Date(now - 12_000).toISOString() }),
          observation({
            target_id: "22222222-2222-4222-8222-222222222222",
            observed_at: new Date(now - 1_000).toISOString(),
          }),
        ],
        id,
        now,
      ),
    ).toBe("Observed 12 s ago");
    expect(
      observedLine(
        [observation({ observed_at: new Date(now - 2_000).toISOString() })],
        id,
        now,
      ),
    ).toBe("Observed just now");
    expect(
      observedLine(
        [observation({ observed_at: new Date(now - 240_000).toISOString() })],
        id,
        now,
      ),
    ).toBe("Observed 4 min ago");
  });

  it("says not observed yet when the target has no observation", () => {
    expect(
      observedLine(
        [observation({ target_id: "22222222-2222-4222-8222-222222222222" })],
        id,
        now,
      ),
    ).toBe("Not observed yet");
  });
});
