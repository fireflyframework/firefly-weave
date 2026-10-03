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
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
// The component's module imports dialog.ts, which registers a document focus
// listener when it loads; Node has no document, and the model needs none.
vi.hoisted(() => {
  const scope = globalThis as { document?: unknown };
  scope.document ??= { addEventListener() {} };
});
import {
  addRow,
  applyInferred,
  changeType,
  decimalPaths,
  designerIssues,
  inferenceNotes,
  inferViaHttpAction,
  modelToSchema,
  moveRow,
  newRow,
  removeRow,
  schemaToModel,
  widenDecimals,
  type DesignerModel,
  type DesignerRow,
} from "../src/app/forms/ui/schema-designer";
import { getAt, type Json } from "../src/app/forms/core/json";

const root = resolve(import.meta.dirname, "../..");
const fixture = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/shipped-schemas.json"),
    "utf8",
  ),
) as { schemas: { id: string; schema: Record<string, unknown> }[] };
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
const hasPython = existsSync(python);

const row = (model: DesignerModel, name: string): DesignerRow => {
  const found = model.rows.find((r) => r.name === name);
  if (!found) throw Error(`no row ${name}`);
  return found;
};

describe("reading a schema into rows", () => {
  it("lists fields with their type, required flag, labels and constraints", () => {
    const model = schemaToModel({
      type: "object",
      additionalProperties: false,
      required: ["customerId", "tier"],
      properties: {
        customerId: {
          type: "string",
          minLength: 1,
          maxLength: 64,
          title: "Customer",
        },
        tier: {
          type: "string",
          enum: ["gold", "standard"],
          description: "Plan",
        },
        amount: { type: ["number", "null"], minimum: 0 },
        when: { type: "string", format: "date-time" },
        tags: { type: "array", items: { type: "string" }, maxItems: 5 },
        address: {
          type: "object",
          properties: { city: { type: "string" } },
          required: ["city"],
        },
      },
    });
    expect(model.editable).toBe(true);
    expect(model.closed).toBe(true);
    expect(model.rows.map((r) => [r.name, r.type, r.required])).toEqual([
      ["customerId", "string", true],
      ["tier", "string", true],
      ["amount", "number", false],
      ["when", "string", false],
      ["tags", "array", false],
      ["address", "object", false],
    ]);
    expect(row(model, "customerId")).toMatchObject({
      title: "Customer",
      min: "1",
      max: "64",
    });
    expect(row(model, "tier").choices).toBe("gold\nstandard");
    expect(row(model, "tier").description).toBe("Plan");
    expect(row(model, "amount")).toMatchObject({ nullable: true, min: "0" });
    expect(row(model, "when").format).toBe("date-time");
    expect(row(model, "tags").item?.type).toBe("string");
    expect(row(model, "tags").max).toBe("5");
    expect(
      row(model, "address").children.map((c) => [c.name, c.required]),
    ).toEqual([["city", true]]);
  });

  it("keeps keywords it cannot edit and flags them, locking advanced ones", () => {
    const model = schemaToModel({
      type: "object",
      properties: {
        code: { type: "string", pattern: "^[A-Z]+$", default: "AB" },
        choice: { oneOf: [{ type: "string" }, { type: "integer" }] },
        linked: { $ref: "#/$defs/Thing", title: "Linked" },
      },
      $defs: { Thing: { type: "string" } },
    });
    expect(row(model, "code").kept).toEqual(["pattern", "default"]);
    expect(row(model, "code").locked).toBeNull();
    expect(row(model, "choice").type).toBe("advanced");
    expect(row(model, "choice").locked).toBe("oneOf");
    expect(row(model, "linked").locked).toBe("$ref");
    expect(row(model, "linked").title).toBe("Linked");
    expect(model.kept).toEqual(["$defs"]);
  });

  it("refuses roots that are not a list of fields, and keeps them unchanged", () => {
    for (const schema of [
      { type: "string" },
      { $ref: "#/$defs/X", $defs: { X: {} } },
      { oneOf: [{ type: "object" }] },
    ]) {
      const model = schemaToModel(schema);
      expect(model.editable, JSON.stringify(schema)).toBe(false);
      expect(model.reason).toMatch(/Source/);
      expect(modelToSchema(model)).toBe(schema);
    }
    expect(schemaToModel({}).editable).toBe(true);
    expect(schemaToModel(undefined).editable).toBe(true);
    expect(modelToSchema(schemaToModel({}))).toEqual({});
  });
});

describe("round trip", () => {
  it("returns every shipped schema unchanged, key order included, when nothing is edited", () => {
    for (const item of fixture.schemas) {
      const model = schemaToModel(item.schema);
      expect(JSON.stringify(modelToSchema(model)), item.id).toBe(
        JSON.stringify(item.schema),
      );
      expect(designerIssues(model), item.id).toEqual([]);
    }
  });

  it("keeps the original required order and only touches what changed", () => {
    const source = {
      type: "object",
      required: ["b", "a"],
      properties: { a: { type: "string" }, b: { type: "integer", minimum: 1 } },
    };
    const model = schemaToModel(source);
    row(model, "a").title = "First";
    const next = modelToSchema(model) as Record<string, unknown>;
    expect(next["required"]).toEqual(["b", "a"]);
    expect(next["properties"]).toEqual({
      a: { type: "string", title: "First" },
      b: { type: "integer", minimum: 1 },
    });
    expect((next["properties"] as Record<string, unknown>)["b"]).toBe(
      source.properties.b,
    );
  });
});

describe("editing", () => {
  it("adds, renames, requires, reorders and removes fields", () => {
    const model = schemaToModel({});
    const first = addRow(model, null);
    first.name = "customerId";
    first.required = true;
    const second = addRow(model, null);
    second.name = "note";
    changeType(second, "string");
    second.max = "500";
    expect(modelToSchema(model)).toEqual({
      type: "object",
      properties: {
        customerId: { type: "string" },
        note: { type: "string", maxLength: 500 },
      },
      required: ["customerId"],
    });
    moveRow(model, second, -1);
    first.name = "id";
    const moved = modelToSchema(model) as Record<string, unknown>;
    expect(Object.keys(moved["properties"] as object)).toEqual(["note", "id"]);
    expect(moved["required"]).toEqual(["id"]);
    removeRow(model, first);
    expect(modelToSchema(model)).toEqual({
      type: "object",
      properties: { note: { type: "string", maxLength: 500 } },
    });
  });

  it("drops rules that no longer apply when the type changes", () => {
    const model = schemaToModel({
      type: "object",
      properties: {
        code: {
          type: "string",
          minLength: 2,
          pattern: "^[a-z]+$",
          format: "email",
          enum: ["ab", "cd"],
          title: "Code",
        },
      },
    });
    const code = row(model, "code");
    changeType(code, "integer");
    code.choices = "1\n2";
    code.min = "1";
    expect(modelToSchema(model)).toEqual({
      type: "object",
      properties: {
        code: { type: "integer", enum: [1, 2], title: "Code", minimum: 1 },
      },
    });
    expect(code.kept).toEqual([]);
  });

  it("writes nullable, closed groups, lists of groups and nested fields", () => {
    const model = schemaToModel({ type: "object" });
    const items = addRow(model, null);
    items.name = "items";
    changeType(items, "array");
    changeType(items.item!, "object");
    const sku = addRow(model, items.item!);
    sku.name = "sku";
    sku.required = true;
    const qty = addRow(model, items.item!);
    qty.name = "qty";
    changeType(qty, "integer");
    qty.min = "1";
    qty.nullable = true;
    items.item!.closed = true;
    items.min = "1";
    model.closed = true;
    expect(modelToSchema(model)).toEqual({
      type: "object",
      properties: {
        items: {
          type: "array",
          items: {
            type: "object",
            properties: {
              sku: { type: "string" },
              qty: { type: ["integer", "null"], minimum: 1 },
            },
            required: ["sku"],
            additionalProperties: false,
          },
          minItems: 1,
        },
      },
      additionalProperties: false,
    });
  });

  it("explains invalid rows in plain words and blocks the output", () => {
    const model = schemaToModel({});
    const a = addRow(model, null);
    const b = addRow(model, null);
    b.name = "x";
    a.name = "x";
    changeType(b, "integer");
    b.min = "5";
    b.max = "2";
    b.choices = "1\ntwo";
    const c = addRow(model, null);
    c.name = "text";
    c.min = "-1";
    const empty = addRow(model, null);
    const messages = designerIssues(model).map((i) => [i.field, i.message]);
    expect(messages).toEqual([
      ["name", "Another field here is already named “x”."],
      ["name", "Another field here is already named “x”."],
      ["max", "The largest value is smaller than the smallest."],
      ["choices", "“two” is not a whole number."],
      ["min", "Enter a whole number of 0 or more."],
      ["name", "Enter a field name."],
    ]);
    expect(designerIssues(model).map((i) => i.row)).toEqual([
      a.uid,
      b.uid,
      b.uid,
      b.uid,
      c.uid,
      empty.uid,
    ]);
    for (const [, message] of messages) expect(message).not.toMatch(/WV-/);
  });
});

describe("sample inference", () => {
  it("adapts the local http-action builder: samples become the body, items the schema", async () => {
    const calls: [string, unknown][] = [];
    const infer = inferViaHttpAction(async (path, body) => {
      calls.push([path, body]);
      return {
        ok: true,
        action: {
          spec: {
            inputSchema: {
              properties: {
                body: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: { id: { type: "integer" } },
                  },
                },
              },
            },
          },
        },
        diagnostics: [
          {
            code: "WV-HTTP-ACTION-TRUNCATED",
            severity: "warning",
            path: "/bodySample",
            message: "The sample exceeds the inference budget.",
          },
          {
            code: "WV-COMP-X",
            severity: "warning",
            path: "/spec/outputSchema",
            message: "ignored",
          },
        ],
      };
    });
    const result = await infer([{ id: 1 }, { id: 2 }]);
    expect(calls).toEqual([
      [
        "/studio/local/http-action",
        {
          request: {
            name: "sample-inference",
            method: "POST",
            pathTemplate: "/sample",
            bodySample: [{ id: 1 }, { id: 2 }],
          },
        },
      ],
    ]);
    expect(result).toEqual({
      schema: { type: "object", properties: { id: { type: "integer" } } },
      warnings: ["The sample exceeds the inference budget."],
    });
    const failing = inferViaHttpAction(async () => ({
      ok: false,
      action: null,
      diagnostics: [
        { severity: "error", path: "/bodySample", message: "Too big." },
      ],
    }));
    await expect(failing([{}])).rejects.toThrow("Too big.");
  });

  it("replaces or adds fields from an inferred schema and explains empty types", () => {
    const model = schemaToModel({
      type: "object",
      properties: { id: { type: "string", title: "Mine" } },
    });
    const inferred = {
      type: "object",
      properties: { id: { type: "integer" }, note: { type: "null" } },
      required: ["id", "note"],
    };
    expect(applyInferred(model, inferred, "add")).toBe(1);
    expect(model.rows.map((r) => [r.name, r.type, r.title])).toEqual([
      ["id", "string", "Mine"],
      ["note", "null", ""],
    ]);
    expect(inferenceNotes(model)).toEqual([
      "“note” was empty in every example, so its type is Empty. Choose its real type.",
    ]);
    expect(applyInferred(model, inferred, "replace")).toBe(2);
    expect(model.rows.map((r) => [r.name, r.type, r.required])).toEqual([
      ["id", "integer", true],
      ["note", "null", true],
    ]);
  });

  it.skipIf(!hasPython)(
    "infers through the real Studio host builder and the result accepts its sample",
    async () => {
      const samples: Json[] = [
        {
          id: 1,
          name: "Ada",
          tags: ["a"],
          address: { city: "Leeds" },
          vip: null,
        },
        { id: 2, name: "Bo", vip: true },
      ];
      const infer = inferViaHttpAction(async (_path, body) =>
        JSON.parse(
          execFileSync(
            python,
            [
              "-c",
              `import json, sys
from firefly_weave.sdk.http_actions import author_http_action
request = json.load(sys.stdin)["request"]
print(json.dumps(author_http_action(request).model_dump(by_alias=True, mode="json")))`,
            ],
            {
              cwd: root,
              input: JSON.stringify(body),
              env: { ...process.env, PYTHONPATH: resolve(root, "src") },
              encoding: "utf8",
              timeout: 180_000,
            },
          ),
        ),
      );
      const { schema } = await infer(samples);
      const model = schemaToModel({});
      applyInferred(model, schema, "replace");
      expect(model.rows.map((r) => [r.name, r.type, r.required])).toEqual([
        ["id", "integer", true],
        ["name", "string", true],
        ["tags", "array", false],
        ["address", "object", false],
        ["vip", "boolean", true],
      ]);
      // null in one example and true in another: a nullable yes-or-no.
      expect(model.rows[4].nullable).toBe(true);
      const emitted = modelToSchema(model);
      const verdict = execFileSync(
        python,
        [
          "-c",
          `import json, sys
from firefly_weave.compiler.schemas import validate_payload, validate_schema
data = json.load(sys.stdin)
issues = list(validate_schema(data["schema"], {}))
for sample in data["samples"]:
    issues += list(validate_payload(data["schema"], sample, {}))
print(json.dumps([i.code for i in issues]))`,
        ],
        {
          cwd: root,
          input: JSON.stringify({ schema: emitted, samples }),
          env: { ...process.env, PYTHONPATH: resolve(root, "src") },
          encoding: "utf8",
          timeout: 180_000,
        },
      );
      expect(JSON.parse(verdict)).toEqual([]);
    },
  );

  it("finds numbers written with a decimal point, by name only", () => {
    const text = `{"price": 10.0, "qty": 2, "rate": 1e3, "note": "9.5",
      "lines": [{"w": 2.50}, {"w": 3}], "grid": [[1.5]], "a\\"b": -0.0,
      "deep": {"x": {"y": 7.25}}, "plain": [1, 2]}`;
    expect(decimalPaths(text)).toEqual([
      ["price"],
      ["rate"],
      ["lines", 0, "w"],
      ["grid", 0, 0],
      ['a"b'],
      ["deep", "x", "y"],
    ]);
    // Values never come back, and nesting beyond inference depth is skipped.
    const deep = "[".repeat(40) + "1.5" + "]".repeat(40);
    expect(decimalPaths(deep)).toEqual([]);
    expect(decimalPaths("3.0")).toEqual([[]]);
  });

  it("widens inferred whole numbers back to numbers where an example had decimals", () => {
    const inferred: Json = {
      type: "object",
      properties: {
        price: { type: "integer" },
        qty: { type: "integer" },
        maybe: { type: ["integer", "null"] },
        lines: {
          type: "array",
          items: {
            type: "object",
            properties: { w: { type: "integer" } },
          },
        },
        byId: { type: "object", additionalProperties: { type: "integer" } },
        opt: {
          anyOf: [
            { type: "object", properties: { v: { type: "integer" } } },
            { type: "null" },
          ],
        },
      },
    };
    const widened = widenDecimals(inferred, [
      ["price"],
      ["maybe"],
      ["lines", 0, "w"],
      ["byId", "3fa85f64-5717-4562-b3fc-2c963f66afa6"],
      ["opt", "v"],
    ]) as { properties: Record<string, unknown> };
    expect(widened.properties).toEqual({
      price: { type: "number" },
      qty: { type: "integer" },
      maybe: { type: ["number", "null"] },
      lines: {
        type: "array",
        items: { type: "object", properties: { w: { type: "number" } } },
      },
      byId: { type: "object", additionalProperties: { type: "number" } },
      opt: {
        anyOf: [
          { type: "object", properties: { v: { type: "number" } } },
          { type: "null" },
        ],
      },
    });
    // Untouched subtrees are shared; nothing to widen returns the input.
    const source = (inferred as { properties: Record<string, unknown> })
      .properties;
    expect(widened.properties["qty"]).toBe(source["qty"]);
    expect(widenDecimals(inferred, [])).toBe(inferred);
  });

  it.skipIf(!hasPython)(
    "keeps decimals typed as numbers through the real host builder",
    async () => {
      const text = '{"amount": 25.00, "items": [{"weight": 1.0, "n": 1}]}';
      const infer = inferViaHttpAction(async (_path, body) =>
        JSON.parse(
          execFileSync(
            python,
            [
              "-c",
              `import json, sys
from firefly_weave.sdk.http_actions import author_http_action
request = json.load(sys.stdin)["request"]
print(json.dumps(author_http_action(request).model_dump(by_alias=True, mode="json")))`,
            ],
            {
              cwd: root,
              input: JSON.stringify(body),
              env: { ...process.env, PYTHONPATH: resolve(root, "src") },
              encoding: "utf8",
              timeout: 180_000,
            },
          ),
        ),
      );
      const { schema } = await infer([JSON.parse(text) as Json]);
      // JSON.parse turned 25.00 into 25, so the host alone sees a whole number.
      expect(getAt(schema, ["properties", "amount", "type"])).toBe("integer");
      const widened = widenDecimals(schema, decimalPaths(text));
      expect(getAt(widened, ["properties", "amount", "type"])).toBe("number");
      expect(
        getAt(widened, ["properties", "items", "items", "properties"]),
      ).toEqual({ weight: { type: "number" }, n: { type: "integer" } });
    },
  );
});

describe.skipIf(!hasPython)("platform schema profile parity", () => {
  it("every schema the designer writes passes the server's schema check", () => {
    const outputs: unknown[] = [];
    // Every row type, nullable, choices, bounds, formats, groups and lists.
    const model = schemaToModel({});
    for (const type of [
      "string",
      "number",
      "integer",
      "boolean",
      "object",
      "array",
      "any",
      "null",
    ] as const) {
      const r = addRow(model, null);
      r.name = `field_${type}`;
      changeType(r, type);
      r.title = `Title ${type}`;
      r.description = "Shown as help text.";
      if (type === "string") {
        r.min = "1";
        r.max = "20";
        r.format = "email";
      }
      if (type === "number" || type === "integer") {
        r.min = "0";
        r.max = "10";
        r.choices = "1\n2";
        r.nullable = true;
      }
      if (type === "array") {
        r.min = "1";
        r.max = "3";
        changeType(r.item!, "object");
        const inner = addRow(model, r.item!);
        inner.name = "inner";
        inner.required = true;
        r.item!.closed = true;
      }
      if (type === "object") {
        const inner = addRow(model, r);
        inner.name = "nested";
        changeType(inner, "boolean");
        r.closed = true;
      }
    }
    model.rows[0].required = true;
    model.closed = true;
    expect(designerIssues(model)).toEqual([]);
    outputs.push(modelToSchema(model));
    // Every shipped schema after a no-op round trip, and with a field added.
    for (const item of fixture.schemas) {
      const m = schemaToModel(item.schema);
      outputs.push(modelToSchema(m));
      if (m.editable) {
        const extra = addRow(m, null);
        extra.name = "studio_added";
        outputs.push(modelToSchema(m));
      }
    }
    const verdict = execFileSync(
      python,
      [
        "-c",
        `import json, sys
from firefly_weave.compiler.schemas import validate_schema
print(json.dumps([[i.code for i in validate_schema(s, {})] for s in json.load(sys.stdin)]))`,
      ],
      {
        cwd: root,
        input: JSON.stringify(outputs),
        env: { ...process.env, PYTHONPATH: resolve(root, "src") },
        encoding: "utf8",
        timeout: 180_000,
      },
    );
    const results = JSON.parse(verdict) as string[][];
    expect(results).toHaveLength(outputs.length);
    expect(results.filter((codes) => codes.length)).toEqual([]);
  });
});

describe("new rows", () => {
  it("start as optional text with no name", () => {
    const r = newRow();
    expect(r).toMatchObject({ name: "", type: "string", required: false });
  });
});
