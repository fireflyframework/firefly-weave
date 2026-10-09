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
// and references. Between the braces, a field is named by a dotted path
// (input.name, steps.check.output.amount); other formulas are edited as
// YAML until the formula editor arrives. \{{ keeps literal braces.
import { parsePointer, formatPointer } from "../../../forms/core/json";
import type { Expression } from "../registry";
import type { ParamValue } from "./value-io";

export type TemplatePart = { text: string } | { ref: string };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const SEGMENT = /^[A-Za-z0-9_-]+$/;
const STEP_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

function part(expression: unknown): TemplatePart | null {
  if (!isRecord(expression) || Object.keys(expression).length !== 1)
    return null;
  if (typeof expression["literal"] === "string")
    return { text: expression["literal"] };
  if (typeof expression["ref"] === "string") return { ref: expression["ref"] };
  return null;
}

export function templateParts(expression: unknown): TemplatePart[] | null {
  const single = part(expression);
  if (single) return [single];
  const op =
    isRecord(expression) &&
    Object.keys(expression).length === 1 &&
    isRecord(expression["op"])
      ? expression["op"]
      : null;
  if (
    !op ||
    Object.keys(op).length !== 2 ||
    op["name"] !== "concat" ||
    !Array.isArray(op["args"])
  )
    return null;
  const parts = op["args"].map(part);
  return parts.every((p): p is TemplatePart => !!p) ? parts : null;
}

export function refToPath(ref: string): string | null {
  const segments = parsePointer(ref);
  if (!segments) return null;
  const input =
    segments[0] === "input" && segments.slice(1).every((s) => SEGMENT.test(s));
  const step =
    segments[0] === "steps" &&
    STEP_ID.test(segments[1] ?? "") &&
    segments[2] === "output" &&
    segments.slice(3).every((s) => SEGMENT.test(s));
  if (!input && !step) return null;
  const path = segments.join(".");
  // A dotted step ID can contain the output delimiter; only lossless paths are editable.
  return pathToRef(path) === ref ? path : null;
}

export function pathToRef(path: string): string | null {
  const text = path.trim();
  if (text === "input" || text.startsWith("input.")) {
    const rest = text === "input" ? [] : text.slice(6).split(".");
    return rest.every((s) => SEGMENT.test(s))
      ? formatPointer(["input", ...rest])
      : null;
  }
  const match = /^steps\.(.+?)\.output((?:\.[A-Za-z0-9_-]+)*)$/.exec(text);
  if (!match || !STEP_ID.test(match[1])) return null;
  const rest = match[2] ? match[2].slice(1).split(".") : [];
  return formatPointer(["steps", match[1], "output", ...rest]);
}

export function partsToText(parts: TemplatePart[]): string {
  return parts
    .map((p) =>
      "text" in p
        ? p.text.replace(/\{\{/g, "\\{{")
        : `{{ ${refToPath(p.ref) ?? p.ref} }}`,
    )
    .join("");
}

export function textToExpression(
  text: string,
): { ok: true; value: ParamValue } | { ok: false; message: string } {
  const parts: TemplatePart[] = [];
  let buffer = "";
  for (let i = 0; i < text.length; ) {
    if (text.startsWith("\\{{", i)) {
      buffer += "{{";
      i += 3;
      continue;
    }
    if (text.startsWith("{{", i)) {
      const end = text.indexOf("}}", i + 2);
      if (end < 0)
        return { ok: false, message: "Close the braces: {{ input.field }}." };
      const ref = pathToRef(text.slice(i + 2, end));
      if (!ref)
        return {
          ok: false,
          message:
            "Between the braces, name a field such as input.name or steps.check.output.amount.",
        };
      if (buffer) parts.push({ text: buffer });
      buffer = "";
      parts.push({ ref });
      i = end + 2;
      continue;
    }
    buffer += text[i++];
  }
  if (buffer) parts.push({ text: buffer });
  const refs = parts.filter((p) => "ref" in p);
  if (!refs.length)
    return {
      ok: true,
      value: {
        mode: "fixed",
        value: parts.map((p) => ("text" in p ? p.text : "")).join(""),
      },
    };
  if (parts.length === 1)
    return {
      ok: true,
      value: {
        mode: "mapped",
        expression: { ref: (parts[0] as { ref: string }).ref },
      },
    };
  const args: Expression[] = parts.map((p) =>
    "text" in p ? { literal: p.text } : { ref: p.ref },
  );
  return {
    ok: true,
    value: { mode: "mapped", expression: { op: { name: "concat", args } } },
  };
}
