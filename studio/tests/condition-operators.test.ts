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
import { Injector, ElementRef, runInInjectionContext } from "@angular/core";
import {
  buildCondition,
  parseCondition,
  conditionSummary,
} from "../src/app/designer/conditions";
import { ConditionEditor } from "../src/app/designer/condition-editor";

describe("text and list condition operators", () => {
  it.each(["contains", "notContains", "startsWith", "endsWith"] as const)(
    "round-trips %s with an unchanged text operand",
    (operator) => {
      const expression = {
        op: {
          name: operator,
          args: [{ ref: "/input/name" }, { literal: "Ann" }],
        },
      };
      const parsed = parseCondition(expression);
      expect(parsed).toEqual({
        join: "and",
        rows: [{ ref: "/input/name", operator, value: "Ann" }],
      });
      expect(buildCondition(parsed!)).toEqual(expression);
    },
  );
  it.each(["in", "notIn"] as const)(
    "round-trips %s with a typed literal list",
    (operator) => {
      const expression = {
        op: {
          name: operator,
          args: [{ ref: "/input/amount" }, { literal: [1, 2.5] }],
        },
      };
      const parsed = parseCondition(expression);
      expect(parsed).toEqual({
        join: "and",
        rows: [{ ref: "/input/amount", operator, value: [1, 2.5] }],
      });
      expect(buildCondition(parsed!)).toEqual(expression);
      expect(conditionSummary(expression)).toContain("1, 2.5");
    },
  );
  it("uses a list's item schema for contains and keeps unrelated schema operators out", () => {
    const editor = runInInjectionContext(
      Injector.create({
        providers: [
          {
            provide: ElementRef,
            useValue: { nativeElement: { querySelectorAll: () => [] } },
          },
        ],
      }),
      () => new ConditionEditor(),
    );
    editor.references = [
      {
        ref: "/input/amount",
        label: "Amount",
        source: "input",
        breadcrumb: "Input",
        typeLabel: "",
        optional: false,
        schema: { type: "number" },
      },
      {
        ref: "/input/names",
        label: "Names",
        source: "input",
        breadcrumb: "Input",
        typeLabel: "",
        optional: false,
        schema: { type: "array", items: { type: "integer" } },
      },
      {
        ref: "/input/region",
        label: "Region",
        source: "input",
        breadcrumb: "Input",
        typeLabel: "",
        optional: false,
        schema: { type: "string", enum: ["eu", "us"] },
      },
    ];
    expect(
      editor.inputFor({ ref: "/input/names", operator: "contains", text: "" }),
    ).toEqual({ kind: "number", integer: true });
    expect(
      editor
        .operatorsFor({ ref: "/input/amount", operator: "eq", text: "" })
        .map((item) => item.value),
    ).not.toContain("contains");
    expect(
      editor
        .operatorsFor({ ref: "/input/region", operator: "eq", text: "" })
        .map((item) => item.value),
    ).not.toContain("gt");
  });
  it("keeps malformed membership operands and object comparisons in the formula editor", () => {
    expect(
      parseCondition({
        op: { name: "in", args: [{ ref: "/input/a" }, { literal: "wrong" }] },
      }),
    ).toBeNull();
    expect(
      parseCondition({
        op: {
          name: "contains",
          args: [{ ref: "/input/a" }, { literal: { nested: true } }],
        },
      }),
    ).toBeNull();
  });
});

it("editing a sibling does not coerce an untouched mixed membership list", () => {
  const editor = runInInjectionContext(
    Injector.create({
      providers: [
        {
          provide: ElementRef,
          useValue: { nativeElement: { querySelectorAll: () => [] } },
        },
      ],
    }),
    () => new ConditionEditor(),
  );
  editor.value = {
    op: {
      name: "and",
      args: [
        {
          op: {
            name: "in",
            args: [{ ref: "/input/free" }, { literal: [1, true, null] }],
          },
        },
        {
          op: {
            name: "eq",
            args: [{ ref: "/input/name" }, { literal: "before" }],
          },
        },
      ],
    },
  };
  const emitted: unknown[] = [];
  editor.valueChange.subscribe((value) => emitted.push(value));
  editor.ngOnChanges();
  editor.setText(1, { target: { value: "after" } } as unknown as Event);
  expect(emitted.at(-1)).toEqual({
    op: {
      name: "and",
      args: [
        {
          op: {
            name: "in",
            args: [{ ref: "/input/free" }, { literal: [1, true, null] }],
          },
        },
        {
          op: {
            name: "eq",
            args: [{ ref: "/input/name" }, { literal: "after" }],
          },
        },
      ],
    },
  });
});

it("keeps stored empty text comparisons complete", () => {
  const expression = {
    op: { name: "contains", args: [{ ref: "/input/name" }, { literal: "" }] },
  };
  expect(buildCondition(parseCondition(expression)!)).toEqual(expression);
});

it("does not coerce a stored operand when choosing its current reference", () => {
  const editor = runInInjectionContext(
    Injector.create({
      providers: [
        {
          provide: ElementRef,
          useValue: { nativeElement: { querySelectorAll: () => [] } },
        },
      ],
    }),
    () => new ConditionEditor(),
  );
  editor.value = {
    op: { name: "contains", args: [{ ref: "/input/free" }, { literal: 1 }] },
  };
  const emitted: unknown[] = [];
  editor.valueChange.subscribe((value) => emitted.push(value));
  editor.ngOnChanges();
  editor.setRef(0, "/input/free");
  expect(emitted).toEqual([]);
});

it("uses nullable list item types and offers only presence for object data", () => {
  const editor = runInInjectionContext(
    Injector.create({
      providers: [
        {
          provide: ElementRef,
          useValue: { nativeElement: { querySelectorAll: () => [] } },
        },
      ],
    }),
    () => new ConditionEditor(),
  );
  editor.references = [
    {
      ref: "/input/list",
      label: "List",
      source: "input",
      breadcrumb: "Input",
      typeLabel: "",
      optional: false,
      schema: { type: ["array", "null"], items: { type: "integer" } },
    },
    {
      ref: "/input/object",
      label: "Object",
      source: "input",
      breadcrumb: "Input",
      typeLabel: "",
      optional: false,
      schema: { type: "object" },
    },
  ];
  expect(
    editor.inputFor({ ref: "/input/list", operator: "contains", text: "" }),
  ).toEqual({ kind: "number", integer: true });
  expect(
    editor
      .operatorsFor({ ref: "/input/object", operator: "eq", text: "" })
      .map((item) => item.value),
  ).toEqual(["exists", "missing"]);
});
