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
import { execFileSync, spawnSync } from "node:child_process";
import { resolve as resolvePath } from "node:path";
import { describe, expect, it } from "vitest";
import {
  describeField,
  fieldLabel,
  fieldsOf,
  humanize,
  isPydanticDefaultTitle,
  resolveSchema,
  schemaTitle,
  type FieldInfo,
  type FieldKind,
} from "../src/app/forms/core/resolve";
import { groupedObject, scalarList } from "../src/app/task-form";

const root = resolvePath(import.meta.dirname, "../..");
const fixturePath = resolvePath(
  import.meta.dirname,
  "fixtures/shipped-schemas.json",
);
interface Shipped {
  id: string;
  origin: string;
  role: string;
  schema: Record<string, unknown>;
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as {
  summary: {
    schemas: number;
    fields: number;
    taskFormWidgets: Record<string, number>;
  };
  schemas: Shipped[];
};

describe("$ref and $defs", () => {
  it("resolves local references against the document root", () => {
    const document = {
      $defs: {
        Name: { $ref: "#/$defs/Text" },
        Text: { type: "string", maxLength: 10 },
      },
      properties: { name: { $ref: "#/$defs/Name", title: "Customer name" } },
    };
    const field = describeField("name", document.properties.name, {
      root: document,
    });
    expect(field.kind).toBe("text");
    expect(field.schema["maxLength"]).toBe(10);
    // Siblings of $ref apply too (2020-12) and win over the target.
    expect(field.label).toBe("Customer name");
  });

  it("resolves bundle keys and pointers into bundle documents", () => {
    const bundle = {
      "common.json": { $defs: { Id: { type: "string", format: "uuid" } } },
      "money.json": { type: "number", minimum: 0 },
    };
    expect(
      resolveSchema({ $ref: "common.json#/$defs/Id" }, { bundle }).schema,
    ).toEqual({ type: "string", format: "uuid" });
    expect(
      resolveSchema({ $ref: "money.json" }, { bundle }).schema["minimum"],
    ).toBe(0);
  });

  it("cuts reference cycles and reports unresolved references", () => {
    const document = {
      $defs: {
        A: { $ref: "#/$defs/B" },
        B: { $ref: "#/$defs/A" },
      },
    };
    const cyclic = resolveSchema({ $ref: "#/$defs/A" }, { root: document });
    expect(cyclic.cyclic).toBe(true);
    expect(
      describeField("x", { $ref: "#/$defs/A" }, { root: document }).kind,
    ).toBe("json");
    const missing = resolveSchema({ $ref: "#/$defs/Nope" }, { root: {} });
    expect(missing.unresolved).toEqual(["#/$defs/Nope"]);
    expect(
      describeField("x", { $ref: "#/$defs/Nope" }, { root: {} }).kind,
    ).toBe("json");
    // Remote references are never fetched.
    expect(
      resolveSchema({ $ref: "https://example.com/s.json" }).unresolved,
    ).toEqual(["https://example.com/s.json"]);
  });

  it("expands a recursive structure one level at a time without looping", () => {
    const tree = {
      $defs: {
        Node: {
          type: "object",
          properties: {
            name: { type: "string" },
            children: { type: "array", items: { $ref: "#/$defs/Node" } },
          },
        },
      },
      $ref: "#/$defs/Node",
    };
    const fields = fieldsOf(tree, { root: tree });
    expect(fields.map((f) => [f.key, f.kind])).toEqual([
      ["name", "text"],
      ["children", "table"],
    ]);
  });

  it("classifies a recursive list without recursing forever", () => {
    // Trusted contract schemas may recurse, and Source can hold one before
    // Validate rejects it; reading the field must not overflow the stack.
    const nested = { type: "array", items: { $ref: "#" } };
    expect(describeField("tree", nested).kind).toBe("json");
    const viaDefs = {
      type: "object",
      properties: { rows: { $ref: "#/$defs/Rows" } },
      $defs: { Rows: { type: "array", items: { $ref: "#/$defs/Rows" } } },
    };
    expect(fieldsOf(viaDefs).map((f) => f.kind)).toEqual(["json"]);
    // Lists of lists keep their kind; the guard is released afterwards.
    expect(
      describeField("grid", {
        type: "array",
        items: { type: "array", items: { type: "integer" } },
      }).kind,
    ).toBe("json");
    expect(
      describeField("tags", { type: "array", items: { type: "string" } }).kind,
    ).toBe("list");
  });
});

describe("normalization", () => {
  it("merges allOf object branches", () => {
    const merged = resolveSchema({
      allOf: [
        {
          type: "object",
          properties: { a: { type: "string" } },
          required: ["a"],
        },
        {
          properties: { b: { type: "integer" }, a: { maxLength: 5 } },
          required: ["b"],
        },
      ],
    });
    expect(merged.schema["type"]).toBe("object");
    expect(merged.schema["required"]).toEqual(["a", "b"]);
    expect(merged.schema["properties"]).toEqual({
      a: { allOf: [{ type: "string" }, { maxLength: 5 }] },
      b: { type: "integer" },
    });
    expect(
      describeField("a", merged.schema["properties"]!["a" as never]).kind,
    ).toBe("text");
    expect(merged.unsupported).toEqual([]);
  });

  it("keeps conflicting allOf branches as an unsupported construct", () => {
    const conflict = resolveSchema({
      allOf: [{ type: "string" }, { type: "integer" }],
    });
    expect(conflict.unsupported).toContain("allOf");
    expect(
      describeField("x", { allOf: [{ type: "string" }, { type: "integer" }] })
        .kind,
    ).toBe("json");
  });

  it("normalizes the three nullable spellings", () => {
    for (const schema of [
      { type: ["string", "null"], maxLength: 3 },
      { anyOf: [{ type: "string", maxLength: 3 }, { type: "null" }] },
      { oneOf: [{ type: "null" }, { type: "string", maxLength: 3 }] },
    ]) {
      const field = describeField("x", schema);
      expect(field.nullable, JSON.stringify(schema)).toBe(true);
      expect(field.kind).toBe("text");
      expect(field.schema["maxLength"]).toBe(3);
    }
    expect(describeField("x", { type: "null" }).kind).toBe("const");
  });

  it("treats boolean schemas as any value and no value", () => {
    expect(describeField("x", true).kind).toBe("any");
    expect(describeField("x", {}).kind).toBe("any");
    expect(describeField("x", false).kind).toBe("never");
    expect(resolveSchema(false).never).toBe(true);
  });
});

describe("widget kinds", () => {
  const kind = (schema: unknown): FieldKind => describeField("x", schema).kind;

  it("reads constants, choices and labelled oneOf choices", () => {
    const fixed = describeField("driver", { const: "kafka", type: "string" });
    expect(fixed.kind).toBe("const");
    expect(fixed.constValue).toBe("kafka");
    const choice = describeField("tls", {
      enum: ["tls", "starttls", null],
    });
    expect(choice.kind).toBe("choice");
    expect(choice.nullable).toBe(true);
    expect(choice.options).toEqual([
      { value: "tls", label: "tls" },
      { value: "starttls", label: "starttls" },
      { value: null, label: "None" },
    ]);
    const labelled = describeField("tier", {
      oneOf: [
        { const: "gold", title: "Gold customer" },
        { const: "std", title: "Standard" },
      ],
    });
    expect(labelled.kind).toBe("choice");
    expect(labelled.options).toEqual([
      { value: "gold", label: "Gold customer" },
      { value: "std", label: "Standard" },
    ]);
  });

  it("picks inputs from the four supported string formats", () => {
    expect(kind({ type: "string", format: "date-time" })).toBe("datetime");
    expect(kind({ type: "string", format: "email" })).toBe("email");
    expect(kind({ type: "string", format: "uri" })).toBe("uri");
    expect(kind({ type: "string", format: "uuid" })).toBe("uuid");
    expect(describeField("x", { type: "string", format: "uuid" }).format).toBe(
      "uuid",
    );
    expect(kind({ type: "string", format: "hostname" })).toBe("text");
    expect(kind({ type: "string", maxLength: 16384 })).toBe("multiline");
    expect(kind({ type: "string", maxLength: 256 })).toBe("text");
    expect(kind({ type: "string" })).toBe("text");
  });

  it("classifies scalars, groups, maps, lists, tables, unions and secrets", () => {
    expect(kind({ type: "integer" })).toBe("integer");
    expect(kind({ type: "number" })).toBe("number");
    expect(kind({ type: "boolean" })).toBe("boolean");
    expect(kind({ type: "object", properties: { a: {} } })).toBe("object");
    expect(kind({ type: "object" })).toBe("map");
    expect(
      kind({ type: "object", additionalProperties: { type: "string" } }),
    ).toBe("map");
    expect(kind({ type: "object", additionalProperties: false })).toBe(
      "object",
    );
    expect(kind({ type: "array", items: { type: "string" } })).toBe("list");
    expect(kind({ type: "array", items: { enum: ["a", "b"] } })).toBe("list");
    expect(kind({ type: "array" })).toBe("list");
    expect(
      kind({ type: "array", items: { type: "object", properties: { a: {} } } }),
    ).toBe("table");
    expect(kind({ type: "array", prefixItems: [{ type: "string" }] })).toBe(
      "json",
    );
    expect(kind({ type: ["string", "integer"] })).toBe("union");
    expect(
      kind({
        oneOf: [
          { type: "object", properties: { kind: { const: "a" } } },
          { type: "object", properties: { kind: { const: "b" } } },
        ],
      }),
    ).toBe("union");
    expect(kind({ type: "string", "x-secret": true })).toBe("secret");
    expect(kind({ type: "string", writeOnly: true })).toBe("secret");
    expect(
      describeField("x", { type: "string", "x-secret": true }).secret,
    ).toBe(true);
  });

  it("renders the base type of a schema with unsupported extras and lists them", () => {
    const field = describeField("message_id", {
      type: "string",
      not: { pattern: "[^0-9]" },
      pattern: "^[1-9]",
    });
    expect(field.kind).toBe("text");
    expect(field.unsupported).toEqual(["not"]);
    expect(kind({ if: { type: "string" }, then: { minLength: 1 } })).toBe(
      "json",
    );
  });

  it("marks required, read-only, deprecated and annotation members", () => {
    const fields = fieldsOf({
      type: "object",
      required: ["a"],
      properties: {
        a: { type: "string", default: "x", examples: ["y"] },
        b: { type: "string", readOnly: true, deprecated: true },
      },
    });
    expect(fields[0]).toMatchObject({
      key: "a",
      required: true,
      default: "x",
      examples: ["y"],
    });
    expect(fields[1]).toMatchObject({
      key: "b",
      required: false,
      readOnly: true,
      deprecated: true,
    });
  });
});

describe("labels", () => {
  it("ignores Pydantic default titles and humanizes the key instead", () => {
    expect(isPydanticDefaultTitle("sasl_mechanism", "Sasl Mechanism")).toBe(
      true,
    );
    expect(isPydanticDefaultTitle("maxBytes", "Maxbytes")).toBe(true);
    expect(isPydanticDefaultTitle("activity_id", "Activity Id")).toBe(true);
    expect(isPydanticDefaultTitle("customerId", "Customer")).toBe(false);
    expect(fieldLabel("sasl_mechanism", { title: "Sasl Mechanism" })).toBe(
      "SASL mechanism",
    );
    expect(fieldLabel("maxBytes", { title: "Maxbytes" })).toBe("Max bytes");
    expect(fieldLabel("activity_id", { title: "Activity Id" })).toBe(
      "Activity ID",
    );
    expect(fieldLabel("tls", { title: "Tls" })).toBe("TLS");
    expect(fieldLabel("customerId", {})).toBe("Customer ID");
    expect(fieldLabel("customerId", { title: "Customer" })).toBe("Customer");
    expect(fieldLabel("x", { title: "   " })).toBe("X");
  });

  it("humanizes snake, kebab, camel and Pascal case keys", () => {
    expect(humanize("statement_timeout_ms")).toBe("Statement timeout ms");
    expect(humanize("api-key")).toBe("API key");
    expect(humanize("baseURL")).toBe("Base URL");
    expect(humanize("HTTPStatus")).toBe("HTTP status");
    expect(humanize("retry2Count")).toBe("Retry 2 count");
    expect(humanize("")).toBe("");
  });

  it("drops class-name root titles that Pydantic generates", () => {
    expect(schemaTitle({ title: "BrokerConnectionConfig" })).toBeUndefined();
    expect(schemaTitle({ title: "Customer details" })).toBe("Customer details");
    expect(schemaTitle({ title: "Customer" })).toBe("Customer");
  });
});

/** Walks fields the way TaskForm nests them: groups to depth 4. */
function walkFields(
  schema: unknown,
  rootSchema: unknown,
  visit: (field: FieldInfo) => void,
  depth = 0,
) {
  for (const field of fieldsOf(schema, { root: rootSchema })) {
    visit(field);
    if (field.kind === "object" && depth + 1 < 4)
      walkFields(field.schema, rootSchema, visit, depth + 1);
  }
}

describe("shipped schema corpus", () => {
  it("is the documented 55-schema, 153-field corpus", () => {
    expect(fixture.summary.schemas).toBe(55);
    expect(fixture.schemas).toHaveLength(55);
    expect(fixture.summary.fields).toBe(153);
    expect(fixture.summary.taskFormWidgets["json"]).toBe(40);
  });

  it("agrees with TaskForm's own rules on today's 40 JSON fallbacks", () => {
    // Recount with the component's exported predicates, not the Python copy.
    let fields = 0;
    let json = 0;
    const scalar = new Set(["string", "number", "integer", "boolean"]);
    const count = (schema: Record<string, unknown>, depth: number) => {
      for (const field of Object.values(
        (schema["properties"] ?? {}) as Record<string, Record<string, unknown>>,
      )) {
        fields++;
        if (groupedObject(field) && depth + 1 < 4) count(field, depth + 1);
        else if (
          scalarList(field) ||
          field["enum"] ||
          field["type"] === "boolean"
        )
          continue;
        else if (!scalar.has(String(field["type"]))) json++;
      }
    };
    for (const item of fixture.schemas) count(item.schema, 0);
    expect(fields).toBe(153);
    expect(json).toBe(40);
  });

  it("leaves at most 10 fields to a raw JSON editor once forms use the resolver", () => {
    const fallbacks: string[] = [];
    const kinds: Record<string, number> = {};
    let fields = 0;
    for (const item of fixture.schemas)
      walkFields(item.schema, item.schema, (field) => {
        fields++;
        kinds[field.kind] = (kinds[field.kind] ?? 0) + 1;
        if (field.kind === "json") fallbacks.push(`${item.id} › ${field.key}`);
      });
    expect(fields).toBe(153);
    // WP-12 accepts up to 10; the resolver reaches none. Keep it a ratchet:
    // a new fallback must be a deliberate, reviewed change to this list.
    expect(fallbacks, fallbacks.join("\n")).toEqual([]);
    // Bodies typed `{}` are "any value" (a typed value tree), not raw JSON.
    expect(kinds).toEqual({
      any: 4,
      boolean: 2,
      choice: 10,
      const: 4,
      integer: 20,
      list: 13,
      map: 18,
      multiline: kinds["multiline"],
      number: 1,
      object: 1,
      table: 3,
      text: kinds["text"],
      uuid: 4,
    });
    expect(kinds["multiline"] + kinds["text"]).toBe(73);
    // Every label is plain: no raw Pydantic titles such as "Sasl Mechanism".
    for (const item of fixture.schemas)
      walkFields(item.schema, item.schema, (field) => {
        expect(field.label).not.toMatch(/_/);
        expect(field.label.trim()).toBe(field.label);
      });
  });
});

const python = resolvePath(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
// The corpus covers every first-party connector, including Teams, whose
// descriptor needs the optional `teams` extra. An environment without it, such
// as the desktop build's `--extra studio` install, cannot regenerate the corpus.
const connectorExtras =
  existsSync(python) &&
  spawnSync(
    python,
    [
      "-c",
      "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('microsoft_agents') else 1)",
    ],
    { timeout: 180_000 },
  ).status === 0;
describe.skipIf(!connectorExtras)("fixture freshness", () => {
  it("matches what scripts/studio_schema_fixtures.py generates today", () => {
    // Throws (non-zero exit) when the committed corpus is out of date.
    execFileSync(python, ["scripts/studio_schema_fixtures.py", "--check"], {
      cwd: root,
      env: { ...process.env, PYTHONPATH: resolvePath(root, "src") },
      encoding: "utf8",
      timeout: 180_000,
    });
  });
});
