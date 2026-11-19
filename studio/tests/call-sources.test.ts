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
// Where the editor of one window finds the workflows a step may call: the
// project's catalog lock and the drafts on this computer, read once and kept
// per account and workspace.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { stringify } from "yaml";
import type { StudioApi } from "../src/app/api";
import {
  calledRemovalQuestion,
  CALLERS_WAIT_MS,
  CallSources,
  workflowRows,
  type CallSourcesHost,
} from "../src/app/editor/calls/call-sources";
import { LocalDrafts, type DraftStorage } from "../src/app/local-drafts";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/callable-corpus.json"),
    "utf8",
  ),
) as { lists: { lock: unknown }[] };
const LOCK = corpus.lists[0].lock;

function memoryStorage(): DraftStorage {
  const items = new Map<string, string>();
  return {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}
const workflow = (name: string, version: string, callable?: unknown) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [],
    output: { literal: {} },
    ...(callable === undefined ? {} : { callable }),
  },
});

function sources(options: { connected?: boolean; canRead?: boolean } = {}) {
  const asked: string[] = [];
  const storage = memoryStorage();
  const drafts = new LocalDrafts(() => storage);
  drafts.save(
    {
      id: "d1",
      name: "lookup-customer",
      version: "0.1.0",
      savedAt: "2026-11-18T10:00:00Z",
    },
    {
      source: stringify(workflow("lookup-customer", "0.1.0", {})),
      format: "yaml",
      layout: null,
      savedAt: "2026-11-18T10:00:00Z",
    },
  );
  drafts.save(
    {
      id: "d2",
      name: "broken-draft",
      version: "0.1.0",
      savedAt: "2026-11-18T09:00:00Z",
    },
    {
      source: "spec: [",
      format: "yaml",
      layout: null,
      savedAt: "2026-11-18T09:00:00Z",
    },
  );
  const host: CallSourcesHost = {
    profile:
      options.connected === false
        ? null
        : {
            name: "acme",
            baseUrl: "https://weave.example",
            tenantId: "t",
            projectId: "p",
            environmentId: "e",
          },
    stepDataScope: "account-1",
    api: {
      project: "/studio/api/api/v1/tenants/t/projects/p",
      request: async (path: string) => {
        asked.push(path);
        return LOCK;
      },
    } as unknown as StudioApi,
    can: (capability) =>
      capability === "catalog.read" && options.canRead !== false,
    localDrafts: drafts,
  };
  return { made: new CallSources(host), asked, drafts, host };
}
const settle = () => new Promise((done) => setTimeout(done, 0));

describe("the workflows a step may call", () => {
  it("reads the project's catalog lock once, from the project's catalog", async () => {
    const { made, asked } = sources();
    made.list("order-intake");
    made.list("order-intake");
    await settle();
    made.list("order-intake");
    expect(asked).toEqual(["/studio/api/api/v1/tenants/t/projects/p/catalog"]);
  });

  it("reads the lock again for another account", async () => {
    const { made, asked, host } = sources();
    made.list("order-intake");
    await settle();
    (host as { stepDataScope?: string }).stepDataScope = "account-2";
    made.list("order-intake");
    await settle();
    expect(asked).toHaveLength(2);
  });

  it("doesn't call a version missing while the project's versions load", () => {
    const { made } = sources();
    expect(made.note("missing@1.0.0")).toBeNull();
  });

  it("lists the project's versions, then the drafts on this computer", async () => {
    const { made } = sources();
    made.list("order-intake");
    await settle();
    const uses = made.list("order-intake").items.map((item) => item.uses);
    expect(uses.slice(0, 2)).toEqual([
      "customer-profile@2.0.0",
      "notify-customer@2.0.0-rc.1",
    ]);
    expect(uses.slice(-2)).toEqual([
      "broken-draft@0.1.0",
      "lookup-customer@0.1.0",
    ]);
  });

  it("finds drafts on this computer alone when not connected or not allowed to read the catalog", async () => {
    for (const options of [{ connected: false }, { canRead: false }]) {
      const { made, asked } = sources(options);
      const list = made.list("order-intake");
      await settle();
      expect(asked).toEqual([]);
      expect(list.items.map((item) => item.source)).toEqual(["local", "local"]);
      expect(list.note).toBe(
        options.connected === false
          ? "Connect to a platform to call published workflows."
          : "You can't read this project's published workflows here.",
      );
    }
  });

  it("answers a called version's contract from the lock or a draft, and what can't be read as unknown", async () => {
    const { made } = sources();
    made.list("order-intake");
    await settle();
    expect(made.contract("notify-customer@1.0.0")).toMatchObject({
      source: "project",
      callable: {},
    });
    expect(made.contract("lookup-customer@0.1.0")).toMatchObject({
      source: "local",
      callable: {},
    });
    expect(made.contract("broken-draft@0.1.0")).toMatchObject({
      source: "local",
      inputSchema: null,
    });
    expect(made.contract("missing@1.0.0")).toBeNull();
  });

  it("says why a chosen version can't be found, and why the project's are missing", async () => {
    const { made } = sources();
    made.list("order-intake");
    await settle();
    expect(made.note("missing@1.0.0")).toBe(
      "Studio can't find missing 1.0.0 in this project.",
    );
    expect(made.note("notify-customer@1.0.0")).toBeNull();
    expect(made.note()).toBeNull();
    const offline = sources({ connected: false }).made;
    expect(offline.note("notify-customer@1.0.0")).toBe(
      "Connect to a platform to call published workflows.",
    );
    expect(offline.note("lookup-customer@0.1.0")).toBeNull();
    expect(offline.note()).toBe(
      "Connect to a platform to call published workflows.",
    );
  });

  it("reads a draft saved again, not the copy it read before", async () => {
    const { made, drafts } = sources({ connected: false });
    expect(made.contract("lookup-customer@0.1.0")?.["callable"]).toEqual({});
    drafts.save(
      {
        id: "d1",
        name: "lookup-customer",
        version: "0.1.0",
        savedAt: "2026-11-18T11:00:00Z",
      },
      {
        source: JSON.stringify(
          workflow("lookup-customer", "0.1.0", { allowedCallers: ["x"] }),
        ),
        format: "json",
        layout: null,
        savedAt: "2026-11-18T11:00:00Z",
      },
    );
    made.refresh();
    expect(made.contract("lookup-customer@0.1.0")?.["callable"]).toEqual({
      allowedCallers: ["x"],
    });
  });
});

describe("the catalog port's rows", () => {
  it("say where each version lives, under its workflow's name, with why it can't be chosen", async () => {
    const { made } = sources();
    made.list("order-intake");
    await settle();
    const rows = workflowRows(made.list("order-intake"));
    expect(rows.find((row) => row.uses === "notify-customer@1.9.0")).toEqual({
      uses: "notify-customer@1.9.0",
      title: "notify-customer 1.9.0",
      group: "In this project",
      heading: "notify-customer",
      disabled:
        "This workflow version does not allow calls from order-intake; add order-intake to its callable.allowedCallers in a new version.",
    });
    expect(rows.find((row) => row.uses === "lookup-customer@0.1.0")).toEqual({
      uses: "lookup-customer@0.1.0",
      title: "lookup-customer 0.1.0",
      group: "On this computer",
      heading: "lookup-customer",
      aside: "Draft",
    });
    expect(rows.find((row) => row.uses === "broken-draft@0.1.0")).toEqual({
      uses: "broken-draft@0.1.0",
      title: "broken-draft 0.1.0",
      group: "On this computer",
      heading: "broken-draft",
      aside: "Draft",
      note: "Studio can't tell whether broken-draft 0.1.0 allows this workflow.",
    });
  });
});

describe("the question before Called by a workflow goes", () => {
  const callee = {
    metadata: { name: "notify-customer" },
    spec: { callable: { allowedCallers: ["order-intake"] } },
  };
  const host = (answer: () => Promise<unknown>): CallSourcesHost => ({
    profile: {
      name: "acme",
      baseUrl: "https://weave.example",
      tenantId: "t",
      projectId: "p",
      environmentId: "e",
    },
    stepDataScope: "account-1",
    api: {
      project: "/studio/api/api/v1/tenants/t/projects/p",
      request: answer,
    } as unknown as StudioApi,
    can: () => true,
  });

  it("names the published callers it reads", async () => {
    const lock = {
      definitions: [
        {
          document: workflowCalling("order-intake", "1.2.0"),
          digest: "0".repeat(64),
        },
      ],
    };
    const question = await calledRemovalQuestion(
      host(async () => lock),
      callee,
    );
    expect(question.message.split(/\n\s*\n/).slice(0, 2)).toEqual([
      "order-intake calls this workflow. Its next activation will fail until it stops calling it.",
      "order-intake 1.2.0 · step notify",
    ]);
  });

  it("asks in general when the callers can't be read in time", async () => {
    vi.useFakeTimers();
    try {
      const asked = calledRemovalQuestion(
        host(() => new Promise(() => {})),
        callee,
      );
      await vi.advanceTimersByTimeAsync(CALLERS_WAIT_MS);
      expect((await asked).message.split(/\n\s*\n/)[0]).toBe(
        "Workflows that call this one fail at their next activation until they stop calling it.",
      );
    } finally {
      vi.useRealTimers();
    }
  });
});

function workflowCalling(name: string, version: string) {
  return {
    ...workflow(name, version),
    spec: {
      ...workflow(name, version).spec,
      steps: [
        {
          id: "notify",
          kind: "callWorkflow",
          uses: "notify-customer@1.0.0",
          mode: "wait",
          with: { literal: {} },
        },
      ],
    },
  };
}
