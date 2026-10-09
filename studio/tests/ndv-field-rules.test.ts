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
  fieldRules,
  requiredMessage,
  shownProblems,
} from "../src/app/editor/ndv/params/field-rules";
import { ABSENT, fixed, mapped } from "../src/app/editor/ndv/params/value-io";
import type { ParamSpec } from "../src/app/editor/ndv/registry";

const spec = (extra: Partial<ParamSpec>): ParamSpec => ({
  id: "f",
  path: ["f"],
  type: "text",
  label: "Field",
  ...extra,
});
const none = { kind: "absent" } as const;

describe("field rules", () => {
  it("asks for a required value in words that fit the field", () => {
    expect(requiredMessage(spec({ type: "text" }))).toBe("Enter a value.");
    expect(requiredMessage(spec({ type: "text", mapping: "both" }))).toBe(
      "Enter a value or map one.",
    );
    expect(requiredMessage(spec({ type: "select" }))).toBe("Choose one.");
    expect(requiredMessage(spec({ type: "connection" }))).toBe(
      "Choose a connection.",
    );
    expect(requiredMessage(spec({ type: "list" }))).toBe("Add at least one.");
    expect(requiredMessage(spec({ type: "formula" }))).toBe("Map a value.");
    expect(requiredMessage(spec({ type: "conditions" }))).toBe(
      "Choose when this path applies.",
    );
    expect(fieldRules(spec({ required: true }), ABSENT, none)).toEqual([
      { code: "required", message: "Enter a value.", severity: "error" },
    ]);
    expect(fieldRules(spec({ required: true }), fixed(""), none)).toHaveLength(
      1,
    );
    expect(
      fieldRules(spec({ required: true }), mapped({ ref: "/input/a" }), none),
    ).toEqual([]);
    expect(
      fieldRules(
        spec({ type: "list", required: true, default: ["approve", "reject"] }),
        ABSENT,
        none,
      ),
    ).toEqual([]);
  });
  it("keeps numbers outside their range and flags them", () => {
    expect(
      fieldRules(spec({ type: "number", min: 1, max: 100 }), fixed(250), none),
    ).toEqual([
      {
        code: "range",
        message: "Use a number from 1 to 100.",
        severity: "error",
      },
    ]);
    expect(
      fieldRules(spec({ type: "number", min: 1 }), fixed(0), none)[0].message,
    ).toBe("Use 1 or more.");
    expect(
      fieldRules(spec({ type: "number", max: 9 }), fixed(10), none)[0].message,
    ).toBe("Use 9 or less.");
    expect(
      fieldRules(spec({ type: "duration" }), fixed(0), none)[0].message,
    ).toBe("Enter a whole number greater than 0.");
  });
  it("checks text length and list sizes", () => {
    expect(
      fieldRules(spec({ maxLength: 3 }), fixed("abcd"), none)[0].message,
    ).toBe("Use at most 3 characters.");
    expect(
      fieldRules(spec({ type: "list", minItems: 1, maxItems: 2 }), fixed([]), {
        kind: "array",
        length: 0,
      })[0].message,
    ).toBe("Add at least 1.");
    expect(
      fieldRules(spec({ type: "list", maxItems: 2 }), fixed([1, 2, 3]), {
        kind: "array",
        length: 3,
      })[0].message,
    ).toBe("Use at most 2.");
  });
  it("hides required messages on a fresh step until the field is left or everything is revealed", () => {
    const problems = [
      { code: "required", message: "Enter a value." },
      { code: "range", message: "Use 1 or more." },
    ];
    expect(
      shownProblems("f", problems, {
        fresh: true,
        touched: new Set(),
        revealAll: false,
      }),
    ).toEqual([problems[1]]);
    expect(
      shownProblems("f", problems, {
        fresh: true,
        touched: new Set(["f"]),
        revealAll: false,
      }),
    ).toEqual(problems);
    expect(
      shownProblems("f", problems, {
        fresh: true,
        touched: new Set(),
        revealAll: true,
      }),
    ).toEqual(problems);
    expect(
      shownProblems("f", problems, {
        fresh: false,
        touched: new Set(),
        revealAll: false,
      }),
    ).toEqual(problems);
  });
});
