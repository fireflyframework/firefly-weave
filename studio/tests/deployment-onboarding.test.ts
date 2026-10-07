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
  supportedCapabilities,
  supportedPlanComponents,
  runnerSetup,
  observedComponents,
} from "../src/app/operations/deployment-onboarding";
import type {
  Target,
  ObservedResource,
} from "../src/app/operations/deployment-contracts";
const target = {
  id: "target",
  adapter: "kubernetes",
  boundary: "team",
  external_identity: "namespace-uid",
  scope: {
    tenant_id: "tenant",
    project_id: "project",
    environment_id: "environment",
  },
  capabilities: ["observe", "deploy", "update"],
} as Target;
describe("existing destination onboarding", () => {
  it("offers creation only for the implemented Compose adapter", () => {
    expect(supportedCapabilities("docker-compose")).toContain("deploy");
    expect(supportedCapabilities("kubernetes")).not.toContain("deploy");
    expect(supportedCapabilities("azure-container-apps")).not.toContain(
      "deploy",
    );
  });
  it("uses known scope and pinned destination in a nonsecret observe-only local template", () => {
    const config = runnerSetup(target, "https://weave.example");
    expect(config.scope).toEqual(target.scope);
    expect(config.destination).toMatchObject({
      target_id: "target",
      external_identity: "namespace-uid",
      boundary: "team",
      capabilities: ["observe"],
    });
    expect(JSON.stringify(config)).not.toContain("client_secret");
  });
  it("imports only supported immutable observed components without guessing local aliases or worker admission", () => {
    const resources = [
      {
        name: "worker",
        kind: "worker",
        image: "registry/worker@sha256:" + "a".repeat(64),
        replicas: 2,
      },
      { name: "unknown", kind: "unknown", image: null, replicas: 1 },
      {
        name: "migration",
        kind: "migration",
        image: "registry/migration@sha256:" + "b".repeat(64),
        replicas: 1,
      },
      { name: "tag", kind: "api", image: "registry/api:latest", replicas: 1 },
    ] as ObservedResource[];
    expect(observedComponents(resources)).toEqual([
      {
        name: "worker",
        kind: "worker",
        image: resources[0].image,
        replicas: 2,
        configuration: "",
        cpu_millis: null,
        memory_mib: null,
        worker_release_id: null,
      },
    ]);
  });
});

it("refuses ACA API update plans without hiding supported worker scaling", () => {
  expect(
    supportedPlanComponents("azure-container-apps", "update", [
      { kind: "api" },
    ]),
  ).toBe(false);
  expect(
    supportedPlanComponents("azure-container-apps", "update", [
      { kind: "lumi" },
      { kind: "worker" },
    ]),
  ).toBe(true);
  expect(
    supportedPlanComponents("azure-container-apps", "scale_workers", [
      { kind: "api" },
      { kind: "worker" },
    ]),
  ).toBe(true);
  expect(
    supportedPlanComponents("kubernetes", "update", [{ kind: "api" }]),
  ).toBe(true);
});
