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
// A result a person types is kept in the audit log: what is sent says exactly
// what was typed, or nothing is sent.
import { describe, expect, it } from "vitest";
import { typedJson } from "../src/app/operate/incidents/typed-json";

const NOT_JSON = "Enter the result as JSON.";
const NUMBER =
  "A number in the result can't be sent exactly as typed. Use a shorter or smaller one, or text in quotes if the result takes text.";
const TWICE =
  "An object in the result has the same key twice. Keep one of them.";
const typed = (text: string) => typedJson(text, NOT_JSON);

describe("a result typed as JSON", () => {
  it.each([
    ["null", null],
    [' {"quantity": 40} ', { quantity: 40 }],
    // Written another way, the same number.
    [
      '{"price": 1.10, "rate": 1E2, "none": -0}',
      { price: 1.1, rate: 100, none: -0 },
    ],
    ["9007199254740991", 9007199254740991],
    ["123456789012345680000", 123456789012345680000],
    ["[0.1, 5e-324, -2.50e+3]", [0.1, 5e-324, -2500]],
    ["[0.0000001, -0.00000012, 0.0]", [1e-7, -1.2e-7, 0]],
    // Text, whatever it looks like.
    ['"12345678901234567890"', "12345678901234567890"],
    ['["a\\"1e999", "{\\"a\\":1,\\"a\\":2}"]', ['a"1e999', '{"a":1,"a":2}']],
    // The same key in other objects, or as a value.
    [
      '{"a": {"x": 1, "b": [{"x": 2}, {"x": 3}]}, "b": "a", "x": "x"}',
      { a: { x: 1, b: [{ x: 2 }, { x: 3 }] }, b: "a", x: "x" },
    ],
  ])("is kept: %s", (text, value) => {
    expect(typed(text)).toEqual({ value });
  });

  it.each([
    "12345678901234567890",
    "9007199254740993",
    "0.1000000000000000055511151231257827",
    "1e999",
    "-1e999",
    "1e-999",
    '{"a": [1, {"b": 1e400}]}',
  ])("is refused for a number that would be sent as another: %s", (text) => {
    expect(typed(text)).toEqual({ problem: NUMBER });
  });

  it.each([
    '{"a": 1, "a": 1}',
    '{"a": 1, "\\u0061": 2}',
    '[{"a": {"b": 1, "c": [], "b": 2}}]',
    '{"a": {"b": 1}, "a": 2}',
    '{"a": [{"c": 1}], "b": 1, "a": 2}',
  ])("is refused for a key whose first value would be lost: %s", (text) => {
    expect(typed(text)).toEqual({ problem: TWICE });
  });

  it.each(["", "{quantity: 40}", "{'a': 1}", "01", "1e", "NaN", '{"a": 1,}'])(
    "is refused in the form's words when it isn't JSON: %s",
    (text) => {
      expect(typed(text)).toEqual({ problem: NOT_JSON });
    },
  );
});
