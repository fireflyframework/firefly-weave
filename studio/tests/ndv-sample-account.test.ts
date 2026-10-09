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
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() => vi.stubGlobal("document", { addEventListener: vi.fn() }));
import { App } from "../src/app/app";
import { StudioApi } from "../src/app/api";
import { StructuredCanvasAdapter } from "../src/app/model";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
afterAll(() => vi.unstubAllGlobals());

function fixture() {
  const app = Object.create(App.prototype) as App;
  app.api = new StudioApi();
  app.api.session.profile = {
    name: "Test",
    baseUrl: "https://weave.example",
    tenantId: "t",
    projectId: "p",
    environmentId: "dev",
  };
  app.model = new StructuredCanvasAdapter();
  app.identity = {
    principal_id: "person",
    kind: "human",
    grants: [],
    workspaces: [],
    truncated: false,
  };
  Object.assign(app, {
    stepDataGeneration: 0,
    platformGeneration: 0,
    importGeneration: 0,
    listSequence: 0,
    adminGeneration: 0,
    catalogGeneration: 0,
    contractGeneration: 0,
    contractRevision: 0,
    knownRuns: new Map(),
    workflowVersions: new Map(),
    contractCache: new Map(),
    cdr: { markForCheck() {} },
    closeRecord() {},
    closeSimulation() {},
    refreshConnectionStatus: async () => {},
    catalogAfterIdentity() {},
    renewedStatus() {},
    detachPlatformDraft() {},
    loginWatcher: { stop() {} },
  });
  const controller = new StepDetailsController(app);
  controller.setTestEvent({ name: "private sample" });
  return { app, controller };
}

describe("sample ownership at the real App account boundary", () => {
  it.each(["expired", "not-linked"] as const)(
    "clears on %s with the same API, profile and workflow",
    (kind) => {
      const { app, controller } = fixture();
      const owner = controller.testEventOwner();
      const profile = app.profile,
        api = app.api,
        model = app.model;
      app.wizardAccountProblem(kind);
      expect(app.api).toBe(api);
      expect(app.profile).toBe(profile);
      expect(app.model).toBe(model);
      expect(controller.testEvent()).toBeUndefined();
      expect(owner()).toBe(false);
    },
  );
  it("retains on same-account verification with a fresh profile object", () => {
    const { app, controller } = fixture();
    const owner = controller.testEventOwner();
    app.wizardVerified({
      session: structuredClone(app.api.session),
      identity: structuredClone(app.identity!),
    } as never);
    expect(controller.testEvent()).toEqual({ name: "private sample" });
    expect(owner()).toBe(true);
  });
  it("clears on a different verified principal", () => {
    const { app, controller } = fixture();
    app.wizardVerified({
      session: structuredClone(app.api.session),
      identity: { ...app.identity, principal_id: "other" },
    } as never);
    expect(controller.testEvent()).toBeUndefined();
  });
  it("clears on workspace change even when profile values are unchanged", () => {
    const { app, controller } = fixture();
    app.wizardWorkspace({
      session: app.api.session,
      identity: app.identity,
    } as never);
    expect(controller.testEvent()).toBeUndefined();
  });
  it("clears on a connection switch with the same open workflow", () => {
    const { app, controller } = fixture();
    app.applyConnection({ session: app.api.session });
    expect(controller.testEvent()).toBeUndefined();
  });
});
