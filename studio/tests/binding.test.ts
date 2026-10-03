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
import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import {
  Bound,
  bindingMode,
  decode,
  encode,
  formula,
  get,
  literalValue,
  reference,
  remove,
  set,
  value,
} from "../src/app/forms/core/binding";
import { StructuredCanvasAdapter, Step } from "../src/app/model";

const examples = resolve(import.meta.dirname, "../../examples");
const read = (path: string) => readFileSync(resolve(examples, path), "utf8");
const checkCustomer = parse(read("definitions/check-customer.action.yaml"));
// A readable view for assertions.
function compact(bound: Bound): unknown {
  if (bound.kind === "value") return bound.value;
  if (bound.kind === "ref") return { ref: bound.pointer };
  if (bound.kind === "formula") return { formula: bound.expression };
  if (bound.kind === "array") return bound.items.map(compact);
  return Object.fromEntries(
    Object.entries(bound.entries).map(([k, v]) => [k, compact(v)]),
  );
}
// The bound tree without provenance, for structural equivalence.
function strip(bound: Bound): unknown {
  const { source: _source, ...rest } = bound as Bound & { source?: unknown };
  if (rest.kind === "object")
    return {
      ...rest,
      entries: Object.fromEntries(
        Object.entries(rest.entries).map(([k, v]) => [k, strip(v)]),
      ),
    };
  if (rest.kind === "array") return { ...rest, items: rest.items.map(strip) };
  return rest;
}

describe("decoding expressions into bound fields", () => {
  it("descends literal, object and array; references and formulas are leaves", () => {
    const op = { op: { name: "not", args: [{ ref: "/input/flag" }] } };
    const expression = {
      object: {
        id: { ref: "/input/customerId" },
        limits: { literal: { max: 3, tags: ["a", "b"] } },
        list: { array: [{ literal: 1 }, { ref: "/steps/a/output" }] },
        active: op,
      },
    };
    const bound = decode(expression);
    expect(compact(bound)).toEqual({
      id: { ref: "/input/customerId" },
      limits: { max: 3, tags: ["a", "b"] },
      list: [1, { ref: "/steps/a/output" }],
      active: { formula: op },
    });
    expect(bindingMode(get(bound, ["id"])!)).toBe("data");
    expect(bindingMode(get(bound, ["limits", "max"])!)).toBe("value");
    expect(bindingMode(get(bound, ["limits"])!)).toBe("fields");
    expect(bindingMode(get(bound, ["active"])!)).toBe("formula");
    expect(get(bound, ["list", 1])).toMatchObject({
      kind: "ref",
      pointer: "/steps/a/output",
    });
  });
  it("a shape that does not fit the schema becomes a formula on that node only", () => {
    const expression = {
      object: {
        name: { object: { first: { literal: "Ada" } } },
        count: { array: [] },
        note: { literal: { free: "form" } },
        ok: { literal: true },
      },
    };
    const schema = {
      type: "object",
      properties: {
        name: { type: "string" },
        count: { type: "integer" },
        note: { type: "string" },
        ok: { type: "boolean" },
      },
    };
    const bound = decode(expression, schema);
    expect(get(bound, ["name"])).toMatchObject({ kind: "formula" });
    expect(get(bound, ["count"])).toMatchObject({ kind: "formula" });
    // A mismatched literal stays a value; the compiler reports the type.
    expect(get(bound, ["note"])).toMatchObject({
      kind: "value",
      value: { free: "form" },
    });
    expect(get(bound, ["ok"])).toMatchObject({ kind: "value", value: true });
    expect(encode(bound)).toBe(expression);
  });
  it("keeps malformed or unknown expressions intact as formulas", () => {
    for (const odd of [{}, { literal: 1, ref: "/x" }, { weird: 1 }, 7, null])
      expect(encode(decode(odd))).toBe(odd);
    expect(decode({ object: [1] })).toMatchObject({ kind: "formula" });
  });
  it("follows local $ref and additionalProperties for child schemas", () => {
    const schema = {
      type: "object",
      properties: { address: { $ref: "#/$defs/address" } },
      additionalProperties: { type: "string" },
      $defs: {
        address: { type: "object", properties: { city: { type: "string" } } },
      },
    };
    const bound = decode(
      {
        object: {
          address: { object: { city: { array: [] } } },
          extra: { object: {} },
        },
      },
      schema,
    );
    expect(get(bound, ["address", "city"])).toMatchObject({ kind: "formula" });
    expect(get(bound, ["extra"])).toMatchObject({ kind: "formula" });
  });
});

describe("shape-preserving encoding", () => {
  it("untouched input returns the same object", () => {
    const expression = {
      object: { a: { literal: 1 }, b: { ref: "/input/b" } },
    };
    const bound = decode(expression);
    expect(encode(bound)).toBe(expression);
    expect(set(bound, ["a"], get(bound, ["a"])!)).toBe(bound);
    // Writing an equal value keeps the original node and its source.
    expect(encode(set(bound, ["a"], value(1)))).toBe(expression);
    expect(encode(set(bound, ["b"], reference("/input/b")))).toBe(expression);
  });
  it("re-encodes only the edited path; untouched siblings keep their objects", () => {
    const keep = { ref: "/input/reference_id" };
    const formulaNode = { op: { name: "exists", args: [{ ref: "/input/x" }] } };
    const expression = {
      object: {
        reference_id: keep,
        flag: formulaNode,
        text: { literal: "Message received." },
      },
    };
    const next = encode(set(decode(expression), ["text"], value("Thanks!")));
    expect(next).toEqual({
      object: {
        reference_id: keep,
        flag: formulaNode,
        text: { literal: "Thanks!" },
      },
    });
    const object = (next as { object: Record<string, unknown> }).object;
    expect(object["reference_id"]).toBe(keep);
    expect(object["flag"]).toBe(formulaNode);
  });
  it("an edited all-literal group collapses to one literal; a mixed group stays an object", () => {
    const expression = { object: { customerId: { ref: "/input/customerId" } } };
    const bound = decode(expression);
    expect(encode(set(bound, ["customerId"], value("abc")))).toEqual({
      literal: { customerId: "abc" },
    });
    const literal = decode({ literal: { a: 1, b: { c: 2 } } });
    expect(encode(set(literal, ["b", "c"], reference("/input/c")))).toEqual({
      object: { a: { literal: 1 }, b: { object: { c: { ref: "/input/c" } } } },
    });
    expect(encode(set(literal, ["b", "c"], value(5)))).toEqual({
      literal: { a: 1, b: { c: 5 } },
    });
    // An untouched object-form child is never rewritten into a literal.
    const objectChild = { object: { x: { literal: 1 } } };
    const parent = decode({ object: { kept: objectChild, y: { literal: 2 } } });
    const encoded = encode(set(parent, ["y"], value(3))) as {
      object: Record<string, unknown>;
    };
    expect(encoded.object["kept"]).toBe(objectChild);
  });
  it("adds, removes and appends fields", () => {
    const bound = decode({ literal: {} });
    const added = set(
      bound,
      ["parameters", "customerId"],
      reference("/input/id"),
    );
    expect(encode(added)).toEqual({
      object: {
        parameters: { object: { customerId: { ref: "/input/id" } } },
      },
    });
    expect(encode(remove(added, ["parameters"]))).toEqual({ literal: {} });
    expect(remove(added, ["missing"])).toBe(added);
    const list = set(decode({ literal: ["a"] }), [1], value("b"));
    expect(encode(list)).toEqual({ literal: ["a", "b"] });
    expect(encode(remove(list, [0]))).toEqual({ literal: ["b"] });
    expect(() => set(list, [5], value("x"))).toThrow(RangeError);
    expect(() => set(decode({ ref: "/input/x" }), ["inner"], value(1))).toThrow(
      /single value/,
    );
    expect(() => value(undefined)).toThrow();
  });
  it("reports literal values for previews", () => {
    expect(literalValue(decode({ literal: { a: [1, { b: null }] } }))).toEqual({
      a: [1, { b: null }],
    });
    expect(
      literalValue(decode({ object: { a: { ref: "/input/a" } } })),
    ).toBeUndefined();
    expect(literalValue(decode({ object: { a: { literal: 2 } } }))).toEqual({
      a: 2,
    });
  });
});

// A small deterministic generator for the round-trip property.
function generator(seed: number) {
  let state = seed >>> 0;
  const next = () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
  const pick = <T>(items: T[]) => items[Math.floor(next() * items.length)];
  const scalar = () =>
    pick<unknown>([null, true, false, 0, 7, -2.5, "", "text", "a/b~c"]);
  const json = (depth: number): unknown => {
    if (depth <= 0 || next() < 0.4) return scalar();
    if (next() < 0.5)
      return Array.from({ length: Math.floor(next() * 3) }, () =>
        json(depth - 1),
      );
    return Object.fromEntries(
      Array.from({ length: Math.floor(next() * 3) }, (_, i) => [
        pick(["a", "b", "c~", "d/e"]) + i,
        json(depth - 1),
      ]),
    );
  };
  const expression = (depth: number): unknown => {
    const roll = next();
    if (depth <= 0 || roll < 0.3) return { literal: json(2) };
    if (roll < 0.45) return { ref: pick(["/input/a", "/steps/x/output"]) };
    if (roll < 0.55)
      return {
        op: { name: "eq", args: [{ ref: "/input/a" }, { literal: 1 }] },
      };
    if (roll < 0.8)
      return {
        object: Object.fromEntries(
          Array.from({ length: Math.floor(next() * 4) }, (_, i) => [
            `k${i}`,
            expression(depth - 1),
          ]),
        ),
      };
    return {
      array: Array.from({ length: Math.floor(next() * 3) }, () =>
        expression(depth - 1),
      ),
    };
  };
  const paths = (bound: Bound, prefix: (string | number)[] = []) => {
    const out: (string | number)[][] = [prefix];
    if (bound.kind === "object")
      for (const [key, child] of Object.entries(bound.entries))
        out.push(...paths(child, [...prefix, key]));
    if (bound.kind === "array")
      bound.items.forEach((child, i) =>
        out.push(...paths(child, [...prefix, i])),
      );
    return out;
  };
  const leaf = (): Bound =>
    pick([
      () => value(json(2)),
      () => reference("/input/" + pick(["a", "b"])),
      () => formula({ op: { name: "not", args: [{ literal: true }] } }),
    ])();
  return { next, pick, expression, paths, leaf };
}

describe("round-trip properties", () => {
  it("encode(decode(e)) returns e itself for 500 generated expressions", () => {
    const g = generator(7);
    for (let i = 0; i < 500; i++) {
      const expression = g.expression(4);
      expect(encode(decode(expression))).toBe(expression);
    }
  });
  it("decode(encode(x)) is equivalent to x after random edits", () => {
    const g = generator(11);
    for (let i = 0; i < 500; i++) {
      let bound = decode(g.expression(4));
      for (let edit = 0; edit < 3; edit++) {
        const path = g.pick(g.paths(bound));
        if (path.length && g.next() < 0.25) bound = remove(bound, path);
        else if (path.length) bound = set(bound, path, g.leaf());
      }
      const encoded = encode(bound);
      expect(strip(decode(encoded))).toEqual(strip(bound));
      expect(encode(decode(encoded))).toBe(encoded);
    }
  });
});

describe("canonical examples stay byte-identical when unedited", () => {
  const actionSteps = (steps: Step[]): Step[] =>
    steps.flatMap((step) => [
      ...(step.kind === "action" ? [step] : []),
      ...actionSteps(
        [
          ...((step["cases"] as { steps: Step[] }[]) ?? []),
          ...(step["default"] ? [step["default"] as { steps: Step[] }] : []),
          ...Object.values(
            (step["branches"] as Record<string, { steps: Step[] }>) ?? {},
          ),
        ].flatMap((b) => b.steps),
      ),
    ]);
  const contracts: Record<string, unknown> = {
    "onboarding.check-customer@1.0.0": checkCustomer.spec.inputSchema,
  };
  for (const file of [
    "definitions/customer-onboarding.workflow.yaml",
    "teams/reply.workflow.yaml",
  ])
    it(file, () => {
      const text = read(file);
      const model = new StructuredCanvasAdapter();
      model.setSource(text, "yaml");
      expect(model.error).toBe("");
      const steps = actionSteps(model.definition.spec.steps);
      expect(steps.length).toBeGreaterThan(0);
      for (const step of steps) {
        const original = step["with"];
        const encoded = encode(
          decode(original, contracts[step["uses"] as string]),
        );
        expect(encoded).toBe(original);
        // The editor writes only when the encoded input changed.
        if (encoded !== original)
          model.update(step.id, JSON.stringify({ ...step, with: encoded }));
      }
      expect(model.source).toBe(text);
    });
  it("editing one field changes only that key", () => {
    const text = read("definitions/customer-onboarding.workflow.yaml");
    const model = new StructuredCanvasAdapter();
    model.setSource(text, "yaml");
    const check = model.definition.spec.steps[0];
    const bound = decode(check["with"], checkCustomer.spec.inputSchema);
    const edited = encode(set(bound, ["customerId"], reference("/input/id")));
    model.update(check.id, JSON.stringify({ ...check, with: edited }));
    const before = parse(text);
    const after = parse(model.source);
    expect(after.spec.steps[0].with).toEqual({
      object: { customerId: { ref: "/input/id" } },
    });
    after.spec.steps[0].with = before.spec.steps[0].with;
    expect(after).toEqual(before);
    expect(model.source.startsWith(text.split("apiVersion")[0])).toBe(true);
  });
});

// Encoded inputs must satisfy the platform's Expression contract.
const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
describe.skipIf(!existsSync(python))(
  "encoded expressions against the Python contract",
  () => {
    it("every edited and re-encoded expression validates as an Expression", () => {
      const g = generator(23);
      const encoded: unknown[] = [];
      for (let i = 0; i < 300; i++) {
        let bound = decode(g.expression(4));
        for (let edit = 0; edit < 3; edit++) {
          const path = g.pick(g.paths(bound));
          bound = path.length ? set(bound, path, g.leaf()) : bound;
        }
        encoded.push(encode(bound));
      }
      const failures = JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            `
import json, sys
from pydantic import TypeAdapter, ValidationError
from firefly_weave.contracts.definitions import Expression
adapter = TypeAdapter(Expression)
failures = []
for index, item in enumerate(json.load(sys.stdin)):
    try:
        adapter.validate_python(item)
    except ValidationError as error:
        failures.append({"index": index, "error": str(error)[:300]})
print(json.dumps(failures))
`,
          ],
          {
            cwd: root,
            encoding: "utf8",
            timeout: 180_000,
            input: JSON.stringify(encoded),
            env: {
              ...process.env,
              PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
                .filter(Boolean)
                .join(delimiter),
            },
          },
        ),
      );
      expect(failures).toEqual([]);
    });
  },
);
