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
// The workflows an execution reaches: the ones it calls, and through the
// drafts on this computer among them, the ones those call; each draft here
// goes with its source so the host runs it.
import { describe, expect, it, vi } from "vitest";
import { stringify } from "yaml";
import type { StudioApi } from "../src/app/api";
import {
  calledWorkflows,
  calleeDefinitions,
  studioCallees,
  undoDefinitions,
  type CalleesHost,
} from "../src/app/editor/calls/callee-drafts";
import { LocalDrafts, type DraftStorage } from "../src/app/local-drafts";

function memoryStorage(): DraftStorage {
  const items = new Map<string, string>();
  return {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}
const calling = (name: string, version: string, ...uses: string[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    steps: uses.map((reference, n) => ({
      id: `call-${n}`,
      kind: "callWorkflow",
      uses: reference,
      mode: "wait",
      with: { literal: {} },
    })),
    output: { literal: {} },
  },
});
function drafts(...documents: { doc: object; format: "yaml" | "json" }[]) {
  const store = new LocalDrafts(() => storage);
  const storage = memoryStorage();
  documents.forEach(({ doc, format }, n) => {
    const { name, version } = (
      doc as { metadata: { name: string; version: string } }
    ).metadata;
    store.save(
      { id: `d${n}`, name, version, savedAt: "2026-11-18T10:00:00Z" },
      {
        source: format === "json" ? JSON.stringify(doc) : stringify(doc),
        format,
        layout: null,
        savedAt: "2026-11-18T10:00:00Z",
      },
    );
  });
  return store;
}

describe("the workflows an execution reaches", () => {
  it("sends each draft here with its source, and follows the calls inside it", () => {
    const lookup = calling("lookup-customer", "0.1.0", "audit-trail@2.0.0");
    const store = drafts(
      { doc: lookup, format: "yaml" },
      { doc: calling("unrelated", "1.0.0"), format: "json" },
    );
    const reached = calledWorkflows(
      store,
      calling(
        "order-intake",
        "1.2.0",
        "lookup-customer@0.1.0",
        "notify-customer@1.0.0",
        "lookup-customer@0.1.0",
      ),
    );
    expect(reached.references).toEqual([
      "lookup-customer@0.1.0",
      "notify-customer@1.0.0",
      "audit-trail@2.0.0",
    ]);
    expect(reached.sources).toEqual({
      "lookup-customer@0.1.0": { source: stringify(lookup), format: "yaml" },
    });
  });

  it("stops at a draft that calls back, and reads no draft without storage", () => {
    const store = drafts({
      doc: calling("ping", "1.0.0", "order-intake@1.2.0"),
      format: "json",
    });
    const reached = calledWorkflows(
      store,
      calling("order-intake", "1.2.0", "ping@1.0.0"),
    );
    expect(reached.references).toEqual(["ping@1.0.0", "order-intake@1.2.0"]);
    expect(Object.keys(reached.sources)).toEqual(["ping@1.0.0"]);
    expect(
      calledWorkflows(undefined, calling("x", "1.0.0", "ping@1.0.0")),
    ).toEqual({ sources: {}, references: ["ping@1.0.0"] });
  });

  it("leaves a draft out when the project publishes the same version", () => {
    const store = drafts({
      doc: calling("lookup-customer", "0.1.0", "audit-trail@2.0.0"),
      format: "yaml",
    });
    const reached = calledWorkflows(
      store,
      calling("order-intake", "1.2.0", "lookup-customer@0.1.0"),
      (reference) => reference === "lookup-customer@0.1.0",
    );
    expect(reached).toEqual({
      sources: {},
      references: ["lookup-customer@0.1.0"],
    });
  });
});

describe("the workflows an execution sends right after a workflow opens", () => {
  const published = calling("lookup-customer", "0.1.0");
  // The project's catalog may still be on its way when Execute starts.
  const host = (answer: () => Promise<unknown>): CalleesHost => ({
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
    localDrafts: drafts({
      doc: calling("lookup-customer", "0.1.0", "audit-trail@2.0.0"),
      format: "yaml",
    }),
    model: {
      definition: calling("order-intake", "1.2.0", "lookup-customer@0.1.0"),
    },
  });

  it("waits for the project's published workflows, so the published version wins over a draft of it", async () => {
    let answer!: (lock: unknown) => void;
    const opened = host(() => new Promise((done) => (answer = done)));
    const documents = calleeDefinitions(opened);
    await new Promise((done) => setTimeout(done, 0));
    answer({ definitions: [{ document: published, digest: "0".repeat(64) }] });
    expect(await documents).toEqual([published]);
    expect(studioCallees(opened)).toEqual({
      sources: {},
      references: ["lookup-customer@0.1.0"],
    });
  });

  it("brings the published action that undoes a step, once, so its test output can be checked", async () => {
    const action = (name: string) => ({
      apiVersion: "weave/v1alpha1",
      kind: "Action",
      metadata: { name, version: "1.0.0" },
      spec: { outputSchema: { type: "object" } },
    });
    const undo = (name: string) => ({
      kind: "action",
      uses: `${name}@1.0.0`,
      with: { literal: {} },
    });
    const opened = {
      ...host(async () => ({
        definitions: ["confirm-hold", "release-hold", "remove", "other"].map(
          (name) => ({ document: action(name), digest: "0".repeat(64) }),
        ),
      })),
      model: {
        definition: {
          spec: {
            steps: [
              {
                id: "move",
                kind: "transaction",
                steps: [
                  {
                    id: "hold",
                    kind: "action",
                    uses: "other@1.0.0",
                    confirm: undo("confirm-hold"),
                    cancel: undo("release-hold"),
                  },
                  {
                    id: "again",
                    kind: "action",
                    uses: "other@1.0.0",
                    confirm: undo("confirm-hold"),
                    cancel: { ...undo("remove"), kind: "callWorkflow" },
                  },
                ],
              },
            ],
          },
        },
      },
    };
    await calleeDefinitions(opened);
    // What the editor sends already (release-hold) isn't sent again.
    expect(undoDefinitions(opened, [action("release-hold")])).toEqual([
      action("confirm-hold"),
    ]);
  });

  it("sends the draft when the project's workflows can't be read within 3 seconds", async () => {
    vi.useFakeTimers();
    try {
      const opened = host(() => new Promise(() => {}));
      let documents: unknown[] | null = null;
      void calleeDefinitions(opened).then((found) => (documents = found));
      await vi.advanceTimersByTimeAsync(2_900);
      expect(documents).toBeNull();
      await vi.advanceTimersByTimeAsync(100);
      expect(documents).toEqual([
        calling("lookup-customer", "0.1.0", "audit-trail@2.0.0"),
      ]);
    } finally {
      vi.useRealTimers();
    }
  });
});
