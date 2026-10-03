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
  compatibleSlots,
  declareSlot,
  planSlot,
  slotName,
  type WorkflowSlot,
} from "../src/app/integrations/slot-binding";
import { StructuredCanvasAdapter } from "../src/app/model";

const sql = { connector: "weave-postgresql@1.0.0" };
const http = { connector: "weave-http@2.0.0" };
const slot = (
  name: string,
  connector: string,
  required = true,
): WorkflowSlot => ({ name, connector, required });

describe("slot names", () => {
  it("cleans a base name and avoids names in use", () => {
    expect(slotName("weave-postgresql", [])).toBe("weave-postgresql");
    expect(slotName("Pet Store!", [])).toBe("Pet-Store-");
    expect(slotName("--crm", [])).toBe("crm");
    expect(slotName("", [])).toBe("connection");
    expect(slotName("crm", ["crm", "crm-2"])).toBe("crm-3");
  });
});

describe("binding an inserted action to a connection slot", () => {
  it("needs no slot when the action declares no connection", () => {
    expect(planSlot(null, [slot("crm", "crm@1.0.0")])).toEqual({});
  });
  it("binds the only compatible slot", () => {
    const slots = [slot("db", sql.connector), slot("crm", "crm@1.0.0")];
    expect(planSlot(sql, slots)).toEqual({ connection: "db" });
  });
  it("declares a slot named after the connector when none fits", () => {
    expect(planSlot(sql, [])).toEqual({
      connection: "weave-postgresql",
      add: {
        name: "weave-postgresql",
        connector: sql.connector,
        required: true,
      },
    });
  });
  it("never reuses a slot name that belongs to another connector", () => {
    const plan = planSlot(sql, [slot("weave-postgresql", "crm@1.0.0")]);
    expect(plan.connection).toBe("weave-postgresql-2");
    expect(plan.add?.name).toBe("weave-postgresql-2");
  });
  it("leaves the choice to the person when several slots fit", () => {
    const slots = [
      slot("primary", sql.connector),
      slot("replica", sql.connector),
    ];
    expect(planSlot(sql, slots)).toEqual({});
  });
  it("matches required and optional connections like the compiler", () => {
    // An optional connection may use a required or an optional slot.
    expect(
      planSlot({ ...sql, required: false }, [slot("db", sql.connector)]),
    ).toEqual({ connection: "db" });
    // A required connection never binds an optional slot.
    expect(compatibleSlots(sql, [slot("db", sql.connector, false)])).toEqual(
      [],
    );
    expect(planSlot(sql, [slot("db", sql.connector, false)]).add).toEqual({
      name: "weave-postgresql",
      connector: sql.connector,
      required: true,
    });
    expect(planSlot({ ...sql, required: false }, []).add?.required).toBe(false);
  });
  it("uses the API action builder's slot name, reusing it when it fits", () => {
    expect(
      planSlot(http, [slot("pet-store", http.connector)], "pet-store"),
    ).toEqual({ connection: "pet-store" });
    expect(
      planSlot(http, [slot("other", http.connector)], "pet-store"),
    ).toEqual({
      connection: "pet-store",
      add: { name: "pet-store", connector: http.connector, required: true },
    });
    expect(
      planSlot(http, [slot("pet-store", "crm@1.0.0")], "pet-store").connection,
    ).toBe("pet-store-2");
  });
  it("declares a slot next to the existing ones", () => {
    expect(
      declareSlot(
        { db: { connector: sql.connector } },
        { name: "api", connector: http.connector, required: true },
      ),
    ).toEqual({
      db: { connector: sql.connector },
      api: { connector: http.connector, required: true },
    });
  });
});

describe("one undo step for an insert with its slot", () => {
  const prepared = () => {
    const model = new StructuredCanvasAdapter();
    model.setSource(
      [
        "apiVersion: weave/v1alpha1",
        "kind: Workflow",
        "metadata:",
        "  name: pets # the pet workflow",
        "  version: 1.0.0",
        "spec:",
        "  inputSchema:",
        "    type: object",
        "  outputSchema:",
        "    type: object",
        "  steps: []",
        "  output:",
        "    literal: {}",
        "",
      ].join("\n"),
      "yaml",
    );
    model.clearHistory();
    return model;
  };
  it("undoes the step and the declared slot together", () => {
    const model = prepared();
    model.batch(() => {
      const document = structuredClone(model.definition);
      document.spec["connections"] = declareSlot(undefined, {
        name: "pet-store",
        connector: http.connector,
        required: true,
      });
      model.updateWorkflow(document);
      model.insert("action", "root", 0, {
        uses: "get-pet@1.0.0",
        connection: "pet-store",
      });
    });
    expect(model.source).toContain("connection: pet-store");
    expect(model.source).toContain("connector: weave-http@2.0.0");
    // YAML comments survive the batched edit.
    expect(model.source).toContain("# the pet workflow");
    model.undo();
    expect(model.definition.spec.steps).toEqual([]);
    expect(model.definition.spec["connections"]).toBeUndefined();
    expect(model.canUndo).toBe(false);
    model.redo();
    expect(model.definition.spec.steps).toHaveLength(1);
    expect(model.definition.spec["connections"]).toEqual({
      "pet-store": { connector: http.connector, required: true },
    });
  });
  it("leaves the workflow and undo history untouched when a part fails", () => {
    const model = prepared();
    const before = model.source;
    expect(() =>
      model.batch(() => {
        const document = structuredClone(model.definition);
        document.spec["connections"] = { api: { connector: http.connector } };
        model.updateWorkflow(document);
        model.insert("action", "missing-owner", 0);
      }),
    ).toThrow();
    expect(model.source).toBe(before);
    expect(model.definition.spec["connections"]).toBeUndefined();
    expect(model.canUndo).toBe(false);
  });
});
