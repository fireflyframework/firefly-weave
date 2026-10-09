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
vi.hoisted(() => {
  vi.stubGlobal("document", { addEventListener: vi.fn() });
});
import { App } from "../src/app/app";
import { StudioApi } from "../src/app/api";
afterAll(() => vi.unstubAllGlobals());

const run = { id: "run-1", state: { active: ["review"] } };
const task = {
  id: "task-1",
  run_id: "run-1",
  node_id: "review",
  title: "Review expense",
};
const identity = () => ({
  principal_id: "human",
  kind: "human",
  grants: [
    {
      role: "reader",
      scope: {
        tenant_id: "tenant",
        project_id: "project",
        environment_id: "development",
      },
      resources: [],
      capabilities: ["human_task.read"],
    },
  ],
  workspaces: [],
  truncated: false,
});
function host() {
  const app = Object.create(App.prototype) as App;
  app.api = new StudioApi();
  app.api.session.profile = {
    name: "Test",
    baseUrl: "https://weave.example",
    tenantId: "tenant",
    projectId: "project",
    environmentId: "development",
  };
  app.view = "runs";
  app.selectedRecord = run;
  app.identity = identity();
  app.runTask = null;
  app.runNodes = () =>
    [{ step: { id: "review", kind: "humanTask" } }] as ReturnType<
      App["runNodes"]
    >;
  Object.assign(app, { cdr: { markForCheck: vi.fn() }, runTaskSequence: 0 });
  return app;
}
function find(app: App) {
  return (
    app as unknown as {
      findRunTask: (run: Record<string, unknown>) => Promise<void>;
    }
  ).findRunTask(run);
}
function delayed(app: App) {
  let release!: (value: {
    items: Record<string, unknown>[];
    next_cursor: null;
  }) => void;
  const ready = new Promise<{
    items: Record<string, unknown>[];
    next_cursor: null;
  }>((resolve) => (release = resolve));
  app.api.page = vi.fn(async (_collection, _environment, _cursor, filters) =>
    filters?.["status"] === "ready" ? ready : { items: [], next_cursor: null },
  );
  return () => release({ items: [task], next_cursor: null });
}
describe("waiting run task ownership", () => {
  for (const field of [
    "name",
    "baseUrl",
    "tenantId",
    "projectId",
    "environmentId",
  ] as const)
    it(`rejects a task when the current ${field} changes with the same run ID`, async () => {
      const app = host(),
        release = delayed(app),
        pending = find(app);
      app.api.session.profile![field] = "other";
      release();
      await pending;
      expect(app.runTask).toBeNull();
    });
  it("rejects a task read under an older identity", async () => {
    const app = host(),
      release = delayed(app),
      pending = find(app);
    app.identity = identity();
    release();
    await pending;
    expect(app.runTask).toBeNull();
  });
  it("rejects a task after task access is removed", async () => {
    const app = host(),
      release = delayed(app),
      pending = find(app);
    app.identity!.grants[0].capabilities = [];
    release();
    await pending;
    expect(app.runTask).toBeNull();
  });
  it("rejects a task after the same run is reopened", async () => {
    const app = host(),
      release = delayed(app),
      pending = find(app);
    app.selectedRecord = structuredClone(run);
    release();
    await pending;
    expect(app.runTask).toBeNull();
  });
  it("clears a cached task when authoritative identity loses task access", async () => {
    const app = host();
    app.runTask = task;
    app.api.request = vi.fn(async () => ({
      ...identity(),
      grants: [],
    })) as typeof app.api.request;
    await app.loadIdentity();
    expect(app.runTask).toBeNull();
    expect(app.canReadTasks).toBe(false);
  });
  it("keeps the newest task read when the same run is refreshed", async () => {
    const app = host(),
      release = delayed(app),
      older = find(app);
    const currentTask = { ...task, title: "Updated review" };
    app.api.page = vi.fn(
      async (_collection, _environment, _cursor, filters) => ({
        items: filters?.["status"] === "ready" ? [currentTask] : [],
        next_cursor: null,
      }),
    );
    await find(app);
    release();
    await older;
    expect(app.runTask).toEqual(currentTask);
  });
  it("rejects a task after leaving Runs", async () => {
    const app = host(),
      release = delayed(app),
      pending = find(app);
    app.view = "tasks";
    release();
    await pending;
    expect(app.runTask).toBeNull();
  });
  it("does not clear the current task when an older detail finishes loading", async () => {
    const app = host();
    app.selectedRecord = structuredClone(run);
    app.runTask = task;
    app.api.page = vi.fn();
    await find(app);
    expect(app.runTask).toEqual(task);
    expect(app.api.page).not.toHaveBeenCalled();
  });
  it("enriches the open run from identity returned by a platform check", async () => {
    const app = host();
    app.identity = null;
    app.api.page = vi.fn(
      async (_collection, _environment, _cursor, filters) => ({
        items: filters?.["status"] === "ready" ? [task] : [],
        next_cursor: null,
      }),
    );
    Object.assign(app, {
      platformGeneration: 1,
      connection: { test: async () => ({ identity: identity() }) },
      renewedStatus: vi.fn(),
    });
    await (
      app as unknown as { checkPlatform: (generation: number) => Promise<void> }
    ).checkPlatform(1);
    await vi.waitFor(() => expect(app.runTask).toEqual(task));
  });
  it("clears the open run's cached task when identity cannot be read", async () => {
    const app = host();
    app.runTask = task;
    app.api.request = vi.fn(async () => {
      throw Error("Unavailable");
    });
    await app.loadIdentity();
    expect(app.identity).toBeNull();
    expect(app.runTask).toBeNull();
  });
  it("keeps a task read for the current run and authority", async () => {
    const app = host(),
      release = delayed(app),
      pending = find(app);
    release();
    await pending;
    expect(app.runTask).toEqual(task);
  });
});
