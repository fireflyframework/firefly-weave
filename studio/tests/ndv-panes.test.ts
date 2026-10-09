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
import { describe, expect, it } from "vitest";
import {
  safeSample,
  sampleFromSchema,
} from "../src/app/editor/ndv/panes/sample";
import {
  VIEWS_KEY,
  dragSchema,
  defaultOutputView,
  countLabel,
  sampleCount,
  defaultSource,
  filterRows,
  inputSources,
  noOutputSentence,
  readViews,
  schemaRows,
  sourceRows,
  typeIcon,
  writeViews,
} from "../src/app/editor/ndv/panes/views";
import { referenceScope } from "../src/app/forms/core/scope";
import { createStep } from "../src/app/model";

const schema = {
  type: "object",
  required: ["customerId"],
  properties: {
    customerId: { type: "string", title: "Customer ID" },
    amount: { type: "number" },
    shipping: {
      type: "object",
      properties: { country: { type: "string" }, express: { type: "boolean" } },
    },
    items: {
      type: "array",
      items: { type: "object", properties: { sku: { type: "string" } } },
    },
  },
};

describe("data panes", () => {
  it("lists a schema's fields with their depth and type", () => {
    expect(
      schemaRows(schema).map((r) => [r.label, r.depth, r.typeLabel]),
    ).toEqual([
      ["Customer ID", 0, "Text"],
      ["amount", 0, "Number"],
      ["shipping", 0, "Group"],
      ["country", 1, "Text"],
      ["express", 1, "Yes/No"],
      ["items", 0, "List of groups"],
    ]);
    expect(typeIcon(["string"], "Text")).toBe("typeText");
    expect(typeIcon(["array"], "List")).toBe("typeList");
    expect(typeIcon([], "Any value")).toBe("typeAny");
  });
  it("offers the workflow input and every visible step, nearest first", () => {
    const workflow = {
      kind: "Workflow",
      metadata: { name: "w", version: "1.0.0" },
      spec: {
        inputSchema: schema,
        steps: [
          {
            id: "check",
            kind: "transform",
            value: { object: { ok: { literal: true } } },
          },
          { id: "score", kind: "transform", value: { literal: 3 } },
          { id: "use", kind: "transform", value: { ref: "/input" } },
        ],
      },
    };
    const scope = referenceScope(workflow, "use", "/value");
    const sources = inputSources(scope);
    expect(sources.map((s) => [s.id, s.label, s.detail])).toEqual([
      ["step:score", "score", "1 step back"],
      ["step:check", "check", "2 steps back"],
      ["input", "Workflow input", ""],
    ]);
    expect(defaultSource(sources)).toBe("step:score");
    expect(
      sourceRows(scope.entries, "input").map((r) => [r.label, r.ref]),
    ).toContainEqual(["Customer ID", "/input/customerId"]);
    expect(sourceRows(scope.entries, "step:check").map((r) => r.ref)).toEqual([
      "/steps/check/output/ok",
    ]);
  });
  it("filters rows and counts what shows", () => {
    const rows = schemaRows(schema);
    expect(filterRows(rows, "count").map((r) => r.label)).toEqual(["country"]);
    expect(countLabel(4)).toBe("4 fields");
    expect(countLabel(4, 1)).toBe("1 of 4 fields");
    expect(countLabel(1)).toBe("1 field");
    expect(countLabel(12, undefined, "item")).toBe("12 items");
  });
  it("remembers the person's views, and survives bad storage", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (k: string) => values.get(k) ?? null,
      setItem: (k: string, v: string) => void values.set(k, v),
    };
    expect(readViews(storage)).toEqual({ input: "schema", output: "schema" });
    expect(writeViews({ input: "json", output: "table" }, storage)).toBe(true);
    expect(JSON.parse(values.get(VIEWS_KEY)!)).toEqual({
      input: "json",
      output: "table",
    });
    expect(readViews(storage)).toEqual({ input: "json", output: "table" });
    values.set(VIEWS_KEY, "nope");
    expect(readViews(storage)).toEqual({ input: "schema", output: "schema" });
  });
  it("says why some steps have no output", () => {
    expect(
      noOutputSentence({ ...createStep("wait", "w"), durationSeconds: 300 }),
    ).toBe("Waits 5 min, then continues. It has no output.");
    expect(
      noOutputSentence({ ...createStep("fail", "f"), code: "order-rejected" }),
    ).toBe("Stops the run with error order-rejected.");
    expect(noOutputSentence(createStep("transform", "t"))).toBeNull();
  });
  it("generates a sample that fits the schema", () => {
    expect(sampleFromSchema(schema)).toEqual({
      customerId: "text",
      amount: 1,
      shipping: { country: "text", express: true },
      items: [{ sku: "text" }],
    });
    expect(sampleFromSchema({ type: "string", enum: ["open", "closed"] })).toBe(
      "open",
    );
    expect(sampleFromSchema({ type: "string", format: "email" })).toBe(
      "name@example.com",
    );
    expect(sampleFromSchema({ type: "integer", minimum: 5 })).toBe(5);
  });
});

describe("data pane edge cases", () => {
  it("uses escaped unique keys and retains scalar root rows", () => {
    const rows = schemaRows({
      type: "object",
      properties: {
        "a.b": { type: "string" },
        a: { type: "object", properties: { b: { type: "number" } } },
        "a/b~c": { type: "boolean" },
      },
    });
    expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
    expect(rows.at(-1)?.key).toBe("/a~1b~0c");
    expect(schemaRows({ type: "null" })[0].types).toEqual(["null"]);
    const scope = referenceScope(
      {
        kind: "Workflow",
        metadata: { name: "w", version: "1" },
        spec: {
          inputSchema: { type: "string" },
          steps: [{ id: "use", kind: "transform", value: { ref: "/input" } }],
        },
      },
      "use",
      "/value",
    );
    expect(sourceRows(scope.entries, "input")[0].ref).toBe("/input");
  });
  it("survives a denied localStorage getter", () => {
    const descriptor = Object.getOwnPropertyDescriptor(
      globalThis,
      "localStorage",
    );
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw Error("denied");
      },
    });
    try {
      expect(readViews()).toEqual({ input: "schema", output: "schema" });
      expect(writeViews({ input: "json", output: "json" })).toBe(false);
    } finally {
      if (descriptor)
        Object.defineProperty(globalThis, "localStorage", descriptor);
      else Reflect.deleteProperty(globalThis, "localStorage");
    }
  });
  it("bounds candidates, refuses impossible and unsupported schemas, and omits secrets", () => {
    expect(() => sampleFromSchema(false)).toThrow();
    expect(() =>
      sampleFromSchema({ type: "number", minimum: 5, maximum: 2 }),
    ).toThrow();
    expect(() =>
      sampleFromSchema({ type: "string", pattern: "^secret$" }),
    ).toThrow();
    expect(() =>
      sampleFromSchema({ type: "array", minItems: 10000, items: {} }),
    ).toThrow();
    expect(sampleFromSchema({ type: "null" })).toBeNull();
    expect(sampleFromSchema({ type: "number", maximum: 0 })).toBe(0);
    expect(
      sampleFromSchema({
        type: "array",
        maxItems: 0,
        items: { type: "string" },
      }),
    ).toEqual([]);
    const schema = {
      type: "object",
      properties: {
        name: { type: "string" },
        token: { type: "string", writeOnly: true, default: "private" },
      },
    };
    expect(sampleFromSchema(schema)).toEqual({ name: "text" });
    expect(schema.properties.token.default).toBe("private");
    expect(
      sampleFromSchema({
        $defs: { name: { type: "string" } },
        type: "object",
        properties: { name: { $ref: "#/$defs/name" } },
      }),
    ).toEqual({ name: "text" });
  });
});

it("does not put schema value annotations in drag metadata", () => {
  expect(
    dragSchema({
      type: "object",
      default: { token: "private" },
      properties: {
        token: {
          type: "string",
          writeOnly: true,
          const: "private",
          enum: ["private"],
          examples: ["private"],
        },
      },
    }),
  ).toEqual({
    type: "object",
    properties: { token: { type: "string", writeOnly: true } },
  });
});
it("chooses sample views without confusing missing and null", () => {
  expect(defaultOutputView(undefined)).toBe("schema");
  expect(defaultOutputView(null)).toBe("json");
  expect(defaultOutputView([{ name: "sample" }])).toBe("table");
  expect(defaultOutputView({ name: "sample" })).toBe("schema");
});

it("keeps lexical predecessor visibility inside a branch", () => {
  const definition = {
    kind: "Workflow",
    metadata: { name: "w", version: "1" },
    spec: {
      inputSchema: {},
      steps: [
        { id: "before", kind: "transform", value: { literal: 1 } },
        {
          id: "route",
          kind: "switch",
          cases: [
            {
              when: { literal: true },
              steps: [
                { id: "sibling", kind: "transform", value: { literal: 2 } },
              ],
              output: { literal: {} },
            },
            {
              when: { literal: true },
              steps: [
                { id: "inside", kind: "transform", value: { ref: "/input" } },
              ],
              output: { literal: {} },
            },
          ],
        },
        { id: "after", kind: "transform", value: { literal: 3 } },
      ],
    },
  };
  const scope = referenceScope(definition, "inside", "/value");
  expect(inputSources(scope).map((source) => source.id)).toEqual([
    "step:before",
    "input",
  ]);
});

it("keeps schema properties named after annotation keywords in drag metadata", () => {
  expect(
    dragSchema({
      type: "object",
      properties: {
        default: { type: "string", default: "x" },
        enum: { type: "number" },
      },
    }),
  ).toEqual({
    type: "object",
    properties: { default: { type: "string" }, enum: { type: "number" } },
  });
});
it("redacts secrets declared in any union alternative", () => {
  expect(
    safeSample(
      {
        oneOf: [
          {
            type: "object",
            properties: { token: { type: "string", writeOnly: true } },
          },
          { type: "object", properties: { name: { type: "string" } } },
        ],
      },
      { token: "private", name: "public" },
    ),
  ).toEqual({ name: "public" });
});
it("refuses an invalid date-time default", () => {
  expect(() =>
    sampleFromSchema({
      type: "string",
      format: "date-time",
      default: "yesterday",
    }),
  ).toThrow();
});

it("expands locally referenced schema fields for output preview", () => {
  expect(
    schemaRows({
      $defs: {
        Entry: { type: "object", properties: { amount: { type: "number" } } },
      },
      $ref: "#/$defs/Entry",
    }).map((row) => [row.key, row.typeLabel]),
  ).toEqual([["/amount", "Number"]]);
});

it("counts actual sample items and names scalar types", () => {
  expect(sampleCount([{}, {}])).toBe("2 items");
  expect(sampleCount({ name: "a" })).toBe("1 field");
  expect(sampleCount(null)).toBe("Empty");
  expect(sampleCount(12)).toBe("Number");
});

it("reads an explicit preference independently from the other pane", () => {
  expect(
    readViews({ getItem: () => JSON.stringify({ output: "json" }) }),
  ).toEqual({ input: "schema", output: "json" });
});

describe("sample safety boundaries", () => {
  const unusual = JSON.parse(
    '{"constructor":"private","toString":"private","__proto__":"private"}',
  );
  it("uses additional-property secrecy for inherited names", () => {
    const schema = {
      type: "object",
      additionalProperties: { type: "string", writeOnly: true },
    };
    expect(safeSample(schema, unusual)).toEqual({});
    expect(sampleFromSchema({ ...schema, default: unusual })).toEqual({});
  });
  it("retains explicitly declared unusual own names", () => {
    const properties = Object.fromEntries(
      Object.keys(unusual).map((key) => [key, { type: "string" }]),
    );
    const schema = {
      type: "object",
      properties,
      additionalProperties: false,
      default: unusual,
    };
    expect(safeSample(schema, unusual)).toEqual(unusual);
    expect(sampleFromSchema(schema)).toEqual(unusual);
    expect(Object.hasOwn(sampleFromSchema(schema) as object, "__proto__")).toBe(
      true,
    );
  });
  it("refuses forbidden additional properties even with inherited names", () => {
    for (const key of Object.keys(unusual))
      expect(() =>
        sampleFromSchema({
          type: "object",
          additionalProperties: false,
          default: { [key]: "private" },
        }),
      ).toThrow();
  });
  it("withholds a tuple with a secret prefix without shifting later positions", () => {
    expect(
      safeSample(
        {
          type: "array",
          prefixItems: [{ type: "string", writeOnly: true }],
          items: { type: "string" },
        },
        ["private", "public"],
      ),
    ).toBeUndefined();
    expect(
      safeSample(
        {
          type: "array",
          prefixItems: [{ type: "string" }],
          items: { type: "string" },
        },
        ["first", "second"],
      ),
    ).toEqual(["first", "second"]);
    expect(
      safeSample({ type: "array", items: { writeOnly: true } }, ["private"]),
    ).toBeUndefined();
  });
  it.each(["contains", "unevaluatedItems", "additionalItems"])(
    "withholds unsupported %s item rules",
    (keyword) => {
      expect(
        safeSample({ type: "array", [keyword]: { writeOnly: true } }, [
          "private",
        ]),
      ).toBeUndefined();
    },
  );
  it("withholds legacy positional items and unsafe nested array subtrees", () => {
    expect(
      safeSample({ type: "array", items: [{ writeOnly: true }] }, ["private"]),
    ).toBeUndefined();
    expect(
      safeSample(
        {
          type: "object",
          properties: {
            rows: { type: "array", prefixItems: [{ writeOnly: true }] },
            name: { type: "string" },
          },
        },
        { rows: ["private", "second"], name: "public" },
      ),
    ).toEqual({ name: "public" });
  });
  it.each(["allOf", "oneOf", "anyOf"])(
    "refuses %s through references, including below defaults",
    (keyword) => {
      const defs = {
        Composed: { [keyword]: [{ type: "string" }, { type: "string" }] },
      };
      expect(() =>
        sampleFromSchema({ $defs: defs, $ref: "#/$defs/Composed" }),
      ).toThrow();
      expect(() =>
        sampleFromSchema({
          $defs: defs,
          type: "object",
          properties: { value: { $ref: "#/$defs/Composed" } },
          default: { value: "text" },
        }),
      ).toThrow();
    },
  );
  it("keeps nullable metadata for direct and referenced null defaults", () => {
    const Value = { type: ["string", "null"], default: null };
    expect(sampleFromSchema(Value)).toBeNull();
    expect(
      sampleFromSchema({
        $defs: { Value },
        type: "object",
        properties: { value: { $ref: "#/$defs/Value" } },
      }),
    ).toEqual({ value: null });
    expect(
      sampleFromSchema({
        $defs: { Value },
        type: "object",
        properties: { value: { $ref: "#/$defs/Value" } },
        default: { value: null },
      }),
    ).toEqual({ value: null });
    expect(() =>
      sampleFromSchema({
        type: ["string", "null"],
        enum: ["text"],
        default: null,
      }),
    ).not.toThrow();
  });
});

it("does not treat an enum containing null as permission to violate a string type", () => {
  expect(() =>
    sampleFromSchema({ type: "string", enum: [null, "text"] }),
  ).toThrow();
});
it("refuses structural reference intersections while allowing definitions-only siblings", () => {
  expect(() =>
    sampleFromSchema({
      $defs: {
        Value: {
          type: "object",
          properties: { a: { type: "string" } },
          additionalProperties: false,
        },
      },
      $ref: "#/$defs/Value",
      properties: { b: { type: "string" } },
    }),
  ).toThrow();
});
it("withholds unsupported negated array schemas", () => {
  expect(
    safeSample({ type: "array", not: { items: { writeOnly: true } } }, [
      "private",
    ]),
  ).toBeUndefined();
});
