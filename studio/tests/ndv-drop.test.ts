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
  autoScrollDelta,
  caretAt,
  childEntries,
  dropFit,
  mappedRefs,
  nearestRoot,
  parseDrag,
  planDrop,
  rowKey,
  stickyTarget,
  type DragRef,
} from "../src/app/editor/ndv/params/drop";
import { templateParts } from "../src/app/editor/ndv/params/template-text";
import type { ParamSpec } from "../src/app/editor/ndv/registry";
import type { ScopeEntry } from "../src/app/forms/core/scope";

const amount: DragRef = {
  ref: "/steps/check-customer/output/amount",
  breadcrumb: "check-customer › amount",
  schema: { type: "number" },
};
const email: DragRef = {
  ref: "/input/email",
  breadcrumb: "Input › Email",
  schema: { type: "string" },
};
const field = (
  type: ParamSpec["type"],
  extra: Partial<ParamSpec> = {},
): ParamSpec => ({
  id: "f",
  path: ["with", "f"],
  type,
  label: "Limit",
  mapping: "both",
  ...extra,
});
const entry = (ref: string, path: string[]): ScopeEntry => ({
  ref,
  source: ref.startsWith("/input") ? "input" : "step",
  path,
  label: path.at(-1) ?? ref,
  breadcrumb: ref,
  schema: {},
  types: [],
  typeLabel: "Any value",
  optional: false,
});

describe("drag-to-map", () => {
  it("reads what a drag carries", () => {
    expect(parseDrag(JSON.stringify(email))).toEqual(email);
    expect(parseDrag("nope")).toBeNull();
    expect(parseDrag(JSON.stringify({ ref: "input/x" }))).toBeNull();
  });
  it("says whether a dragged field fits", () => {
    expect(
      dropFit(field("number"), "fixed", { type: "integer" }, email, false),
    ).toEqual({
      fit: "mismatch",
      text: "Text doesn't fit a Whole number field",
    });
    expect(
      dropFit(field("number"), "fixed", { type: "integer" }, amount, false),
    ).toEqual({ fit: "unknown", text: "Type not checked" });
    expect(dropFit(field("number"), "fixed", null, amount, false)).toEqual({
      fit: "fits",
      text: "Fits",
    });
    expect(dropFit(field("text"), "fixed", null, amount, true)).toEqual({
      fit: "fits",
      text: "Fits",
    });
    expect(
      dropFit(
        field("number", { mapping: undefined }),
        null,
        null,
        amount,
        false,
      ),
    ).toEqual({
      fit: "refused",
      text: "This field takes a fixed value.",
    });
    expect(dropFit(field("keyValue"), "fixed", null, amount, false).text).toBe(
      "Drop to add a field",
    );
  });
  it("maps an empty field, and replaces a fixed number", () => {
    const options = { caret: null, templates: false, mode: "fixed" as const };
    expect(
      planDrop(field("number"), { mode: "absent" }, amount, options),
    ).toEqual({
      kind: "write",
      next: { mode: "mapped", expression: { ref: amount.ref } },
      replaced: null,
    });
    expect(
      planDrop(field("number"), { mode: "fixed", value: 25 }, amount, options),
    ).toEqual({
      kind: "write",
      next: { mode: "mapped", expression: { ref: amount.ref } },
      replaced: "25",
    });
    expect(
      planDrop(
        field("number", { mapping: undefined }),
        { mode: "absent" },
        amount,
        { ...options, mode: null },
      ),
    ).toEqual({
      kind: "refuse",
      reason: "This field takes a fixed value.",
    });
  });
  it("keeps text and puts the placeholder at the caret", () => {
    const plan = planDrop(
      field("text"),
      { mode: "fixed", value: "Hello there" },
      email,
      {
        caret: 6,
        templates: true,
        mode: "fixed",
      },
    );
    expect(plan.kind).toBe("write");
    if (plan.kind !== "write" || plan.next.mode !== "mapped")
      throw new Error("expected a mapping");
    expect(templateParts(plan.next.expression)).toEqual([
      { text: "Hello " },
      { ref: "/input/email" },
      { text: "there" },
    ]);
    expect(plan.replaced).toBeNull();
    const end = planDrop(field("text"), plan.next, amount, {
      caret: null,
      templates: true,
      mode: "mapped",
    });
    if (end.kind !== "write" || end.next.mode !== "mapped")
      throw new Error("expected a mapping");
    expect(templateParts(end.next.expression)?.at(-1)).toEqual({
      ref: amount.ref,
    });
  });
  it("replaces an exact reference leaf in a non-template field while preserving opaque formulas", () => {
    const options = { caret: null, templates: false, mode: "mapped" as const };
    expect(
      planDrop(
        field("number"),
        { mode: "mapped", expression: { ref: "/input/amount" } },
        amount,
        options,
      ),
    ).toEqual({
      kind: "write",
      next: { mode: "mapped", expression: { ref: amount.ref } },
      replaced: "the previous mapping",
    });
    for (const expression of [
      { op: { name: "add", args: [{ literal: 1 }, { literal: 2 }] } },
      { ref: "/input/amount", extra: "preserve" },
    ])
      expect(
        planDrop(
          field("number"),
          { mode: "mapped", expression },
          amount,
          options,
        ).kind,
      ).toBe("refuse");
  });
  it("names rows after the dragged field and finds an object's fields", () => {
    expect(rowKey("/input/customerId", new Set())).toBe("customerId");
    expect(rowKey("/input/customerId", new Set(["customerId"]))).toBe(
      "customerId-2",
    );
    expect(rowKey("/steps/check-customer/output", new Set())).toBe(
      "check-customer",
    );
    const entries = [
      entry("/input/customerId", ["customerId"]),
      entry("/input/shipping", ["shipping"]),
      entry("/input/shipping/country", ["shipping", "country"]),
      entry("/steps/check/output/ok", ["ok"]),
    ];
    expect(childEntries(entries, "/input").map((e) => e.ref)).toEqual([
      "/input/customerId",
      "/input/shipping",
    ]);
    expect(nearestRoot(entries)).toBe("/steps/check/output");
    expect(nearestRoot(entries.slice(0, 3))).toBe("/input");
  });
  it("finds the field under the pointer with slack, and scrolls near the edges", () => {
    const outer = {
      item: "outer",
      box: { left: 0, top: 0, right: 400, bottom: 200 },
    };
    const inner = {
      item: "inner",
      box: { left: 20, top: 20, right: 200, bottom: 60 },
    };
    expect(stickyTarget([outer, inner], 30, 30)).toBe("inner");
    expect(stickyTarget([outer, inner], 205, 30)).toBe("inner");
    expect(stickyTarget([outer, inner], 300, 150)).toBe("outer");
    expect(stickyTarget([inner], 300, 150)).toBeNull();
    expect(autoScrollDelta({ top: 100, bottom: 600 }, 110)).toBe(-16);
    expect(autoScrollDelta({ top: 100, bottom: 600 }, 590)).toBe(16);
    expect(autoScrollDelta({ top: 100, bottom: 600 }, 300)).toBe(0);
  });
  it("places a caret from a pointer position", () => {
    const measure = (text: string) => text.length * 10;
    expect(caretAt("Hello", 0, measure)).toBe(0);
    expect(caretAt("Hello", 34, measure)).toBe(3);
    expect(caretAt("Hello", 36, measure)).toBe(4);
    expect(caretAt("Hello", 400, measure)).toBe(5);
  });
  it("never splits an emoji or a combining character when placing the caret", () => {
    const graphemes = new Intl.Segmenter(undefined, {
      granularity: "grapheme",
    });
    const measure = (text: string) => [...graphemes.segment(text)].length * 10;
    expect(caretAt("A🙂B", 17, measure)).toBe(3);
    expect(caretAt("Ae\u0301B", 17, measure)).toBe(3);
    expect(caretAt("A👩‍💻B", 17, measure)).toBe(6);
  });
  it("lists the references a step maps", () => {
    expect(
      mappedRefs({
        object: {
          a: { ref: "/input/a" },
          b: {
            op: {
              name: "concat",
              args: [{ ref: "/input/b" }, { ref: "/input/a" }],
            },
          },
        },
      }),
    ).toEqual(["/input/a", "/input/b"]);
  });
});

describe("mapping transport and preserved values", () => {
  it("bounds untrusted metadata and rejects malformed pointers", () => {
    for (const ref of [
      "/input/a~2b",
      "/input/a~",
      "",
      "/input/" + "x".repeat(4096),
    ])
      expect(parseDrag(JSON.stringify({ ...email, ref }))).toBeNull();
    expect(
      parseDrag(JSON.stringify({ ...email, breadcrumb: "x".repeat(70000) })),
    ).toBeNull();
    expect(
      parseDrag(
        JSON.stringify({
          ...email,
          schema: {
            type: "string",
            default: "SECRET",
            examples: ["SECRET"],
            const: "SECRET",
          },
        }),
      )?.schema,
    ).toEqual({ type: "string" });
    expect(
      parseDrag(JSON.stringify({ ...email, ref: "/input/a~1b~0c" }))?.ref,
    ).toBe("/input/a~1b~0c");
  });
  it("keeps literal braces as text when inserting a reference", () => {
    const result = planDrop(
      field("text"),
      { mode: "fixed", value: "Hi {{literal}}" },
      email,
      { caret: 3, templates: true, mode: "fixed" },
    );
    expect(result).toEqual({
      kind: "write",
      next: {
        mode: "mapped",
        expression: {
          op: {
            name: "concat",
            args: [
              { literal: "Hi " },
              { ref: "/input/email" },
              { literal: "{{literal}}" },
            ],
          },
        },
      },
      replaced: null,
    });
  });
  it("refuses text or formulas that the text editor cannot represent without data loss", () => {
    const options = { caret: null, templates: true, mode: "mapped" as const };
    for (const current of [
      {
        mode: "mapped" as const,
        expression: {
          op: { name: "add", args: [{ literal: 1 }, { literal: 2 }] },
        },
      },
      {
        mode: "mapped" as const,
        expression: {
          op: {
            name: "concat",
            args: [{ literal: "Keep " }, { ref: "/input/a~1b" }],
          },
        },
      },
    ])
      expect(planDrop(field("text"), current, email, options).kind).toBe(
        "refuse",
      );
    expect(
      planDrop(
        field("text"),
        { mode: "fixed", value: "Keep me" },
        { ...email, ref: "/input/a~1b" },
        options,
      ).kind,
    ).toBe("refuse");
    expect(
      planDrop(field("text"), { mode: "fixed", value: "Keep me" }, email, {
        ...options,
        templates: false,
      }).kind,
    ).toBe("refuse");
  });
  it("never reads references from literal payloads or unrelated properties", () => {
    expect(
      mappedRefs({
        object: {
          value: { ref: "/input/real" },
          payload: {
            literal: { ref: "/input/canary", object: { ref: "/input/hidden" } },
          },
        },
      }),
    ).toEqual(["/input/real"]);
    expect(mappedRefs({ title: { ref: "/input/config" } })).toEqual([]);
  });
  it("keeps escaped property names and finds only immediate children", () => {
    const entries = [
      entry("/input/a~1b", ["a/b"]),
      entry("/input/a~1b/x", ["a/b", "x"]),
      entry("/input/a~0b", ["a~b"]),
    ];
    expect(childEntries(entries, "/input").map((e) => e.ref)).toEqual([
      "/input/a~1b",
      "/input/a~0b",
    ]);
    expect(rowKey("/input/a~1b", new Set(["a/b"]))).toBe("a/b-2");
  });
});
