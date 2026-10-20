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
  WizardState,
  intentChoices,
} from "../src/app/operate/clusters/change-request-wizard";
import type {
  Deployment,
  Observation,
  Target,
} from "../src/app/operations/deployment-contracts";
import type { WorkerStatus } from "../src/app/operate/workers/workers-model";

const scope = { tenant_id: "t", project_id: "p", environment_id: "e" };
const target: Target = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "local-docker",
  adapter: "docker-compose",
  external_identity: "engine",
  boundary: "weave-local-workers",
  runner_principal_id: "r",
  capabilities: [
    "observe",
    "deploy",
    "update",
    "scale_workers",
    "restart_workers",
  ],
  approval_policy: { distinct_approver: false },
  scope,
  revision: 1,
  disabled: false,
  created_at: "2026-10-20T10:00:00Z",
};
const deployment: Deployment = {
  id: "22222222-2222-4222-8222-222222222222",
  target_id: target.id,
  name: "local-workers",
  ownership: "managed",
  components: [
    {
      name: "fixture-worker",
      kind: "worker",
      image: "127.0.0.1:5000/example-sleep@sha256:" + "b".repeat(64),
      configuration: "local",
      replicas: 3,
      cpu_millis: 500,
      memory_mib: 1024,
      worker_release_id: "release",
    },
  ],
  scope,
  revision: 4,
  created_at: "2026-10-20T10:00:00Z",
};
const member = (ordinal: number) => ({
  host: `c0ffee00000${ordinal}`,
  ordinal,
  state: "running",
  ready: true,
  started_at: null,
  restarts: 0,
  node: null,
});
const observation: Observation = {
  id: "o",
  target_id: target.id,
  target_revision: 1,
  observed_at: "2026-10-20T11:59:00Z",
  expires_at: "2026-10-20T12:14:00Z",
  digest: "d".repeat(64),
  complete: true,
  settled: true,
  resources: [
    {
      name: "fixture-worker",
      external_identity: "compose/weave-local-workers/fixture-worker",
      kind: "worker",
      image: deployment.components[0].image,
      replicas: 3,
      ready_replicas: 3,
      version: "1",
      ownership: "managed",
      state: "ready",
      members: [member(1), member(2), member(3)],
    },
  ],
};
const worker = (host: string, leases: number) =>
  ({
    id: host,
    identity: { host, runtime: "container" },
    active_leases: leases,
  }) as unknown as WorkerStatus;

function wizard() {
  const state = new WizardState(
    [target],
    [deployment],
    [observation],
    [worker("c0ffee000003", 2), worker("c0ffee000002", 0)],
  );
  state.targetId = target.id;
  state.deploymentId = deployment.id;
  return state;
}

describe("the change request wizard", () => {
  it("moves on only once each step is complete, and goes back", () => {
    const state = new WizardState([target], [deployment]);
    expect(state.next()).toBe(false);
    expect(state.blocker).toBe("Choose a target.");
    state.targetId = target.id;
    expect(state.blocker).toBe("Choose what to change.");
    state.deploymentId = deployment.id;
    state.choose("scale_workers");
    expect(state.blocker).toBe("");
    expect(state.next()).toBe(true);
    expect(state.step).toBe(1);
    expect(state.back()).toBe(true);
    expect(state.step).toBe(0);
    expect(state.back()).toBe(false);
  });
  it("offers only the changes the target supports", () => {
    const narrow = {
      ...target,
      adapter: "kubernetes" as const,
      capabilities: ["observe" as const, "scale_workers" as const],
    };
    expect(
      intentChoices(narrow)
        .filter((choice) => choice.enabled)
        .map((choice) => choice.value),
    ).toEqual(["scale_workers"]);
  });
  it("shows the workers a scale-down removes and the tasks they still hold", () => {
    const state = wizard();
    state.choose("scale_workers");
    state.replicas = 1;
    expect(state.drains).toBe(true);
    expect(state.removals).toEqual([
      { host: "c0ffee000003", activeLeases: 2 },
      { host: "c0ffee000002", activeLeases: 0 },
    ]);
    state.replicas = 5;
    expect(state.drains).toBe(false);
    expect(state.removals).toEqual([]);
  });
  it("lets a restart skip letting workers finish first", () => {
    const state = wizard();
    state.choose("restart_workers");
    expect(state.drains).toBe(true);
    state.drainFirst = false;
    expect(state.drains).toBe(false);
  });
  it("creates only with a title and a plan that outlives the drain", () => {
    const state = wizard();
    state.choose("scale_workers");
    state.replicas = 1;
    state.next();
    state.next();
    expect(state.step).toBe(2);
    state.title = "";
    expect(state.blocker).toBe("Give the change a title.");
    expect(state.request()).toBeNull();
    state.title = "Scale fixture-worker to 1";
    state.planMinutes = 7;
    expect(state.blocker).toBe(
      "The plan must live at least 8 minutes to let the workers finish and apply.",
    );
    state.planMinutes = null;
    expect(state.request()).toEqual({
      target_id: target.id,
      deployment_id: deployment.id,
      expected_deployment_revision: 4,
      intent: "scale_workers",
      components: ["fixture-worker"],
      desired: [{ name: "fixture-worker", replicas: 1 }],
      drain: { mode: "drain_first", timeout_seconds: 300 },
      title: "Scale fixture-worker to 1",
      reason: "",
    });
  });
  it("suggests a title from the change", () => {
    const state = wizard();
    state.choose("scale_workers");
    state.replicas = 3;
    state.next();
    expect(state.title).toBe("Scale fixture-worker to 3");
  });
});
