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
import { jsonTokens } from "../src/app/editor/ndv/panes/json-tokens";

const kinds = (text: string) =>
  jsonTokens(text).map(
    (token) => `${token.kind}:${text.slice(token.from, token.to)}`,
  );

describe("colors for typed JSON", () => {
  it("tells keys from strings, and marks numbers and literals", () => {
    expect(
      kinds('{"id": "o-1", "total": -1.5e3, "paid": true, "note": null}'),
    ).toEqual([
      'key:"id"',
      'string:"o-1"',
      'key:"total"',
      "number:-1.5e3",
      'key:"paid"',
      "literal:true",
      'key:"note"',
      "literal:null",
    ]);
  });
  it("keeps escaped quotes inside a string", () => {
    expect(kinds('["a\\"b", 2]')).toEqual(['string:"a\\"b"', "number:2"]);
  });
  it("never throws on half-written text and never returns an empty range", () => {
    for (const text of ['{"a', '{"a": tr', "[1, -", '"\\', "", '{\n  "x":\n']) {
      const tokens = jsonTokens(text);
      for (const token of tokens) expect(token.to).toBeGreaterThan(token.from);
    }
    expect(kinds('{"open\n: 1}')).toEqual(['string:"open', "number:1"]);
  });
});
