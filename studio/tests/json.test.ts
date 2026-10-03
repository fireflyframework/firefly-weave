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
  canonicalJson,
  escapeSegment,
  formatPointer,
  getAt,
  hasOwn,
  isJsonObject,
  isPointer,
  jsonEqual,
  parseJsonText,
  parsePointer,
  removeAt,
  setAt,
  unescapeSegment,
} from "../src/app/forms/core/json";

describe("JSON pointers (RFC 6901)", () => {
  it("escapes and unescapes ~ and / in that order", () => {
    expect(escapeSegment("a/b~c")).toBe("a~1b~0c");
    expect(unescapeSegment("a~1b~0c")).toBe("a/b~c");
    // "~01" is "~1" literally, never "/".
    expect(unescapeSegment("~01")).toBe("~1");
  });

  it("parses and formats pointers, including the whole-document pointer", () => {
    expect(parsePointer("")).toEqual([]);
    expect(parsePointer("/")).toEqual([""]);
    expect(parsePointer("/steps/check/output/a~1b")).toEqual([
      "steps",
      "check",
      "output",
      "a/b",
    ]);
    expect(formatPointer(["steps", "a/b", "~x"])).toBe("/steps/a~1b/~0x");
    expect(formatPointer([])).toBe("");
    expect(formatPointer(["items", 0])).toBe("/items/0");
  });

  it("rejects text that is not a pointer", () => {
    for (const bad of ["input", "/a~2", "/a~", "#/a", " /a"])
      expect(parsePointer(bad), bad).toBeNull();
    expect(isPointer("/input/customerId")).toBe(true);
    expect(isPointer("/a~2")).toBe(false);
  });
});

describe("reading and writing JSON trees", () => {
  const doc = {
    customer: { id: "c-1", tags: ["a", "b"] },
    empty: {},
  };

  it("reads own properties and array indexes only", () => {
    expect(getAt(doc, ["customer", "tags", "1"])).toBe("b");
    expect(getAt(doc, ["customer", "tags", 1])).toBe("b");
    expect(getAt(doc, ["customer", "missing"])).toBeUndefined();
    expect(getAt(doc, ["customer", "tags", "01"])).toBeUndefined();
    expect(getAt(doc, ["customer", "tags", "-1"])).toBeUndefined();
    // Inherited members are not data.
    expect(getAt(doc, ["customer", "toString"])).toBeUndefined();
    expect(getAt(doc, ["__proto__"])).toBeUndefined();
  });

  it("writes immutably with structural sharing", () => {
    const next = setAt(doc, ["customer", "id"], "c-2");
    expect(next).not.toBe(doc);
    expect(doc.customer.id).toBe("c-1");
    expect((next as typeof doc).customer.id).toBe("c-2");
    expect((next as typeof doc).customer.tags).toBe(doc.customer.tags);
    expect((next as typeof doc).empty).toBe(doc.empty);
  });

  it("creates missing parents and appends to arrays", () => {
    expect(setAt({}, ["a", "b"], 1)).toEqual({ a: { b: 1 } });
    expect(setAt({ list: [1] }, ["list", "-"], 2)).toEqual({ list: [1, 2] });
    expect(setAt({ list: [1] }, ["list", 1], 2)).toEqual({ list: [1, 2] });
    expect(setAt(undefined, [], 5)).toBe(5);
  });

  it("removing a value prunes the objects it leaves empty, but never the root", () => {
    const value = { group: { inner: { leaf: 1 } }, keep: 1 };
    expect(removeAt(value, ["group", "inner", "leaf"])).toEqual({ keep: 1 });
    expect(removeAt({ only: { leaf: 1 } }, ["only", "leaf"])).toEqual({});
    expect(removeAt({ list: [1, 2, 3] }, ["list", 1])).toEqual({
      list: [1, 3],
    });
    // Unknown paths leave the value untouched (same reference).
    expect(removeAt(value, ["nope", "x"])).toBe(value);
  });

  it("treats __proto__ and constructor as ordinary keys without touching prototypes", () => {
    const polluted = setAt({}, ["__proto__", "polluted"], true);
    expect(({} as Record<string, unknown>)["polluted"]).toBeUndefined();
    expect(hasOwn(polluted as object, "__proto__")).toBe(true);
    expect(getAt(polluted, ["__proto__", "polluted"])).toBe(true);
    expect(Object.getPrototypeOf(polluted)).toBe(Object.prototype);
    const constructor = setAt({}, ["constructor", "prototype", "x"], 1);
    expect(getAt(constructor, ["constructor", "prototype", "x"])).toBe(1);
    expect(({} as Record<string, unknown>)["x"]).toBeUndefined();
  });
});

describe("JSON equality and parsing", () => {
  it("compares by value regardless of key order", () => {
    expect(jsonEqual({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1 }));
    expect(jsonEqual({ a: 1 }, { a: 1, b: undefined })).toBe(true);
    expect(jsonEqual([1, 2], [2, 1])).toBe(false);
    expect(jsonEqual(1, 1.0)).toBe(true);
    expect(canonicalJson({ b: 1, a: [true, null] })).toBe(
      '{"a":[true,null],"b":1}',
    );
    expect(isJsonObject({})).toBe(true);
    expect(isJsonObject([])).toBe(false);
    expect(isJsonObject(null)).toBe(false);
  });

  it("explains parse failures in plain words with a line and column", () => {
    expect(parseJsonText('{"a": 1}')).toEqual({ ok: true, value: { a: 1 } });
    const empty = parseJsonText("   ");
    expect(empty.ok).toBe(false);
    if (!empty.ok) expect(empty.message).toBe("Paste a JSON value.");
    const broken = parseJsonText('{\n  "a": 1,\n  "b": \n}');
    expect(broken.ok).toBe(false);
    if (!broken.ok) {
      expect(broken.message).toMatch(/^This is not valid JSON/);
      expect(broken.message).not.toMatch(/SyntaxError|Unexpected token/);
      expect(broken.line).toBe(4);
    }
  });
});
