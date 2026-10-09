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
import { describe, expect, it, beforeAll } from "vitest";
import { parse } from "yaml";
import {
  StructuredCanvasAdapter,
  type Workflow,
  type Step,
} from "../src/app/model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import {
  BAD_NAME,
  BAD_URL,
  firstMode,
  resourceModes,
  resourceProblem,
} from "../src/app/editor/ndv/params/resource";
import {
  allSteps,
  newSlotName,
  renamedConnections,
  slotBase,
  slotsOf,
} from "../src/app/editor/ndv/params/slots";

const fixture = parse(
  readFileSync(
    new URL("./fixtures/step-details.yaml", import.meta.url),
    "utf8",
  ),
) as Workflow;
beforeAll(() => loadKindRegistrations());

describe("connection slots", () => {
  it("names a new slot after its connector without replacing existing slots", () => {
    expect(slotsOf(fixture)).toEqual([
      {
        name: "ledger-db",
        connector: "weave-postgresql@1.0.0",
        required: true,
      },
    ]);
    expect(slotBase("weave-http@2.0.0")).toBe("http");
    expect(slotBase("acme-crm@1.0.0")).toBe("acme-crm");
    const document = structuredClone(fixture);
    document.spec["connections"] = { http: {}, "http-2": {} };
    expect(newSlotName(document, "weave-http@2.0.0")).toBe("http-3");
    expect(newSlotName(fixture, "weave-email@1.0.0")).toBe("email");
    expect(
      Object.keys(
        renamedConnections({ a: {}, b: { connector: "x" }, c: {} }, "b", "z"),
      ),
    ).toEqual(["a", "z", "c"]);
  });
  it("walks author steps in source order while ignoring colliding literal steps", () => {
    const document = structuredClone(fixture);
    document.spec["sample"] = {
      steps: [{ id: "lookup", kind: "action", connection: "ledger-db" }],
    };
    document.spec.steps[1]["with"] = {
      literal: { steps: [{ id: "notify-sales", kind: "action" }] },
    };
    expect(allSteps(document).map((step) => step.id)).toEqual([
      "check-customer",
      "lookup",
      "route-by-value",
      "notify-sales",
      "fan-out",
      "wait-a-minute",
      "summarize",
      "score",
      "wait-for-payment",
      "reject-order",
    ]);
  });
  it("visits a shared author step once and stops cyclic author and literal anchors", () => {
    const child: Step = { id: "child", kind: "action" };
    const loop = {
      id: "loop",
      kind: "forEach",
      body: { steps: [] },
    } as unknown as Step;
    loop["body"] = { steps: [child, loop] };
    const document = structuredClone(fixture);
    document.spec.steps = [loop, child];
    const literal: Record<string, unknown> = {};
    literal["steps"] = [literal];
    document.spec["sample"] = literal;
    expect(allSteps(document).map((step) => step.id)).toEqual([
      "loop",
      "child",
    ]);
  });
});

describe("resource values", () => {
  const action: ParamSpec = {
    id: "action",
    path: ["uses"],
    type: "resource",
    label: "Action",
    choices: async () => [],
  };
  it("chooses modes without losing an unknown imported value", () => {
    expect(resourceModes(action)).toEqual(["list", "name"]);
    expect(resourceModes({ ...action, id: "itemId" })).toEqual(["id", "url"]);
    expect(resourceModes({ ...action, choices: undefined })).toEqual(["name"]);
    expect(firstMode(["list", "name"], "", false)).toBe("list");
    expect(firstMode(["list", "name"], "orders.get@1.0.0", false)).toBe("name");
    expect(firstMode(["list", "name"], "orders.get@1.0.0", true)).toBe("list");
    expect(
      firstMode(["id", "url"], "https://drive.example/item/1", false),
    ).toBe("url");
  });
  it.each([
    "orders.get@1.0.0",
    "orders.get@0.0.0-alpha.1+build.001",
    "x@1.2.3-rc-1",
    "1_A-b.c@1.2.3+build",
  ])("accepts the published version %s", (value) =>
    expect(resourceProblem("name", value)).toBe(""),
  );
  it.each([
    "orders.get",
    "x@01.0.0",
    "x@1.02.0",
    "x@1.0.03",
    "x@1.0.0-01",
    "x@1.0.0@2",
    "_x@1.0.0",
  ])("retains invalid published version %s outside the document", (value) =>
    expect(resourceProblem("name", value)).toBe(BAD_NAME),
  );
  it("requires an absolute secure resource URL while leaving IDs literal", () => {
    expect(resourceProblem("url", "https://drive.example/item/1")).toBe("");
    for (const value of [
      "drive.example/x",
      "http://drive.example/x",
      "https://",
      "https:// bad",
    ])
      expect(resourceProblem("url", value)).toBe(BAD_URL);
    expect(resourceProblem("id", "01ABC")).toBe("");
  });
});

function setup() {
  const model = new StructuredCanvasAdapter();
  model.setSource(
    readFileSync(
      new URL("./fixtures/step-details.yaml", import.meta.url),
      "utf8",
    ),
  );
  model.clearHistory();
  let current = true,
    allowed = true,
    accepted = true;
  let answer!: (value: string | null) => void;
  const announcements: string[] = [],
    opened: (string | undefined)[] = [];
  const host = {
    model,
    editingLocked: false,
    profile: null,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    diagnostics: null,
    diagnosticsDefinition: null,
    can: () => allowed,
    notify: () => undefined,
    refreshView: () => undefined,
    perform: (fn: () => void) => {
      if (accepted) fn();
    },
    openConnectionDialog: (slot?: string) => opened.push(slot),
    api: { request: async () => ({ features: [] }) },
  } as unknown as {
    -readonly [K in keyof StepDetailsHost]: StepDetailsHost[K];
  };
  const controller = new StepDetailsController(host);
  const session = new FormSession(
    host,
    controller,
    openRequest("lookup"),
    {
      confirm: async () => true,
      announce: (text) => announcements.push(text),
      prompt: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    },
    () => current,
  );
  const spec = controller
    .parameters("lookup")
    .fields.find((field) => field.id === "connection")!;
  return {
    model,
    host,
    controller,
    session,
    spec,
    announcements,
    opened,
    answer: (value: string | null) => answer(value),
    close: () => {
      current = false;
    },
    deny: () => {
      allowed = false;
    },
    refuse: () => {
      accepted = false;
    },
  };
}

describe("slot transactions", () => {
  it("declares and uses a slot in one Undo without requiring a management grant", () => {
    const f = setup();
    f.deny();
    const before = f.model.source;
    f.session.newSlot(f.spec, "weave-postgresql@1.0.0");
    expect(f.session.read(f.spec)).toEqual({
      mode: "fixed",
      value: "postgresql",
    });
    expect(slotsOf(f.model.definition).map((slot) => slot.name)).toEqual([
      "ledger-db",
      "postgresql",
    ]);
    expect(f.announcements).toEqual(["Declared the slot postgresql."]);
    f.model.undo();
    expect(f.model.source).toBe(before);
  });
  it("keeps consecutive new slots in separate Undo transactions", () => {
    const f = setup();
    f.session.newSlot(f.spec, "weave-postgresql@1.0.0");
    f.session.newSlot(f.spec, "weave-postgresql@1.0.0");
    expect(f.session.read(f.spec)).toEqual({
      mode: "fixed",
      value: "postgresql-2",
    });
    f.model.undo();
    expect(f.session.read(f.spec)).toEqual({
      mode: "fixed",
      value: "postgresql",
    });
    expect(slotsOf(f.model.definition).map((slot) => slot.name)).toEqual([
      "ledger-db",
      "postgresql",
    ]);
  });
  it("renames nested author references but leaves literal collisions untouched in one Undo", async () => {
    const f = setup();
    const nested = allSteps(f.model.definition).find(
      (step) => step.id === "notify-sales",
    )!;
    f.model.update(
      nested.id,
      JSON.stringify({
        ...nested,
        connection: "ledger-db",
        with: {
          literal: { steps: [{ id: "lookup", connection: "ledger-db" }] },
        },
      }),
    );
    const before = f.model.source;
    const pending = f.session.renameSlot(f.spec);
    f.answer("Orders DB");
    await pending;
    expect(slotsOf(f.model.definition).map((slot) => slot.name)).toEqual([
      "orders-db",
    ]);
    expect(
      allSteps(f.model.definition)
        .filter((step) => step["connection"] === "orders-db")
        .map((step) => step.id),
    ).toEqual(["lookup", "notify-sales"]);
    expect(
      allSteps(f.model.definition).find((step) => step.id === "notify-sales")![
        "with"
      ],
    ).toEqual({
      literal: { steps: [{ id: "lookup", connection: "ledger-db" }] },
    });
    expect(f.announcements).toEqual(["Renamed the slot to orders-db."]);
    f.model.undo();
    expect(f.model.source).toBe(before);
  });
  for (const change of [
    "close",
    "reopen",
    "account",
    "model",
    "read only",
    "descriptor",
    "slot",
    "connector",
    "destination",
    "refused",
  ] as const)
    it(`does not finish a slot rename after ${change}`, async () => {
      const f = setup();
      const pending = f.session.renameSlot(f.spec);
      if (change === "close") f.close();
      if (change === "reopen") f.model.clearHistory();
      if (change === "account") f.host.profile = { id: "other" } as never;
      if (change === "model") f.host.model = new StructuredCanvasAdapter();
      if (change === "read only") f.host.editingLocked = true;
      if (change === "descriptor")
        f.controller.parameters = () => ({
          fields: [{ ...f.spec, readOnly: () => "Locked" }],
        });
      if (change === "slot")
        (f.model.definition.spec["connections"] as Record<string, unknown>)[
          "ledger-db"
        ] = { connector: "weave-http@1.0.0" };
      if (change === "connector")
        (f.host.catalogContracts as Map<string, Record<string, unknown>>).set(
          "sql.lookup@1.0.0",
          {
            spec: { connection: { connector: "weave-http@1.0.0" } },
          } as never,
        );
      if (change === "destination")
        (f.model.definition.spec["connections"] as Record<string, unknown>)[
          "orders-db"
        ] = { connector: "other@1.0.0" };
      if (change === "refused") f.refuse();
      const before = JSON.stringify(f.host.model.definition);
      f.answer("orders-db");
      await pending;
      expect(JSON.stringify(f.host.model.definition)).toBe(before);
      expect(f.announcements).toEqual([]);
    });
  it("checks current edit authority and connector at the new-slot boundary", () => {
    const f = setup();
    const before = f.model.source;
    f.session.newSlot(f.spec, "weave-http@1.0.0");
    f.host.editingLocked = true;
    f.session.newSlot(f.spec, "weave-postgresql@1.0.0");
    expect(f.model.source).toBe(before);
    expect(f.announcements).toEqual([]);
  });
  it("only opens connection setup with live edit and management authority", () => {
    const f = setup();
    f.session.createConnection(f.spec);
    expect(f.opened).toEqual([]);
    f.host.profile = { id: "current" } as never;
    const active = new FormSession(
      f.host,
      f.controller,
      openRequest("lookup"),
      { confirm: async () => true, announce: () => undefined },
    );
    f.deny();
    active.createConnection(f.spec);
    expect(f.opened).toEqual([]);
  });
  it("opens setup for the selected slot when authorized, without changing the workflow", () => {
    const f = setup();
    f.host.profile = { id: "current" } as never;
    const active = new FormSession(
      f.host,
      f.controller,
      openRequest("lookup"),
      { confirm: async () => true, announce: () => undefined },
    );
    const before = f.model.source;
    active.createConnection(f.spec);
    expect(f.opened).toEqual(["ledger-db"]);
    expect(f.model.source).toBe(before);
    f.host.editingLocked = true;
    active.createConnection(f.spec);
    expect(f.opened).toHaveLength(1);
  });
});

describe("resource choice ownership", () => {
  it("keeps catalog providers stable across renders and reads the passed context", async () => {
    const f = setup();
    for (const target of ["lookup", "score"]) {
      const first = f.controller.parameters(target).fields[0].choices;
      const next = f.controller.parameters(target).fields[0].choices;
      expect(first).toBe(next);
      if (typeof first !== "function") throw Error("Resource provider missing");
      const context = f.controller.ndvContext(target);
      const changed = {
        ...context,
        catalog: {
          ...context.catalog,
          actions: async () => [
            { uses: "replacement@2.0.0", title: "Replacement" },
          ],
          tables: async () => [
            { uses: "replacement@2.0.0", title: "Replacement" },
          ],
        },
      };
      expect(await first(changed)).toEqual([
        { value: "replacement@2.0.0", label: "Replacement" },
      ]);
    }
  });
});
