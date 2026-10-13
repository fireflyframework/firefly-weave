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
import { ApiError, StudioApi } from "../src/app/api";
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
    refreshRunTaskAfterIdentity() {},
    loadIdentity: async () => {},
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

describe("sample ownership during background platform checks", () => {
  async function check(app: App) {
    await (
      app as unknown as { checkPlatform(): Promise<void> }
    ).checkPlatform();
  }
  it.each([
    { status: 401, detail: {} },
    { status: 403, detail: { code: "WV-AUTH-NOT-LINKED" } },
  ])("clears on authoritative rejection %j", async ({ status, detail }) => {
    const { app, controller } = fixture();
    const owner = controller.testEventOwner();
    Object.assign(app, {
      connection: {
        test: async () => {
          throw new ApiError(status, detail);
        },
      },
    });
    await check(app);
    expect(app.signInEnded).toBe(true);
    expect(controller.testEvent()).toBeUndefined();
    expect(owner()).toBe(false);
  });
  it.each([0, 503])(
    "retains the sample during transient offline status %i",
    async (status) => {
      const { app, controller } = fixture();
      const owner = controller.testEventOwner();
      Object.assign(app, {
        connection: {
          test: async () => {
            throw new ApiError(status, {});
          },
        },
      });
      await check(app);
      expect(controller.testEvent()).toEqual({ name: "private sample" });
      expect(owner()).toBe(true);
    },
  );
  it("retains the sample on same-account background renewal", async () => {
    const { app, controller } = fixture();
    const owner = controller.testEventOwner();
    Object.assign(app, {
      connection: {
        test: async () => ({
          session: structuredClone(app.api.session),
          identity: structuredClone(app.identity),
        }),
      },
    });
    await check(app);
    expect(controller.testEvent()).toEqual({ name: "private sample" });
    expect(owner()).toBe(true);
  });
});

describe("sample ownership through the real identity fallback", () => {
  it.each([0, 503, 401, 403])(
    "handles identity lookup failure %i without conflating offline and sign-out",
    async (status) => {
      const { app, controller } = fixture();
      const owner = controller.testEventOwner();
      Reflect.deleteProperty(app, "loadIdentity");
      app.api.request = vi
        .fn()
        .mockRejectedValue(
          new ApiError(
            status,
            status === 403 ? { code: "WV-AUTH-NOT-LINKED" } : {},
          ),
        );
      Object.assign(app, { connection: { test: async () => null } });
      await (
        app as unknown as { checkPlatform(): Promise<void> }
      ).checkPlatform();
      expect(app.identity).toBeNull();
      expect(controller.testEvent()).toEqual(
        status === 0 || status === 503 ? { name: "private sample" } : undefined,
      );
      expect(owner()).toBe(status === 0 || status === 503);
    },
  );
});

it.each([false, true])(
  "does not let an old identity rejection invalidate a newer sample scope (reset=%s)",
  async (reset) => {
    const { app, controller } = fixture();
    Reflect.deleteProperty(app, "loadIdentity");
    let reject!: (reason: unknown) => void;
    app.api.request = vi.fn().mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    const pending = app.loadIdentity();
    if (reset) app.wizardAccountProblem("expired");
    app.wizardVerified({
      session: structuredClone(app.api.session),
      identity: {
        principal_id: "other",
        kind: "human",
        grants: [],
        workspaces: [],
        truncated: false,
      },
    } as never);
    controller.setTestEvent({ name: "new sample" });
    const owner = controller.testEventOwner();
    reject(new ApiError(401, {}));
    await pending;
    expect(controller.testEvent()).toEqual({ name: "new sample" });
    expect(owner()).toBe(true);
  },
);

for (const status of [401, 403]) {
  it.each(["replacement", "renewal", "reset"])(
    `scopes a pending platform rejection ${status} across %s`,
    async (change) => {
      const { app, controller } = fixture();
      let reject!: (reason: unknown) => void;
      Object.assign(app, {
        connection: {
          test: () =>
            new Promise((_resolve, fail) => {
              reject = fail;
            }),
        },
      });
      const pending = (
        app as unknown as { checkPlatform(): Promise<void> }
      ).checkPlatform();
      if (change === "reset") app.wizardAccountProblem("expired");
      app.wizardVerified({
        session: structuredClone(app.api.session),
        identity: {
          principal_id: change === "replacement" ? "other" : "person",
          kind: "human",
          grants: [],
          workspaces: [],
          truncated: false,
        },
      } as never);
      controller.setTestEvent({ name: "new sample" });
      const owner = controller.testEventOwner();
      reject(
        new ApiError(
          status,
          status === 403 ? { code: "WV-AUTH-NOT-LINKED" } : {},
        ),
      );
      await pending;
      expect(controller.testEvent()).toEqual(
        change === "renewal" ? undefined : { name: "new sample" },
      );
      expect(owner()).toBe(change !== "renewal");
    },
  );
}
