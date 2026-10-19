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
// What the editor draws over the formula's text, from the shared tokens:
// references (drawn as pills) and colors for the rest.
import { ROOTS, escapeSegment } from "./grammar";
import type { Token } from "./tokens";

export interface ReferenceSpan {
  from: number;
  to: number;
  pointer: string;
}
export type HighlightKind =
  | "keyword"
  | "operator"
  | "string"
  | "number"
  | "function"
  | "brace"
  | "error";
export interface HighlightSpan {
  from: number;
  to: number;
  kind: HighlightKind;
}

const KEYWORDS = new Set(["and", "or", "not", "true", "false", "null"]);
const POINTER = /^(?:\/(?:[^~/]|~[01])*)*$/;
const INDEX = /^(?:0|[1-9][0-9]*)$/;
const isPunct = (token: Token | undefined, text: string) =>
  token?.type === "punct" && token.text === text;

export function referenceSpans(tokens: readonly Token[]): ReferenceSpan[] {
  const spans: ReferenceSpan[] = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    const next = tokens[i + 1];
    const value = tokens[i + 2];
    if (
      token.type === "name" &&
      token.text === "ref" &&
      isPunct(next, "(") &&
      value?.type === "string" &&
      isPunct(tokens[i + 3], ")") &&
      POINTER.test(value.value as string)
    ) {
      spans.push({
        from: token.from,
        to: tokens[i + 3].to,
        pointer: value.value as string,
      });
      i += 3;
      continue;
    }
    if (
      token.type !== "name" ||
      !ROOTS.has(token.text) ||
      isPunct(next, "(") ||
      isPunct(next, ":")
    )
      continue;
    const segments = [token.text];
    let to = token.to;
    let j = i + 1;
    for (;;) {
      if (isPunct(tokens[j], ".") && tokens[j + 1]?.type === "segment") {
        segments.push(tokens[j + 1].text);
        to = tokens[j + 1].to;
        j += 2;
        continue;
      }
      const key = tokens[j + 1];
      if (
        isPunct(tokens[j], "[") &&
        isPunct(tokens[j + 2], "]") &&
        ((key?.type === "number" && INDEX.test(key.text)) ||
          key?.type === "string")
      ) {
        segments.push(key.type === "string" ? (key.value as string) : key.text);
        to = tokens[j + 2].to;
        j += 3;
        continue;
      }
      break;
    }
    spans.push({
      from: token.from,
      to,
      pointer: `/${segments.map(escapeSegment).join("/")}`,
    });
    i = j - 1;
  }
  return spans;
}

export function highlightSpans(tokens: readonly Token[]): HighlightSpan[] {
  const spans: HighlightSpan[] = [];
  tokens.forEach((token, i) => {
    const call = isPunct(tokens[i + 1], "(");
    const kind: HighlightKind | null =
      token.type === "string"
        ? "string"
        : token.type === "number"
          ? "number"
          : token.type === "op" || token.type === "assign"
            ? "operator"
            : token.type === "open" ||
                token.type === "close" ||
                token.type === "tick"
              ? "brace"
              : token.type === "error"
                ? "error"
                : token.type === "name" && call && !ROOTS.has(token.text)
                  ? "function"
                  : token.type === "name" && KEYWORDS.has(token.text)
                    ? "keyword"
                    : null;
    if (kind) spans.push({ from: token.from, to: token.to, kind });
  });
  return spans;
}
