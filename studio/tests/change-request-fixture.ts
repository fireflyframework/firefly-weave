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
// One change request for the Studio tests, at a fixed server time.
import type { ChangeRequest } from "../src/app/operations/deployment-contracts";

export const serverNow = Date.parse("2026-10-20T12:00:00Z");
export const at = (seconds: number) =>
  new Date(serverNow + seconds * 1000).toISOString();

export function change(overrides: Partial<ChangeRequest> = {}): ChangeRequest {
  return {
    id: "c1c1c1c1-0000-4000-8000-000000000001",
    target_id: "11111111-1111-4111-8111-111111111111",
    deployment_id: "22222222-2222-4222-8222-222222222222",
    expected_deployment_revision: 1,
    intent: "scale_workers",
    components: ["fixture-worker"],
    desired: [{ name: "fixture-worker", replicas: 3 }],
    drain: { mode: "none", timeout_seconds: 300 },
    title: "Scale fixture-worker to 3",
    reason: "",
    plan_ttl_seconds: 600,
    scope: { tenant_id: "t", project_id: "p", environment_id: "e" },
    revision: 3,
    requested_by: "ana",
    created_at: at(-60),
    deployment_revision: 2,
    approval_policy: { distinct_approver: true },
    state: "awaiting_approval",
    observation_job_id: null,
    observation_id: null,
    plan_id: "p1",
    plan_digest: "a".repeat(64),
    plan_expires_at: at(582),
    approval: null,
    planned_by: "ana",
    drain_set: [],
    drained: [],
    apply_job_id: null,
    failure: null,
    warnings: [],
    history: [
      { at: at(-60), state: "observing", actor_id: "ana" },
      { at: at(-30), state: "ready_to_plan", actor_id: null },
      { at: at(-20), state: "awaiting_approval", actor_id: "ana" },
    ],
    ...overrides,
  };
}
