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
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  fieldable,
  formFields,
  localDateTime,
  missingBound,
  missingData,
  prepareData,
  storedDateTime,
  validReference,
  widgetFor,
} from "../src/app/forms/core/form-model";
import { fieldsOf, type FieldInfo } from "../src/app/forms/core/resolve";

const schema = {
  type: "object",
  required: ["confirm", "token", "kind", "details"],
  properties: {
    confirm: { type: "boolean", title: "Confirm" },
    notify: { type: "boolean", title: "Notify" },
    token: { type: "string", "x-secret": true },
    password: { type: "string", writeOnly: true },
    kind: { const: "lookup" },
    retry: { type: "boolean", default: true },
    details: {
      type: "object",
      required: ["strict", "apiKey"],
      properties: {
        strict: { type: "boolean", default: true },
        apiKey: { type: "boolean", "x-secret": true },
        name: { type: "string" },
      },
    },
    extra: {
      type: "object",
      required: ["flag"],
      properties: { flag: { type: "boolean" } },
    },
  },
};

describe("form start values", () => {
  it("fills required booleans and constants and never keeps a secret", () => {
    const { data, changed } = prepareData(schema, {
      token: "s3cret",
      password: "hunter2",
      details: { apiKey: true },
    });
    expect(changed).toBe(true);
    expect(data).toEqual({
      confirm: false,
      kind: "lookup",
      details: { strict: true },
    });
    // An optional boolean stays unset; absent is not false.
    expect(data).not.toHaveProperty("notify");
    expect(data).not.toHaveProperty("retry");
    // An optional group without data is not created.
    expect(data).not.toHaveProperty("extra");
    expect(JSON.stringify(data)).not.toMatch(/s3cret|hunter2|apiKey/);
  });

  it("fills an optional group's required boolean once the group has data", () => {
    expect(prepareData(schema, { extra: {} }).data["extra"]).toEqual({
      flag: false,
    });
  });

  it("leaves complete data untouched", () => {
    const complete = {
      confirm: true,
      kind: "lookup",
      details: { strict: false },
    };
    const { data, changed } = prepareData(schema, complete);
    expect(changed).toBe(false);
    expect(data).toEqual(complete);
    expect(data).not.toBe(complete);
  });
});

describe("required fields still missing", () => {
  it("skips secrets and constants, and reads nested groups", () => {
    expect(missingData(schema, {})).toEqual(["Confirm", "Details"]);
    expect(missingData(schema, { confirm: false, details: {} })).toEqual([
      "Details › Strict",
    ]);
  });

  it("counts a field bound to data or a formula as set", () => {
    const action = {
      type: "object",
      required: ["customerId", "region", "note"],
      properties: {
        customerId: { type: "string", minLength: 1 },
        region: { type: "string" },
        note: { type: "string" },
      },
    };
    expect(
      missingBound(action, {
        object: {
          customerId: { ref: "/input/customerId" },
          region: { op: { name: "coalesce", args: [{ literal: "eu" }] } },
          note: { literal: "" },
        },
      }),
    ).toEqual(["Note"]);
    expect(missingBound(action, { literal: { region: "eu" } })).toEqual([
      "Customer ID",
      "Note",
    ]);
    // A whole-input reference is the compiler's to check.
    expect(missingBound(action, { ref: "/input" })).toEqual([]);
    expect(missingBound(action, undefined)).toEqual([
      "Customer ID",
      "Region",
      "Note",
    ]);
  });
});

describe("widgets", () => {
  const widget = (field: object, required = false) =>
    widgetFor(
      fieldsOf({
        type: "object",
        required: required ? ["f"] : [],
        properties: { f: field },
      })[0],
    );

  it("gives booleans, formats, maps, tables and constants their own editors", () => {
    expect(widget({ type: "boolean" }, true)).toBe("checkbox");
    expect(widget({ type: "boolean" })).toBe("tristate");
    expect(widget({ type: "string", format: "date-time" })).toBe("datetime");
    expect(widget({ type: "string", format: "email" })).toBe("email");
    expect(widget({ type: "string", format: "uri" })).toBe("uri");
    expect(widget({ type: "string", format: "uuid" })).toBe("uuid");
    expect(
      widget({ type: "object", additionalProperties: { type: "string" } }),
    ).toBe("map");
    expect(
      widget({
        type: "array",
        items: { type: "object", properties: { a: { type: "string" } } },
      }),
    ).toBe("table");
    expect(widget({ const: 2 })).toBe("const");
    expect(
      widget({ oneOf: [{ const: "a", title: "A" }, { const: "b" }] }),
    ).toBe("choice");
    expect(widget({ type: "string", "x-secret": true })).toBe("secret");
    expect(widget({})).toBe("any");
    expect(widget({ not: { type: "string" } })).toBe("json");
  });

  it("leaves at most 10 of the 153 shipped fields to a JSON editor", () => {
    const fixture = JSON.parse(
      readFileSync(
        resolve(import.meta.dirname, "fixtures/shipped-schemas.json"),
        "utf8",
      ),
    ) as { schemas: { id: string; schema: Record<string, unknown> }[] };
    const json: string[] = [];
    let fields = 0;
    const walk = (list: FieldInfo[], id: string, depth: number) => {
      for (const field of list) {
        fields++;
        const kind = widgetFor(field, depth);
        if (kind === "json") json.push(`${id} › ${field.key}`);
        if (kind === "group")
          walk(fieldsOf(field.schema, field.context), id, depth + 1);
      }
    };
    for (const item of fixture.schemas)
      walk(formFields(item.schema), item.id, 0);
    expect(fields).toBe(153);
    expect(json.length, json.join("\n")).toBeLessThanOrEqual(10);
  });
});

describe("small helpers", () => {
  it("knows which inputs can be edited field by field", () => {
    expect(fieldable(undefined)).toBe(true);
    expect(fieldable({ literal: {} })).toBe(true);
    expect(fieldable({ object: { a: { ref: "/input" } } })).toBe(true);
    expect(fieldable({ ref: "/input" })).toBe(false);
    expect(fieldable({ literal: 3 })).toBe(false);
    expect(fieldable({ op: { name: "coalesce", args: [] } })).toBe(false);
  });

  it("round-trips date-times through the local picker", () => {
    const stored = storedDateTime(localDateTime("2026-10-02T09:30:00Z"));
    expect(stored).toBe("2026-10-02T09:30:00.000Z");
    expect(storedDateTime("")).toBeUndefined();
    expect(localDateTime("not a date")).toBe("");
  });

  it("accepts any JSON pointer as a data reference", () => {
    expect(validReference("/input/customerId")).toBe(true);
    expect(validReference("/steps/a~1b/output")).toBe(true);
    expect(validReference("input")).toBe(false);
    expect(validReference("/bad~2")).toBe(false);
    expect(validReference("")).toBe(false);
  });
});

const root = resolve(import.meta.dirname, "../..");
const python = resolve(root, ".venv/bin/python");

describe.skipIf(!existsSync(python))("parity with the compiler", () => {
  it("start values for a secret boolean compile without a secret value", () => {
    const input = {
      type: "object",
      additionalProperties: false,
      required: ["confirm", "apiKey"],
      properties: {
        confirm: { type: "boolean" },
        verbose: { type: "boolean" },
        apiKey: { type: "boolean", "x-secret": true },
      },
    };
    const codes = (literal: unknown) =>
      JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            `
import json, sys
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
schema, literal = json.loads(sys.stdin.read())
action = {"apiVersion": "weave/v1alpha1", "kind": "Action",
          "metadata": {"name": "secret.flag", "version": "1.0.0"},
          "spec": {"implementation": {"kind": "worker", "taskType": "secret.flag", "taskVersion": "1.0.0"},
                   "sideEffect": "read_only", "timeoutSeconds": 30,
                   "inputSchema": schema, "outputSchema": {"type": "object"}}}
catalog = CatalogSnapshot.from_definitions([load_definition(action)], tasks=[{
    "taskType": "secret.flag", "taskVersion": "1.0.0", "inputSchema": schema,
    "outputSchema": {"type": "object"}, "sideEffect": "read_only", "timeoutSeconds": 30}])
workflow = {"apiVersion": "weave/v1alpha1", "kind": "Workflow",
            "metadata": {"name": "w", "version": "1.0.0"},
            "spec": {"inputSchema": {"type": "object"}, "outputSchema": {"type": "object"},
                     "steps": [{"id": "call", "kind": "action", "uses": "secret.flag@1.0.0",
                                "with": {"literal": literal}}],
                     "output": {"literal": {}}}}
result = compile_source(json.dumps(workflow), format="json", catalog=catalog)
print(json.dumps([d.code for d in result.diagnostics]))
`,
          ],
          {
            cwd: root,
            env: { ...process.env, PYTHONPATH: resolve(root, "src") },
            encoding: "utf8",
            input: JSON.stringify([input, literal]),
            timeout: 180_000,
          },
        ),
      ) as string[];
    // What the old "every boolean is false" default wrote is rejected...
    expect(codes({ confirm: false, verbose: false, apiKey: false })).toContain(
      "WV-SCHEMA-SECRET_VALUE",
    );
    // ...while the form's start values are not.
    const prepared = prepareData(input, {}).data;
    expect(prepared).toEqual({ confirm: false });
    expect(codes(prepared)).not.toContain("WV-SCHEMA-SECRET_VALUE");
  });
});

it("a file reference is one file widget and is not seeded with an incomplete discriminator", () => {
  const file = {
    type: "object",
    required: ["kind", "id"],
    properties: { kind: { const: "weave/file" }, id: { type: "string" } },
  };
  const schema = {
    type: "object",
    required: ["attachment"],
    properties: { attachment: file },
  };
  expect(widgetFor(formFields(schema)[0])).toBe("file");
  expect(prepareData(schema, {}).data).toEqual({});
  expect(
    widgetFor(
      formFields({
        type: "object",
        properties: {
          group: { type: "object", properties: { kind: { const: "other" } } },
        },
      })[0],
    ),
  ).toBe("group");
});
