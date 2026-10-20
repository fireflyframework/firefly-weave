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
// Colors for JSON being typed in the output editor: a forgiving scan that
// never throws on half-written text. It marks object keys, strings, numbers
// and the literals true, false and null; everything else is plain.
export type JsonTokenKind = "key" | "string" | "number" | "literal";
export interface JsonToken {
  from: number;
  to: number;
  kind: JsonTokenKind;
}

const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
const LITERAL = /(?:true|false|null)\b/y;

/** Every colored range of the text, in order; an unclosed string runs to the end of its line. */
export function jsonTokens(text: string): JsonToken[] {
  const tokens: JsonToken[] = [];
  let at = 0;
  while (at < text.length) {
    const char = text[at];
    if (char === '"') {
      let end = at + 1;
      while (end < text.length && text[end] !== '"' && text[end] !== "\n")
        end += text[end] === "\\" ? 2 : 1;
      const closed = text[end] === '"';
      end = Math.min(end + (closed ? 1 : 0), text.length);
      let next = end;
      while (next < text.length && /[ \t\r\n]/.test(text[next])) next++;
      // An unclosed string is never a key: the colon may belong to the next line.
      const key = closed && text[next] === ":";
      tokens.push({ from: at, to: end, kind: key ? "key" : "string" });
      at = end;
      continue;
    }
    if (char === "-" || (char >= "0" && char <= "9")) {
      NUMBER.lastIndex = at;
      const match = NUMBER.exec(text);
      if (match) {
        tokens.push({ from: at, to: at + match[0].length, kind: "number" });
        at += match[0].length;
        continue;
      }
    }
    if (char === "t" || char === "f" || char === "n") {
      LITERAL.lastIndex = at;
      const match = LITERAL.exec(text);
      if (match) {
        tokens.push({ from: at, to: at + match[0].length, kind: "literal" });
        at += match[0].length;
        continue;
      }
    }
    at++;
  }
  return tokens;
}
