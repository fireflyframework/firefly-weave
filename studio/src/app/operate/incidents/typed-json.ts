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
// A result a person types, which the audit log keeps. JSON.parse changes some
// of them without a word: a number it can't hold becomes another, or null once
// sent, and only the last value of a repeated key stays. Pure.

/** The value typed, or why it isn't sent. */
export type TypedJson = { value: unknown } | { problem: string };

const NUMBER =
  "A number in the result can't be sent exactly as typed. Use a shorter or smaller one, or text in quotes if the result takes text.";
const TWICE =
  "An object in the result has the same key twice. Keep one of them.";
/** Valid JSON's strings, numbers and marks. */
const TOKENS =
  /"[^"\\]*(?:\\.[^"\\]*)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}[\]:]/g;
const DECIMAL = /^-?(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/;

/** The text as JSON, only when what is sent says what was typed; `notJson` when it isn't JSON. */
export function typedJson(text: string, notJson: string): TypedJson {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { problem: notJson };
  }
  /** The keys of each object being read; null for a list. */
  const open: (Set<string> | null)[] = [];
  let last = "";
  for (const [token] of text.matchAll(TOKENS)) {
    if (token === "{") open.push(new Set());
    else if (token === "[") open.push(null);
    else if (token === "}" || token === "]") open.pop();
    else if (token === ":") {
      const key = JSON.parse(last) as string;
      const keys = open.at(-1);
      if (keys?.has(key)) return { problem: TWICE };
      keys?.add(key);
    } else if (!token.startsWith('"') && !survives(token))
      return { problem: NUMBER };
    last = token;
  }
  return { value };
}

/** Whether a number is still the same once it is read and sent. */
function survives(number: string): boolean {
  const read = Number(number);
  return Number.isFinite(read) && decimal(String(read)) === decimal(number);
}
/** A number's digits and their power of ten, however it is written: 1.10 and 11e-1 are the same. Its sign never changes. */
function decimal(number: string): string {
  const [, whole, fraction = "", exponent = "0"] = DECIMAL.exec(number)!;
  const digits = (whole + fraction).replace(/^0+/, "");
  const kept = digits.replace(/0+$/, "");
  const power =
    Number(exponent) - fraction.length + digits.length - kept.length;
  return kept ? `${kept}e${power}` : "0";
}
