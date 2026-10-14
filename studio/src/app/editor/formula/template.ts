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
// Templates are a view of the platform's `concat`: text segments are string
// literals and every other argument is a `{{ formula }}` placeholder. Text
// escapes `{{` as `\{{` (and a backtick as `` \` `` inside a backtick
// template); backslashes the reader would take as escaping one of those
// (the placeholder's own `{{` counts), or right before a placeholder, are
// doubled; any other backslash is plain text.
import type { Expression } from "../ndv/registry";
import { isReference, isTextLiteral, type TemplatePart } from "./grammar";
import { printFormula } from "./printer";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function escapeTemplateText(
  text: string,
  followed: "placeholder" | "end",
  tick = false,
): string {
  let out = "";
  let i = 0;
  while (i < text.length) {
    if (text[i] === "\\") {
      let run = 0;
      while (text[i + run] === "\\") run++;
      const after = i + run;
      // What the reader sees after the backslashes: the rest of the text,
      // then the placeholder's `{{` or the closing backtick. A trailing `{`
      // and the placeholder's braces make `{{` too.
      const rest =
        text.slice(after) +
        (followed === "placeholder" ? "{{" : tick ? "`" : "");
      const special = rest.startsWith("{{") || (tick && rest[0] === "`");
      out += "\\".repeat(special ? run * 2 : run);
      i = after;
      continue;
    }
    if (text.startsWith("{{", i)) {
      out += "\\{{";
      i += 2;
      continue;
    }
    if (tick && text[i] === "`") {
      out += "\\`";
      i += 1;
      continue;
    }
    out += text[i];
    i += 1;
  }
  return out;
}

/**
 * The platform's template segments (compiler/templates.py `segments`): one
 * per `concat` argument, string literals as text. With `loneReference`, a
 * lone reference is a one-placeholder template, as step details show it.
 */
export function templateSegments(
  expression: unknown,
  loneReference = false,
): TemplatePart[] | null {
  if (loneReference && isReference(expression)) return [{ node: expression }];
  if (!isRecord(expression) || Object.keys(expression).length !== 1)
    return null;
  const op = expression["op"];
  if (
    !isRecord(op) ||
    Object.keys(op).length !== 2 ||
    op["name"] !== "concat" ||
    !Array.isArray(op["args"])
  )
    return null;
  return op["args"].map((arg) =>
    isTextLiteral(arg) ? { text: arg.literal } : { node: arg as Expression },
  );
}

/**
 * Segments as the reader gives them back: adjacent text merged and empty
 * text dropped (compiler/templates.py `template_expression`), so text is
 * escaped against the placeholder that really follows it.
 */
export function mergeTextSegments(
  parts: readonly TemplatePart[],
): TemplatePart[] {
  const out: TemplatePart[] = [];
  for (const part of parts) {
    if ("node" in part) out.push(part);
    else if (part.text) {
      const last = out[out.length - 1];
      if (last && "text" in last)
        out[out.length - 1] = { text: last.text + part.text };
      else out.push({ text: part.text });
    }
  }
  return out;
}

/**
 * Template text for a string literal, a lone reference or any concat; null
 * for other trees, and for a concat whose text would read back as a bare
 * reference (`concat(input.count)` turns its value into text; the reference
 * alone doesn't).
 */
export function printTemplate(expression: unknown): string | null {
  if (isTextLiteral(expression))
    return escapeTemplateText(expression.literal, "end");
  const segments = templateSegments(expression, true);
  if (!segments) return null;
  const parts = mergeTextSegments(segments);
  if (
    !isReference(expression) &&
    parts.length === 1 &&
    "node" in parts[0] &&
    isReference(parts[0].node)
  )
    return null;
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if ("text" in part) {
      out += escapeTemplateText(
        part.text,
        i + 1 < parts.length ? "placeholder" : "end",
      );
      continue;
    }
    const text = printFormula(part.node);
    if (text === null) return null;
    out += `{{ ${text} }}`;
  }
  return out;
}
