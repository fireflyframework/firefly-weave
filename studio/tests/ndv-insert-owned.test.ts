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
import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  COMMON_ACTIONS,
  FILE_CONNECTORS,
  fileConnector,
} from "../src/app/editor/ndv/owned/common-actions";
import {
  insertOwnedStep,
  moveFileStep,
  pruneOwned,
  reservedActionNames,
} from "../src/app/editor/ndv/owned/insert-owned";
import { newConnectorRecipe } from "../src/app/editor/ndv/owned/owned-actions";
import { markPublished } from "../src/app/editor/ndv/owned/owned-versions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { StructuredCanvasAdapter, type Step } from "../src/app/model";

const action = (id: string) => COMMON_ACTIONS.find((a) => a.id === id)!;
const fresh = () => {
  const model = new StructuredCanvasAdapter();
  model.definition.metadata.name = "order-intake";
  return model;
};
const steps = (model: StructuredCanvasAdapter) => model.definition.spec.steps;

beforeAll(async () => {
  await loadKindRegistrations();
});

describe("inserting workflow-owned steps", () => {
  it("lists the common actions in order", () => {
    expect(COMMON_ACTIONS.map((a) => a.label)).toEqual([
      "Call an API",
      "Send email",
      "Reply to an email",
      "List files",
      "Read a file",
      "Download a file",
      "Write a file",
      "Move a file",
      "Delete a file",
    ]);
    // Every file step starts on a connector the host offers that operation for.
    for (const item of COMMON_ACTIONS.slice(3)) {
      const recipe = item.recipe();
      expect(recipe.kind).toBe("connector");
      if (recipe.kind === "connector")
        expect(FILE_CONNECTORS.has(recipe.connector)).toBe(true);
    }
  });
  it("inserts Call an API with an action of its own, in one undo step", () => {
    const model = fresh();
    const id = insertOwnedStep(model, action("call-api"), "root", 0);
    expect(id).toBe("call-api");
    expect(model.selected).toBe("call-api");
    // An action step always maps its input, even when nothing is mapped yet.
    expect(steps(model)[0]).toEqual({
      id: "call-api",
      kind: "action",
      uses: "order-intake.call-api@1.0.0",
      with: { literal: {} },
    });
    expect(
      ownedActionsOf(model.canvas)["order-intake.call-api@1.0.0"],
    ).toMatchObject({ kind: "http", method: "GET" });
    const second = insertOwnedStep(model, action("call-api"), "root", 1);
    expect(second).toBe("call-api-2");
    expect(steps(model)[1]["uses"]).toBe("order-intake.call-api-2@1.0.0");
    model.undo();
    expect(steps(model).map((step) => step.id)).toEqual(["call-api"]);
    expect(Object.keys(ownedActionsOf(model.canvas))).toEqual([
      "order-intake.call-api@1.0.0",
    ]);
    model.undo();
    expect(steps(model)).toEqual([]);
    expect(ownedActionsOf(model.canvas)).toEqual({});
    model.redo();
    expect(steps(model).map((step) => step["uses"])).toEqual([
      "order-intake.call-api@1.0.0",
    ]);
  });
  it("shares one email action between steps and uses the only slot of its connector", () => {
    const model = fresh();
    model.definition.spec["connections"] = {
      mail: { connector: "weave-email@1.0.0", required: true },
    };
    insertOwnedStep(model, action("send-email"), "root", 0);
    insertOwnedStep(model, action("send-email"), "root", 1);
    expect(steps(model).map((s) => [s.id, s["uses"], s["connection"]])).toEqual(
      [
        ["send-email", "order-intake.send-email@1.0.0", "mail"],
        ["send-email-2", "order-intake.send-email@1.0.0", "mail"],
      ],
    );
    expect(Object.keys(ownedActionsOf(model.canvas))).toEqual([
      "order-intake.send-email@1.0.0",
    ]);
    // A reply is another connector action: it gets an action of its own.
    insertOwnedStep(model, action("reply-email"), "root", 2);
    expect(steps(model)[2]["uses"]).toBe("order-intake.reply-email@1.0.0");
    expect(
      ownedActionsOf(model.canvas)["order-intake.reply-email@1.0.0"],
    ).toEqual(newConnectorRecipe("weave-email@1.0.0", "reply"));
  });
  it("leaves the slot to the person when there are several, or none", () => {
    const model = fresh();
    model.definition.spec["connections"] = {
      api: { connector: "weave-http@2.0.0" },
      partner: { connector: "weave-http@2.0.0" },
    };
    insertOwnedStep(model, action("call-api"), "root", 0);
    insertOwnedStep(model, action("send-email"), "root", 1);
    expect(steps(model).map((s) => s["connection"])).toEqual([
      undefined,
      undefined,
    ]);
  });
  it("starts file steps on the workflow's file server, else SFTP", () => {
    const model = fresh();
    expect(fileConnector(model.definition)).toBe("weave-sftp@1.0.0");
    model.definition.spec["connections"] = {
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    expect(fileConnector(model.definition)).toBe("weave-google-drive@1.0.0");
    insertOwnedStep(model, action("write-file"), "root", 0);
    expect(
      ownedActionsOf(model.canvas)["order-intake.write-file@1.0.0"],
    ).toMatchObject({
      connector: "weave-google-drive@1.0.0",
      action: "write",
    });
    expect(steps(model)[0]["connection"]).toBe("docs");
    // Two file servers: the person chooses, so the step starts on SFTP.
    model.definition.spec["connections"] = {
      docs: { connector: "weave-google-drive@1.0.0" },
      share: { connector: "weave-microsoft-drive@1.0.0" },
    };
    expect(fileConnector(model.definition)).toBe("weave-sftp@1.0.0");
  });
  it("inserts into a decision's path and keeps every step ID unique", () => {
    const model = fresh();
    model.insert("switch", "root", 0);
    model.insert("action", "root", 1, {});
    model.renameStep(model.selected, "call-api");
    const id = insertOwnedStep(
      model,
      action("call-api"),
      "decision-1/case 1",
      0,
    );
    expect(id).toBe("call-api-2");
    const decision = steps(model)[0] as Step & {
      cases: { steps: Step[] }[];
    };
    expect(decision.cases[0].steps.map((s) => s.id)).toEqual(["call-api-2"]);
    expect(decision.cases[0].steps[0]["uses"]).toBe(
      "order-intake.call-api-2@1.0.0",
    );
    // Its action survives pruning: steps inside paths count.
    expect(
      Object.keys(ownedActionsOf(pruneOwned(model.canvas, model.definition))),
    ).toEqual(["order-intake.call-api-2@1.0.0"]);
  });
  it("never takes over an action another step or the workspace already calls", () => {
    const model = fresh();
    // A published action that happens to have the owned name and version.
    model.insert("action", "root", 0, {
      uses: "order-intake.call-api@1.0.0",
    });
    const id = insertOwnedStep(model, action("call-api"), "root", 1);
    expect(id).toBe("call-api");
    expect(steps(model)[1]["uses"]).toBe("order-intake.call-api-2@1.0.0");
    expect(Object.keys(ownedActionsOf(model.canvas))).toEqual([
      "order-intake.call-api-2@1.0.0",
    ]);
    // Names the workspace publishes or Studio imported are taken too.
    const reserved = reservedActionNames(
      new Map([["order-intake.send-email@1.0.0", {}]]),
      [{ name: "order-intake.send-email-2", version: "2.0.0" }],
      model.canvas,
    );
    insertOwnedStep(model, action("send-email"), "root", 2, reserved);
    expect(steps(model)[2]["uses"]).toBe("order-intake.send-email-3@1.0.0");
    // A document Studio cached for an action this workflow owns is no reservation.
    expect([
      ...reservedActionNames(
        new Map([["order-intake.call-api-2@1.0.0", {}]]),
        [],
        model.canvas,
      ),
    ]).toEqual([]);
  });
  it("keeps long workflow names within 128 characters, suffix included", () => {
    const model = fresh();
    model.definition.metadata.name = "w".repeat(125);
    insertOwnedStep(model, action("call-api"), "root", 0);
    insertOwnedStep(model, action("call-api"), "root", 1);
    const names = Object.keys(ownedActionsOf(model.canvas)).map((uses) =>
      uses.slice(0, uses.lastIndexOf("@")),
    );
    expect(names).toHaveLength(2);
    expect(new Set(names).size).toBe(2);
    for (const name of names) expect(name.length).toBeLessThanOrEqual(128);
  });
  it("changes nothing when the insert is refused", () => {
    const model = fresh();
    insertOwnedStep(model, action("call-api"), "root", 0);
    const before = JSON.stringify([model.definition, model.canvas]);
    const selected = model.selected;
    expect(() =>
      insertOwnedStep(model, action("send-email"), "gone/path", 0),
    ).toThrow("Target branch no longer exists.");
    model.readonly = true;
    expect(() =>
      insertOwnedStep(model, action("send-email"), "root", 1),
    ).toThrow();
    model.readonly = false;
    expect(JSON.stringify([model.definition, model.canvas])).toBe(before);
    expect(model.selected).toBe(selected);
    // The refused inserts left no undo step behind.
    model.undo();
    expect(steps(model)).toEqual([]);
  });
  it("drops owned actions no step uses, and keeps those of steps inside paths or not placed", () => {
    const model = fresh();
    insertOwnedStep(model, action("call-api"), "root", 0);
    model.remove("call-api");
    expect(ownedActionsOf(pruneOwned(model.canvas, model.definition))).toEqual(
      {},
    );
    const unplaced: Step = {
      id: "later",
      kind: "action",
      uses: "order-intake.call-api@1.0.0",
    };
    expect(
      Object.keys(
        ownedActionsOf(pruneOwned(model.canvas, model.definition, [unplaced])),
      ),
    ).toEqual(["order-intake.call-api@1.0.0"]);
  });
  it("reuses an action whose step was deleted only after it is pruned", () => {
    const model = fresh();
    insertOwnedStep(model, action("call-api"), "root", 0);
    model.remove("call-api");
    // Undo can still bring the step back with its action...
    model.undo();
    expect(ownedActionsOf(model.canvas)).toHaveProperty([
      "order-intake.call-api@1.0.0",
    ]);
    model.redo();
    // ...and a new Call an API gets a fresh action under the freed name.
    insertOwnedStep(model, action("call-api"), "root", 0);
    expect(steps(model)[0]["uses"]).toBe("order-intake.call-api@1.0.0");
    model.undo();
    model.undo();
    expect(steps(model).map((step) => step.id)).toEqual(["call-api"]);
  });
});

describe("moving a file step to another file server", () => {
  const twoServers = () => {
    const model = fresh();
    model.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    return model;
  };
  it("moves the action with a step that is its only user, keeping its settings", () => {
    const model = twoServers();
    insertOwnedStep(model, action("write-file"), "root", 0);
    const uses = "order-intake.write-file@1.0.0";
    model.canvas = withOwnedAction(model.canvas, uses, {
      ...newConnectorRecipe("weave-sftp@1.0.0", "write"),
      timeoutSeconds: 90,
      description: "Keeps invoices",
    });
    model.update(
      "write-file",
      JSON.stringify({
        ...steps(model)[0],
        connection: "files",
        with: { literal: { destination: "/in" } },
      }),
    );
    model.batch(() => moveFileStep(model, "write-file", "docs"));
    expect(steps(model)[0]).toMatchObject({
      uses,
      connection: "docs",
      with: { literal: { destination: "/in" } },
    });
    expect(ownedActionsOf(model.canvas)[uses]).toEqual({
      ...newConnectorRecipe("weave-google-drive@1.0.0", "write"),
      timeoutSeconds: 90,
      description: "Keeps invoices",
    });
    model.undo();
    expect(ownedActionsOf(model.canvas)[uses]).toMatchObject({
      connector: "weave-sftp@1.0.0",
    });
    expect(steps(model)[0]["connection"]).toBe("files");
  });
  it("gives a step that shares its action an action of its own, leaving the other step as it was", () => {
    const model = twoServers();
    model.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
    };
    insertOwnedStep(model, action("write-file"), "root", 0);
    insertOwnedStep(model, action("write-file"), "root", 1);
    const uses = "order-intake.write-file@1.0.0";
    expect(steps(model).map((s) => s["uses"])).toEqual([uses, uses]);
    model.canvas = withOwnedAction(model.canvas, uses, {
      ...newConnectorRecipe("weave-sftp@1.0.0", "write"),
      timeoutSeconds: 90,
    });
    model.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    model.batch(() => moveFileStep(model, "write-file-2", "docs"));
    expect(steps(model).map((s) => [s["uses"], s["connection"]])).toEqual([
      [uses, "files"],
      ["order-intake.write-file-2@1.0.0", "docs"],
    ]);
    expect(ownedActionsOf(model.canvas)).toEqual({
      [uses]: {
        ...newConnectorRecipe("weave-sftp@1.0.0", "write"),
        timeoutSeconds: 90,
      },
      "order-intake.write-file-2@1.0.0": {
        ...newConnectorRecipe("weave-google-drive@1.0.0", "write"),
        timeoutSeconds: 90,
      },
    });
  });
  it("joins the workflow's action for that file server when it has one", () => {
    const model = twoServers();
    insertOwnedStep(model, action("write-file"), "root", 0);
    // Only the Drive slot: the next write starts there with an action of its own.
    model.definition.spec["connections"] = {
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    insertOwnedStep(model, action("write-file"), "root", 1);
    expect(steps(model)[1]["uses"]).toBe("order-intake.write-file-2@1.0.0");
    model.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    model.batch(() => moveFileStep(model, "write-file", "docs"));
    expect(steps(model).map((s) => [s["uses"], s["connection"]])).toEqual([
      ["order-intake.write-file-2@1.0.0", "docs"],
      ["order-intake.write-file-2@1.0.0", "docs"],
    ]);
  });
  it("refuses actions it doesn't own, slots that aren't file servers and missing slots", () => {
    const model = twoServers();
    model.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
      mail: { connector: "weave-email@1.0.0" },
    };
    model.insert("action", "root", 0, {
      id: "published",
      uses: "acme.write@1.0.0",
      connection: "files",
    });
    insertOwnedStep(model, action("write-file"), "root", 1);
    const before = JSON.stringify([model.definition, model.canvas]);
    expect(() => moveFileStep(model, "published", "docs")).toThrow(
      "Only a file step's own action can move to another file server.",
    );
    expect(() => moveFileStep(model, "write-file", "mail")).toThrow(
      "Choose a slot on a file server.",
    );
    expect(() => moveFileStep(model, "write-file", "gone")).toThrow(
      "Choose a slot on a file server.",
    );
    expect(JSON.stringify([model.definition, model.canvas])).toBe(before);
  });
  it("moves a file step off a published action to its next version, and a copy for one step starts unpublished", () => {
    const model = twoServers();
    insertOwnedStep(model, action("write-file"), "root", 0);
    const uses = "order-intake.write-file@1.0.0";
    model.update(
      "write-file",
      JSON.stringify({ ...steps(model)[0], connection: "files" }),
    );
    model.canvas = markPublished(model, uses);
    const moved = model.batch(() => moveFileStep(model, "write-file", "docs"));
    expect(moved).toBe("order-intake.write-file@1.0.1");
    expect(steps(model)[0]).toMatchObject({ uses: moved, connection: "docs" });
    expect(ownedActionsOf(model.canvas)[moved]).toMatchObject({
      connector: "weave-google-drive@1.0.0",
      version: "1.0.1",
      published: { version: "1.0.0" },
    });
    const shared = twoServers();
    shared.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
    };
    insertOwnedStep(shared, action("write-file"), "root", 0);
    insertOwnedStep(shared, action("write-file"), "root", 1);
    shared.canvas = markPublished(shared, uses);
    shared.definition.spec["connections"] = {
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    const copy = shared.batch(() =>
      moveFileStep(shared, "write-file-2", "docs"),
    );
    expect(copy).not.toBe(uses);
    expect(ownedActionsOf(shared.canvas)[copy]).not.toHaveProperty("published");
    expect(ownedActionsOf(shared.canvas)[copy]).not.toHaveProperty("version");
    expect(steps(shared)[0]["uses"]).toBe(uses);
  });
});

/** Step details for one step of a workflow, as the session tests open them. */
function session(model: StructuredCanvasAdapter, target: string) {
  let refuse = false;
  const announcements: string[] = [];
  const host = {
    model,
    editingLocked: false,
    profile: null,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    diagnostics: null,
    diagnosticsDefinition: null,
    can: () => true,
    notify: () => undefined,
    refreshView: () => undefined,
    perform: (fn: () => void) => fn(),
    api: { request: async () => ({ features: [] }) },
  } as unknown as {
    -readonly [K in keyof StepDetailsHost]: StepDetailsHost[K];
  };
  const controller = new StepDetailsController(host);
  const form = new FormSession(
    host,
    controller,
    openRequest(target),
    {
      confirm: async () => true,
      announce: (text) => announcements.push(text),
    },
    () => !refuse,
  );
  const spec = controller
    .parameters(target)
    .fields.find((field) => field.id === "connection")!;
  return {
    host,
    form,
    spec,
    announcements,
    close: () => {
      refuse = true;
    },
  };
}

describe("the Connection field of a file step", () => {
  const workflow = () => {
    const model = fresh();
    model.setSource(
      readFileSync(
        new URL("./fixtures/step-details.yaml", import.meta.url),
        "utf8",
      ),
    );
    model.definition.metadata.name = "order-intake";
    const document = structuredClone(model.definition);
    document.spec["connections"] = {
      ...(document.spec["connections"] as object),
      files: { connector: "weave-sftp@1.0.0" },
      docs: { connector: "weave-google-drive@1.0.0" },
    };
    model.updateWorkflow(document);
    model.clearHistory();
    return model;
  };
  it("offers every file server's slots for the step's own file action, and only then", () => {
    const model = workflow();
    insertOwnedStep(model, action("read-file"), "root", 0);
    const { form, spec } = session(model, "read-file");
    expect(form.fileServers(spec)).toBe(FILE_CONNECTORS);
    model.insert("action", "root", 1, {
      id: "published",
      uses: "acme.read@1.0.0",
      connection: "files",
    });
    const other = session(model, "published");
    expect(other.form.fileServers(other.spec)).toBeNull();
    insertOwnedStep(model, action("send-email"), "root", 2);
    const mail = session(model, "send-email");
    expect(mail.form.fileServers(mail.spec)).toBeNull();
  });
  it("moves the step and its action in one undo step, and announces it", () => {
    const model = workflow();
    insertOwnedStep(model, action("read-file"), "root", 0);
    model.clearHistory();
    const { form, spec, announcements } = session(model, "read-file");
    const before = model.source;
    expect(form.useFileServer(spec, "docs")).toBe(true);
    expect(steps(model)[0]["connection"]).toBe("docs");
    expect(
      ownedActionsOf(model.canvas)["order-intake.read-file@1.0.0"],
    ).toMatchObject({ connector: "weave-google-drive@1.0.0" });
    expect(announcements).toEqual([
      "Now uses the slot docs. The fields follow its file server.",
    ]);
    model.undo();
    expect(model.source).toBe(before);
    expect(model.canUndo).toBe(false);
  });
  it("writes nothing once step details closed, or for a slot that isn't a file server", () => {
    const model = workflow();
    insertOwnedStep(model, action("read-file"), "root", 0);
    const before = JSON.stringify([model.definition, model.canvas]);
    const f = session(model, "read-file");
    expect(f.form.useFileServer(f.spec, "ledger-db")).toBe(false);
    f.close();
    expect(f.form.useFileServer(f.spec, "docs")).toBe(false);
    expect(JSON.stringify([model.definition, model.canvas])).toBe(before);
  });
});

describe("a file step inserted from an app's own view", () => {
  it("calls an action on that file server", () => {
    const model = new StructuredCanvasAdapter();
    const write = COMMON_ACTIONS.find((action) => action.id === "write-file")!;
    const id = insertOwnedStep(
      model,
      write,
      "root",
      0,
      new Set(),
      "weave-ftp@1.0.0",
    );
    const uses = String(
      model.definition.spec.steps.find((step) => step.id === id)!["uses"],
    );
    expect(ownedActionsOf(model.canvas)[uses]).toMatchObject({
      kind: "connector",
      connector: "weave-ftp@1.0.0",
      action: "write",
    });
    // A connector that isn't a file server changes nothing.
    const other = insertOwnedStep(
      model,
      write,
      "root",
      0,
      new Set(),
      "weave-http@2.0.0",
    );
    const otherUses = String(
      model.definition.spec.steps.find((step) => step.id === other)!["uses"],
    );
    expect(ownedActionsOf(model.canvas)[otherUses]).toMatchObject({
      connector: "weave-sftp@1.0.0",
    });
  });
});
