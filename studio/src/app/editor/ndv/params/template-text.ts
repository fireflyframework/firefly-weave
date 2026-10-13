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
// Mapped text fields as text: "Hello {{ input.name }}" is a concat of text
// and placeholders written in the formula language. These helpers keep the
// names step details used before the formula editor: parts of text and
// references, their text, and what typed text means.
import {
  isReference,
  parseFormula,
  parseTemplate,
  type ParseOptions,
} from "../../formula/grammar";
import { printRef } from "../../formula/printer";
import { printTemplate, templateSegments } from "../../formula/template";
import type { Expression } from "../registry";
import type { ParamValue } from "./value-io";

export type TemplatePart = { text: string } | { ref: string };

/** A template field exists only on platforms with text.concat. */
const TEMPLATE_FIELD: ParseOptions = { features: ["text.concat"], loops: [] };

/** Text and references of a template, or null when a placeholder is any other formula. */
export function templateParts(expression: unknown): TemplatePart[] | null {
  const segments =
    typeof (expression as { literal?: unknown } | null)?.literal === "string" &&
    Object.keys(expression as object).length === 1
      ? [{ text: (expression as { literal: string }).literal }]
      : templateSegments(expression, true);
  if (!segments) return null;
  const parts: TemplatePart[] = [];
  for (const segment of segments) {
    if ("text" in segment) parts.push({ text: segment.text });
    else if (isReference(segment.node)) parts.push({ ref: segment.node.ref });
    else return null;
  }
  return parts;
}

/** A reference as formula text: a dotted path, brackets for other keys, or ref("…"). */
export function refToPath(ref: string): string {
  return printRef(ref);
}

/** The pointer a reference's formula text names, or null when the text is anything else. */
export function pathToRef(path: string): string | null {
  const result = parseFormula(path, { features: [], loops: [] });
  return result.kind === "ok" && isReference(result.expression)
    ? result.expression.ref
    : null;
}

export function partsToText(parts: TemplatePart[]): string {
  const args: Expression[] = parts.map((part) =>
    "text" in part ? { literal: part.text } : { ref: part.ref },
  );
  const text = printTemplate(
    args.length === 1 && "ref" in args[0]
      ? args[0]
      : { op: { name: "concat", args } },
  );
  return text ?? "";
}

export function textToExpression(
  text: string,
  options: ParseOptions = TEMPLATE_FIELD,
): { ok: true; value: ParamValue } | { ok: false; message: string } {
  const result = parseTemplate(text, options);
  if (result.kind === "error")
    return { ok: false, message: result.error.message };
  const expression = result.kind === "ok" ? result.expression : { literal: "" };
  return typeof (expression as { literal?: unknown }).literal === "string" &&
    Object.keys(expression).length === 1
    ? {
        ok: true,
        value: {
          mode: "fixed",
          value: (expression as { literal: string }).literal,
        },
      }
    : { ok: true, value: { mode: "mapped", expression } };
}
