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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  parseFormula,
  parseTemplate,
  templateExpression,
  type TemplatePart,
} from "../src/app/editor/formula/grammar";
import {
  escapeTemplateText,
  printTemplate,
  templateSegments,
} from "../src/app/editor/formula/template";

const options = { features: ["text.concat", "text.join"], loops: [] };
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const ref = (pointer: string) => ({ ref: pointer });
const lit = (value: unknown) => ({ literal: value });
const back = (text: string) => {
  const result = parseTemplate(text, options);
  if (result.kind !== "ok")
    throw new Error(`${text}: ${JSON.stringify(result)}`);
  return result.expression;
};
function random(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const PIECES = ["a", " ", "\\", "{", "}", "`", "{{", "}}", "ü", "😀", "\n"];
const text = (next: () => number) =>
  Array.from(
    { length: Math.floor(next() * 8) },
    () => PIECES[Math.floor(next() * PIECES.length)],
  ).join("");
const PLACEHOLDERS = [
  ref("/input/name"),
  ref("/steps/lookup.v2/output"),
  lit(42),
  lit(true),
  op("join", ref("/input/tags"), lit(", ")),
  op("coalesce", ref("/input/nickname"), lit("}}")),
  lit({ a: { b: 1 } }),
];

describe("text templates", () => {
  it("follows the platform's template segments and saved forms", () => {
    const { cases } = JSON.parse(
      readFileSync(
        resolve(
          import.meta.dirname,
          "fixtures/language/template-segments.json",
        ),
        "utf8",
      ),
    ) as {
      cases: {
        name: string;
        expression: unknown;
        segments:
          | (
              | { kind: "text"; text: string }
              | { kind: "placeholder"; expression: unknown }
            )[]
          | null;
        saved: unknown;
      }[];
    };
    for (const item of cases) {
      const parts = templateSegments(item.expression);
      expect(
        parts?.map((part) =>
          "text" in part
            ? { kind: "text", text: part.text }
            : { kind: "placeholder", expression: part.node },
        ) ?? null,
        item.name,
      ).toEqual(item.segments);
      if (parts)
        expect(templateExpression(parts), item.name).toEqual(item.saved);
    }
  });

  it("prints templates with placeholders in formula text", () => {
    expect(
      printTemplate(
        op(
          "concat",
          lit("Hello "),
          ref("/input/name"),
          lit(", you owe "),
          ref("/input/amount"),
        ),
      ),
    ).toBe("Hello {{ input.name }}, you owe {{ input.amount }}");
    expect(printTemplate(lit("Keep {{ this }}"))).toBe("Keep \\{{ this }}");
    expect(printTemplate(ref("/input/name"))).toBe("{{ input.name }}");
    expect(printTemplate(op("concat", lit(42)))).toBe("{{ 42 }}");
    expect(
      printTemplate(
        op("concat", lit("Tags: "), op("join", ref("/input/tags"), lit(", "))),
      ),
    ).toBe('Tags: {{ join(input.tags, ", ") }}');
    expect(
      printTemplate(
        op("concat", lit("Order "), ref("/steps/lookup.v2/output")),
      ),
    ).toBe('Order {{ steps["lookup.v2"].output }}');
    expect(printTemplate(op("eq", lit(1), lit(1)))).toBeNull();
    expect(printTemplate(lit(42))).toBeNull();
  });

  it("round-trips template text with backslashes, braces and backticks", () => {
    const next = random(11);
    for (let i = 0; i < 2000; i++) {
      const parts: TemplatePart[] = [];
      for (let j = Math.floor(next() * 4); j >= 0; j--)
        parts.push(
          next() < 0.5
            ? { text: text(next) }
            : { node: PLACEHOLDERS[Math.floor(next() * PLACEHOLDERS.length)] },
        );
      const expression = templateExpression(parts, true);
      const printed = printTemplate(expression);
      expect(printed, JSON.stringify(expression)).not.toBeNull();
      expect(back(printed!), printed!).toEqual(expression);
    }
  });

  it("escapes text inside a backtick template", () => {
    const next = random(12);
    for (let i = 0; i < 500; i++) {
      const value = text(next);
      const alone = parseFormula(
        "`" + escapeTemplateText(value, "end", true) + "`",
        options,
      );
      expect(alone, value).toMatchObject({
        kind: "ok",
        expression: lit(value),
      });
      const before = parseFormula(
        "`" + escapeTemplateText(value, "placeholder", true) + "{{ input.a }}`",
        options,
      );
      expect(before, value).toMatchObject({
        kind: "ok",
        expression: op(
          "concat",
          ...(value ? [lit(value)] : []),
          ref("/input/a"),
        ),
      });
    }
  });
});
