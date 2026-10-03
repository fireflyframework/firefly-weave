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
  insertApiAction,
  insertCatalogAction,
  requirementOf,
  startFromTemplate,
  useApiAction,
} from "../src/app/integrations/editor-bridge";
import type { EditorHost } from "../src/app/integrations/editor-host";
import type { HttpActionUse } from "../src/app/integrations/http-action-builder";
import { StructuredCanvasAdapter, type Workflow } from "../src/app/model";

const sqlAction = {
  kind: "Action",
  metadata: { name: "sql.lookup", version: "1.0.0" },
  spec: { connection: { connector: "weave-postgresql@1.0.0" } },
};
const workerAction = {
  kind: "Action",
  metadata: { name: "crm.lookup", version: "2.0.0" },
  spec: { implementation: { kind: "worker", taskType: "crm" } },
};
const exports: Record<string, unknown> = {
  a1: { document: sqlAction },
  a2: { document: workerAction },
};

function workflow(connections?: Record<string, unknown>): Workflow {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "pets", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      ...(connections ? { connections } : {}),
      steps: [],
      output: { literal: {} },
    },
  };
}

/**
 * An EditorHost over a real model, with a platform answering action exports
 * (after `held` settles, when given).
 */
function host(definition = workflow(), fail = false, held?: Promise<void>) {
  const model = new StructuredCanvasAdapter();
  model.replace(definition);
  const contracts = new Map<string, Record<string, unknown>>();
  const requests: string[] = [];
  const profile = {
    name: "p",
    baseUrl: "https://weave.invalid",
    tenantId: "t",
    projectId: "p",
    environmentId: "e",
  };
  const fake = {
    api: {
      project: "/studio/api/api/v1/tenants/t/projects/p",
      async request(path: string) {
        requests.push(path);
        if (held) await held;
        if (fail) throw Error("unavailable");
        return exports[path.split("/").at(-2)!];
      },
    },
    model,
    profile,
    identity: null,
    view: "designer",
    can: () => true,
    actionVersions: [
      { id: "a1", name: "sql.lookup", version: "1.0.0" },
      { id: "a2", name: "crm.lookup", version: "2.0.0" },
    ],
    actionNextCursor: null,
    catalogState: "ready",
    catalogError: null,
    catalogAppending: false,
    get catalogContracts() {
      return new Map(contracts);
    },
    async loadActionCatalog() {},
    cacheContract(uses: string, document: Record<string, unknown> | null) {
      if (document) contracts.set(uses, document);
      else contracts.delete(uses);
    },
    get selected() {
      return model.nodes().find((n) => n.step.id === model.selected);
    },
    get workflowSlots() {
      const slots = (model.definition.spec["connections"] ?? {}) as Record<
        string,
        { connector?: string; required?: boolean }
      >;
      return Object.entries(slots).map(([name, slot]) => ({
        name,
        connector: String(slot.connector ?? ""),
        required: slot.required !== false,
      }));
    },
    perform(edit: () => void) {
      try {
        edit();
        this.error = "";
      } catch (e) {
        this.error = (e as Error).message;
      }
    },
    async ensureApplied() {
      return true;
    },
    focused: [] as string[],
    focusStep(id: string) {
      this.focused.push(id);
    },
    focusInspector() {},
    newWorkflow() {
      model.replace(workflow());
    },
    scheduleFit() {},
    refreshView() {},
    error: "",
    message: "",
    dirty: false,
    showInspector: false,
    showPalette: true,
    apiBuilder: null,
    lastUse: null,
    connectionDialog: null,
    showTemplates: false,
    async openApiBuilder() {},
    openConnectionDialog() {},
    async refresh() {},
  };
  return { host: fake as typeof fake & EditorHost, contracts, requests };
}

const use = (placeholder = false): HttpActionUse => ({
  uses: "get-pet@1.0.0",
  action: { kind: "Action", metadata: { name: "get-pet", version: "1.0.0" } },
  connector: "weave-http@2.0.0",
  slot: "pet-store",
  placeholder,
  connection: {
    name: "pet-store",
    baseUrl: "https://api.pets.example",
    auth: { kind: "api-key", header: "X-API-Key" },
  },
});

describe("inserting a published action", () => {
  it("declares the slot it needs and inserts the step as one undo step", async () => {
    const { host: h, contracts, requests } = host();
    const id = await insertCatalogAction(h, {
      uses: "sql.lookup@1.0.0",
      id: "a1",
    });
    expect(id).toBe("call-action-1");
    expect(requests).toEqual([
      "/studio/api/api/v1/tenants/t/projects/p/actions/a1/export",
    ]);
    expect(contracts.get("sql.lookup@1.0.0")).toEqual(sqlAction);
    expect(h.model.definition.spec.steps[0]).toMatchObject({
      uses: "sql.lookup@1.0.0",
      connection: "weave-postgresql",
    });
    expect(h.model.definition.spec["connections"]).toEqual({
      "weave-postgresql": {
        connector: "weave-postgresql@1.0.0",
        required: true,
      },
    });
    expect(h.message).toContain("added the connection slot weave-postgresql");
    expect(h.focused).toEqual(["call-action-1"]);
    expect(h.showPalette).toBe(false);
    h.model.undo();
    expect(h.model.definition.spec.steps).toEqual([]);
    expect(h.model.definition.spec["connections"]).toBeUndefined();
  });
  it("binds the only compatible slot, using a cached contract", async () => {
    const { host: h, requests } = host(
      workflow({ db: { connector: "weave-postgresql@1.0.0" } }),
    );
    h.cacheContract("sql.lookup@1.0.0", sqlAction);
    await insertCatalogAction(h, { uses: "sql.lookup@1.0.0" });
    expect(requests).toEqual([]);
    expect(h.model.definition.spec.steps[0]).toMatchObject({
      connection: "db",
    });
    expect(Object.keys(h.model.definition.spec["connections"]!)).toEqual([
      "db",
    ]);
  });
  it("leaves the slot to the person when several fit", async () => {
    const { host: h } = host(
      workflow({
        primary: { connector: "weave-postgresql@1.0.0" },
        replica: { connector: "weave-postgresql@1.0.0" },
      }),
    );
    await insertCatalogAction(h, { uses: "sql.lookup@1.0.0", id: "a1" });
    expect(h.model.definition.spec.steps[0]["connection"]).toBeUndefined();
    expect(h.message).toContain("Choose its connection slot in the inspector");
  });
  it("needs no slot for a worker action, and guesses none without the contract", async () => {
    const worker = host();
    await insertCatalogAction(worker.host, {
      uses: "crm.lookup@2.0.0",
      id: "a2",
    });
    expect(worker.host.model.definition.spec.steps[0]).not.toHaveProperty(
      "connection",
    );
    expect(worker.host.model.definition.spec["connections"]).toBeUndefined();
    const offline = host(workflow(), true);
    await insertCatalogAction(offline.host, {
      uses: "sql.lookup@1.0.0",
      id: "a1",
    });
    expect(offline.host.model.definition.spec.steps[0]).toEqual(
      expect.objectContaining({ uses: "sql.lookup@1.0.0" }),
    );
    expect(offline.host.model.definition.spec["connections"]).toBeUndefined();
  });
  it("says when the action's details didn't load, so no slot was chosen", async () => {
    const { host: h } = host(workflow(), true);
    await insertCatalogAction(h, { uses: "sql.lookup@1.0.0", id: "a1" });
    expect(h.model.definition.spec.steps).toHaveLength(1);
    expect(h.message).toContain("Its details didn't load");
  });
  it("drops an insert whose details arrive after another workflow opened", async () => {
    let release = () => {};
    const held = new Promise<void>((done) => (release = done));
    const { host: h, requests } = host(workflow(), false, held);
    const pending = insertCatalogAction(h, {
      uses: "sql.lookup@1.0.0",
      id: "a1",
    });
    // Another workflow opens while the action's details load.
    while (!requests.length) await Promise.resolve();
    h.model.replace(workflow({ api: { connector: "weave-http@2.0.0" } }));
    release();
    expect(await pending).toBe("");
    expect(h.model.definition.spec.steps).toEqual([]);
    expect(Object.keys(h.model.definition.spec["connections"]!)).toEqual([
      "api",
    ]);
    expect(h.model.canUndo).toBe(false);
    expect(h.focused).toEqual([]);
  });
  it("drops an insert whose details arrive after the person left the designer", async () => {
    let release = () => {};
    const held = new Promise<void>((done) => (release = done));
    const { host: h } = host(workflow(), false, held);
    const pending = insertCatalogAction(h, {
      uses: "sql.lookup@1.0.0",
      id: "a1",
    });
    // The person leaves before unapplied edits are even settled.
    h.view = "home";
    release();
    expect(await pending).toBe("");
    expect(h.model.definition.spec.steps).toEqual([]);
  });
  it("reads only a well-formed connection requirement", () => {
    expect(requirementOf(sqlAction)).toEqual({
      connector: "weave-postgresql@1.0.0",
    });
    expect(
      requirementOf({
        spec: { connection: { connector: "x@1.0.0", required: false } },
      }),
    ).toEqual({ connector: "x@1.0.0", required: false });
    expect(
      requirementOf({ spec: { connection: { connector: 7 } } }),
    ).toBeNull();
    expect(requirementOf(null)).toBeNull();
  });
});

describe("using an action from the API action builder", () => {
  it("inserts it with the API's slot and remembers the hand-off", () => {
    const { host: h, contracts } = host();
    h.apiBuilder = { context: "workflow", tab: "describe", step: "" };
    insertApiAction(h, use());
    expect(h.apiBuilder).toBeNull();
    expect(h.model.definition.spec.steps[0]).toMatchObject({
      uses: "get-pet@1.0.0",
      connection: "pet-store",
    });
    expect(h.model.definition.spec["connections"]).toEqual({
      "pet-store": { connector: "weave-http@2.0.0", required: true },
    });
    expect(contracts.has("get-pet@1.0.0")).toBe(true);
    expect(h.lastUse?.slot).toBe("pet-store");
    // The hand-off carries names and the origin only.
    expect(JSON.stringify(h.lastUse?.connection)).not.toMatch(/secret|value/i);
  });
  it("never caches a placeholder's contract", () => {
    const { host: h, contracts } = host();
    insertApiAction(h, use(true));
    expect(contracts.size).toBe(0);
    expect(h.model.definition.spec.steps).toHaveLength(1);
  });
  it("renames the slot when its name belongs to another connector", () => {
    const { host: h } = host(
      workflow({ "pet-store": { connector: "crm@1.0.0" } }),
    );
    insertApiAction(h, use());
    expect(h.model.definition.spec.steps[0]["connection"]).toBe("pet-store-2");
    expect(h.lastUse?.slot).toBe("pet-store-2");
  });
  it("fills the step it was opened for, as one undo step", () => {
    const { host: h } = host();
    h.perform(() => h.model.insert("action", "root", 0));
    h.model.clearHistory();
    h.apiBuilder = { context: "step", tab: "describe", step: "call-action-1" };
    expect(useApiAction(h, use())).toBe("call-action-1");
    expect(h.apiBuilder).toBeNull();
    expect(h.model.definition.spec.steps).toHaveLength(1);
    expect(h.model.definition.spec.steps[0]).toMatchObject({
      id: "call-action-1",
      uses: "get-pet@1.0.0",
      connection: "pet-store",
    });
    expect(h.showInspector).toBe(true);
    h.model.undo();
    expect(h.model.definition.spec.steps[0]["connection"]).toBeUndefined();
    expect(h.model.definition.spec["connections"]).toBeUndefined();
    expect(h.model.canUndo).toBe(false);
  });
  it("keeps a compatible slot the step already uses, like a template's", () => {
    const { host: h } = host(
      workflow({ api: { connector: "weave-http@2.0.0" } }),
    );
    h.perform(() =>
      h.model.insert("action", "root", 0, {
        uses: "your-action@1.0.0",
        connection: "api",
      }),
    );
    h.apiBuilder = { context: "step", tab: "describe", step: "call-action-1" };
    useApiAction(h, use());
    expect(h.model.definition.spec.steps[0]).toMatchObject({
      uses: "get-pet@1.0.0",
      connection: "api",
    });
    expect(Object.keys(h.model.definition.spec["connections"]!)).toEqual([
      "api",
    ]);
    // The connection form is offered for that slot, prefilled from the builder.
    expect(h.lastUse?.slot).toBe("api");
    expect(h.lastUse?.connection.name).toBe("pet-store");
  });
  it("inserts a new step when the step it was opened for is gone", () => {
    const { host: h } = host();
    h.apiBuilder = { context: "step", tab: "describe", step: "action-9" };
    expect(useApiAction(h, use())).toBe("call-action-1");
    expect(h.model.definition.spec.steps).toHaveLength(1);
  });
});

describe("templates", () => {
  it("open as a new draft without undo history", () => {
    const { host: h } = host();
    h.showTemplates = true;
    const yaml = [
      "apiVersion: weave/v1alpha1",
      "kind: Workflow",
      "metadata:",
      "  name: approval # from a template",
      "  version: 1.0.0",
      "spec:",
      "  inputSchema: {type: object}",
      "  outputSchema: {type: object}",
      "  steps:",
      "    - id: review",
      "      kind: wait",
      "      durationSeconds: 60",
      "  output: {literal: {}}",
      "",
    ].join("\n");
    const opened = h.model.opened;
    startFromTemplate(h, { title: "Approval", yaml });
    expect(h.showTemplates).toBe(false);
    expect(h.model.definition.metadata.name).toBe("approval");
    expect(h.model.source).toContain("# from a template");
    expect(h.model.canUndo).toBe(false);
    expect(h.model.opened).toBeGreaterThan(opened);
    expect(h.dirty).toBe(false);
    expect(h.message).toBe('Opened the template "Approval" as a new draft.');
    // The control that chose the template is gone: the first step gets focus.
    expect(h.focused).toEqual(["review"]);
  });
  it("reports nothing as opened when a template fails to parse", () => {
    const { host: h } = host();
    h.message = "Earlier news.";
    startFromTemplate(h, { title: "Broken", yaml: "spec: [" });
    expect(h.model.error).not.toBe("");
    expect(h.message).not.toContain("Opened the template");
    expect(h.focused).toEqual([]);
  });
});
