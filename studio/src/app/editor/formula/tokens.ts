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
// One tokenizer for formulas and templates: the parser, the editor's colors
// and pills, and autocomplete all read the same tokens. In a template the
// text between placeholders is one token; inside `{{ … }}` the formula's
// tokens follow, and the placeholder ends at a `}}` that closes no brace of
// its own. In a run of three or more braces, the last two open the
// placeholder. A backtick starts a template inside a formula. The tokenizer
// stops after the first error token.
export type TokenType =
  | "punct"
  | "op"
  | "assign"
  | "string"
  | "number"
  | "name"
  | "segment"
  | "tick"
  | "text"
  | "open"
  | "close"
  | "error";
export interface Token {
  type: TokenType;
  from: number;
  to: number;
  /** The token's source text. */
  text: string;
  /** string and text: the unescaped text; number: its value. */
  value?: string | number;
  /** error: the WV-FORMULA code and its words. */
  code?: string;
  message?: string;
}
export type TokenMode = "formula" | "template";

export const MAX_SAFE_INTEGER = 9_007_199_254_740_991;
export const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;
const NAME = /[A-Za-z_][A-Za-z0-9_]*/y;
const SEGMENT = /[A-Za-z0-9_][A-Za-z0-9_-]*/y;
const NUMBER = /-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/y;
const HEX4 = /^[0-9a-fA-F]{4}$/;
const LONE_SURROGATE =
  /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
const PUNCT = new Set(["(", ")", "[", "]", "{", "}", ",", ":", "."]);
const PAIRS = new Set(["==", "!=", "<=", ">=", "&&", "||"]);
const ESCAPES: Record<string, string> = {
  '"': '"',
  "\\": "\\",
  "/": "/",
  b: "\b",
  f: "\f",
  n: "\n",
  r: "\r",
  t: "\t",
};

/** The column of an offset on its line, in characters, from 1. */
export function columnOf(source: string, offset: number): number {
  const start = source.lastIndexOf("\n", offset - 1) + 1;
  return Array.from(source.slice(start, offset)).length + 1;
}

type Frame =
  | { kind: "formula"; braces: number; placeholder: boolean }
  | { kind: "text"; tick: boolean };

/** Template text up to a placeholder, a closing backtick or the end. */
function readText(
  source: string,
  at: number,
  end: number,
  tick: boolean,
): { value: string; end: number } {
  let value = "";
  let i = at;
  while (i < end) {
    if (tick && source[i] === "`") break;
    if (source[i] === "{") {
      // In a run of braces the last two open the placeholder: "{{{ x }}" is
      // the text "{" before the placeholder.
      let run = 0;
      while (i + run < end && source[i + run] === "{") run++;
      if (run >= 2) {
        value += "{".repeat(run - 2);
        i += run - 2;
        break;
      }
      value += "{";
      i += 1;
      continue;
    }
    if (source[i] === "\\") {
      let run = 0;
      while (i + run < end && source[i + run] === "\\") run++;
      const after = i + run;
      const brace = source.startsWith("{{", after);
      if (brace || (tick && source[after] === "`")) {
        value += "\\".repeat(Math.floor(run / 2));
        if (run % 2 === 1) {
          const escaped = brace ? "{{" : "`";
          value += escaped;
          i = after + escaped.length;
          continue;
        }
        i = after;
        break;
      }
      value += "\\".repeat(run);
      i = after;
      continue;
    }
    value += source[i];
    i++;
  }
  return { value, end: i };
}

type Read = { value: string; end: number } | { error: Token };

function readString(source: string, at: number, end: number): Read {
  const quote = source[at];
  let value = "";
  let i = at + 1;
  while (i < end) {
    const character = source[i];
    if (character === quote) {
      if (LONE_SURROGATE.test(value))
        return {
          error: {
            type: "error",
            from: at,
            to: i + 1,
            text: source.slice(at, i + 1),
            code: "WV-FORMULA-STRING",
            message: "This text has a broken character escape.",
          },
        };
      return { value, end: i + 1 };
    }
    if (character.charCodeAt(0) < 0x20) break;
    if (character === "\\") {
      const next = source[i + 1] ?? "";
      if (next === "'" && quote === "'") {
        value += "'";
        i += 2;
        continue;
      }
      if (next in ESCAPES) {
        value += ESCAPES[next];
        i += 2;
        continue;
      }
      const hex = source.slice(i + 2, i + 6);
      if (next === "u" && HEX4.test(hex)) {
        value += String.fromCharCode(parseInt(hex, 16));
        i += 6;
        continue;
      }
      return {
        error: {
          type: "error",
          from: i,
          to: Math.min(i + 2, end),
          text: source.slice(i, i + 2),
          code: "WV-FORMULA-TOKEN",
          message: `Unexpected \`${source.slice(i, i + 2)}\` at column ${columnOf(source, i)}.`,
        },
      };
    }
    value += character;
    i++;
  }
  return {
    error: {
      type: "error",
      from: at,
      to: i,
      text: source.slice(at, i),
      code: "WV-FORMULA-STRING",
      message: `This text isn't closed. Add a closing \`${quote}\`.`,
    },
  };
}

export function tokenize(
  source: string,
  mode: TokenMode = "formula",
  from = 0,
  to = source.length,
): Token[] {
  const tokens: Token[] = [];
  const stack: Frame[] = [
    mode === "formula"
      ? { kind: "formula", braces: 0, placeholder: false }
      : { kind: "text", tick: false },
  ];
  const push = (
    type: TokenType,
    start: number,
    end: number,
    value?: string | number,
  ) =>
    tokens.push({
      type,
      from: start,
      to: end,
      text: source.slice(start, end),
      ...(value === undefined ? {} : { value }),
    });
  const error = (start: number, end: number, code: string, message: string) =>
    tokens.push({
      type: "error",
      from: start,
      to: end,
      text: source.slice(start, end),
      code,
      message,
    });
  let at = from;
  while (at < to) {
    const frame = stack[stack.length - 1];
    if (frame.kind === "text") {
      if (frame.tick && source[at] === "`") {
        push("tick", at, at + 1);
        stack.pop();
        at += 1;
        continue;
      }
      if (
        source.startsWith("{{", at) &&
        source[at + 2] !== "{" &&
        at + 2 <= to
      ) {
        push("open", at, at + 2);
        stack.push({ kind: "formula", braces: 0, placeholder: true });
        at += 2;
        continue;
      }
      const text = readText(source, at, to, frame.tick);
      push("text", at, text.end, text.value);
      at = text.end;
      continue;
    }
    const character = source[at];
    if (/\s/.test(character)) {
      at += 1;
      continue;
    }
    if (
      frame.placeholder &&
      frame.braces === 0 &&
      source.startsWith("}}", at) &&
      at + 2 <= to
    ) {
      push("close", at, at + 2);
      stack.pop();
      at += 2;
      continue;
    }
    if (character === "`") {
      push("tick", at, at + 1);
      stack.push({ kind: "text", tick: true });
      at += 1;
      continue;
    }
    const previous = tokens[tokens.length - 1];
    if (
      previous?.type === "punct" &&
      previous.text === "." &&
      /[A-Za-z0-9_]/.test(character)
    ) {
      SEGMENT.lastIndex = at;
      const segment = SEGMENT.exec(source)![0];
      const end = Math.min(at + segment.length, to);
      push("segment", at, end);
      at = end;
      continue;
    }
    if (PUNCT.has(character)) {
      if (frame.placeholder && character === "{") frame.braces += 1;
      if (frame.placeholder && character === "}")
        frame.braces = Math.max(0, frame.braces - 1);
      push("punct", at, at + 1);
      at += 1;
      continue;
    }
    const pair = source.slice(at, at + 2);
    if (PAIRS.has(pair) && at + 2 <= to) {
      push("op", at, at + 2);
      at += 2;
      continue;
    }
    if (character === "<" || character === ">" || character === "!") {
      push("op", at, at + 1);
      at += 1;
      continue;
    }
    if (character === "=") {
      push("assign", at, at + 1);
      at += 1;
      continue;
    }
    if (character === '"' || character === "'") {
      const read = readString(source, at, to);
      if ("error" in read) {
        tokens.push(read.error);
        return tokens;
      }
      push("string", at, read.end, read.value);
      at = read.end;
      continue;
    }
    if (character === "-" || (character >= "0" && character <= "9")) {
      NUMBER.lastIndex = at;
      const match = NUMBER.exec(source);
      if (!match || at + match[0].length > to) {
        error(
          at,
          at + 1,
          "WV-FORMULA-TOKEN",
          `Unexpected \`${character}\` at column ${columnOf(source, at)}.`,
        );
        return tokens;
      }
      const end = at + match[0].length;
      const value = Number(match[0]);
      if (!Number.isFinite(value)) {
        error(at, end, "WV-FORMULA-NUMBER", "This number is too large.");
        return tokens;
      }
      if (!match[1] && !match[2] && Math.abs(value) > MAX_SAFE_INTEGER) {
        error(
          at,
          end,
          "WV-FORMULA-NUMBER",
          "Whole numbers must be between −9,007,199,254,740,991 and 9,007,199,254,740,991.",
        );
        return tokens;
      }
      push("number", at, end, value);
      at = end;
      continue;
    }
    if (/[A-Za-z_]/.test(character)) {
      NAME.lastIndex = at;
      const name = NAME.exec(source)![0];
      const end = Math.min(at + name.length, to);
      push("name", at, end);
      at = end;
      continue;
    }
    const width = (source.codePointAt(at) ?? 0) > 0xffff ? 2 : 1;
    error(
      at,
      at + width,
      "WV-FORMULA-TOKEN",
      `Unexpected \`${source.slice(at, at + width)}\` at column ${columnOf(source, at)}.`,
    );
    return tokens;
  }
  return tokens;
}
