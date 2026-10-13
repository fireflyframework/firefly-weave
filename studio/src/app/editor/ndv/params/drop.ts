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
// Drag-to-map rules, free of the DOM: whether a dragged field fits a
// parameter, what a drop writes (a reference, or a placeholder at the caret
// of a template, keeping the text), rows from a dragged object, which field
// the pointer is over (with 8 px of slack), when to scroll, where a caret
// falls, and which references a step already maps.
import { parsePointer } from "../../../forms/core/json";
import { dragSchema } from "../panes/views";
import { uniqueIdentifier } from "./identifiers";
import {
  compatibility,
  typeLabel,
  type Schema,
  type ScopeEntry,
} from "../../../forms/core/scope";
import type { ParamSpec } from "../registry";
import {
  partsToText,
  refToPath,
  templateParts,
  textToExpression,
} from "./template-text";
import type { ParamValue } from "./value-io";

export interface DragRef {
  ref: string;
  breadcrumb: string;
  schema: Schema;
}
export type DropFit = "fits" | "unknown" | "mismatch" | "refused";
export type DropPlan =
  | { kind: "write"; next: ParamValue; replaced: string | null }
  | { kind: "refuse"; reason: string };
export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const FIXED_ONLY = "This field takes a fixed value.";
const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function parseDrag(text: string): DragRef | null {
  if (text.length > 65536) return null;
  try {
    const value: unknown = JSON.parse(text);
    if (!isObject(value) || typeof value["ref"] !== "string") return null;
    const ref = value["ref"];
    const parts = ref.length <= 4096 ? parsePointer(ref) : null;
    if (!parts?.length || parts.length > 64 || /[\u0000-\u001f]/.test(ref))
      return null;
    const breadcrumb =
      typeof value["breadcrumb"] === "string" ? value["breadcrumb"] : ref;
    if (breadcrumb.length > 4096) return null;
    return {
      ref,
      breadcrumb,
      schema: isObject(value["schema"]) ? dragSchema(value["schema"]) : {},
    };
  } catch {
    return null;
  }
}

const TYPE_SCHEMA: Partial<Record<ParamSpec["type"], Schema>> = {
  text: { type: "string" },
  multiline: { type: "string" },
  number: { type: "number" },
  boolean: { type: "boolean" },
  dateTime: { type: "string", format: "date-time" },
};

export function dropFit(
  spec: ParamSpec,
  mode: "fixed" | "mapped" | null,
  expected: Schema | null,
  drag: DragRef,
  templates: boolean,
): { fit: DropFit; text: string } {
  if (spec.type === "keyValue")
    return { fit: "fits", text: "Drop to add a field" };
  if (spec.type === "list") return { fit: "fits", text: "Drop to add an item" };
  if (mode === null) return { fit: "refused", text: FIXED_ONLY };
  if (templates) return { fit: "fits", text: "Fits" };
  const target = expected ?? TYPE_SCHEMA[spec.type] ?? {};
  switch (compatibility(drag.schema, target)) {
    case "compatible":
      return { fit: "fits", text: "Fits" };
    case "incompatible":
      return {
        fit: "mismatch",
        text: `${typeLabel(drag.schema)} doesn't fit a ${typeLabel(target)} field`,
      };
    default:
      return { fit: "unknown", text: "Type not checked" };
  }
}

const shown = (value: unknown): string =>
  typeof value === "string" ? value : JSON.stringify(value);

function insertAt(
  text: string,
  caret: number | null,
  ref: string,
): ParamValue | null {
  if (!refToPath(ref)) return null;
  const at =
    caret === null ? text.length : Math.max(0, Math.min(caret, text.length));
  const parsed = textToExpression(
    `${text.slice(0, at)}{{ ${refToPath(ref)} }}${text.slice(at)}`,
  );
  return parsed.ok ? parsed.value : null;
}

export function planDrop(
  spec: ParamSpec,
  current: ParamValue,
  drag: DragRef,
  options: {
    caret: number | null;
    templates: boolean;
    mode: "fixed" | "mapped" | null;
  },
): DropPlan {
  if (options.mode === null) return { kind: "refuse", reason: FIXED_ONLY };
  const reference: ParamValue = {
    mode: "mapped",
    expression: { ref: drag.ref },
  };
  if (
    current.mode === "absent" ||
    (current.mode === "fixed" &&
      (current.value === "" || current.value === null))
  )
    return { kind: "write", next: reference, replaced: null };
  const textField = ["text", "multiline", "urlTemplate"].includes(spec.type);
  if (options.templates) {
    const parts =
      current.mode === "mapped" ? templateParts(current.expression) : null;
    if (parts?.some((part) => "ref" in part && !refToPath(part.ref)))
      return {
        kind: "refuse",
        reason:
          "This formula cannot be edited as text. Use Edit as YAML in the More menu.",
      };
    if (current.mode === "fixed" && typeof current.value === "string") {
      if (!refToPath(drag.ref))
        return {
          kind: "refuse",
          reason:
            "This field name cannot be inserted as text. Use Edit as YAML in the More menu.",
        };
      const at =
        options.caret === null
          ? current.value.length
          : Math.max(0, Math.min(options.caret, current.value.length));
      const args = [
        ...(at ? [{ literal: current.value.slice(0, at) }] : []),
        { ref: drag.ref },
        ...(at < current.value.length
          ? [{ literal: current.value.slice(at) }]
          : []),
      ];
      return {
        kind: "write",
        next: { mode: "mapped", expression: { op: { name: "concat", args } } },
        replaced: null,
      };
    }
    const next = parts
      ? insertAt(partsToText(parts), options.caret, drag.ref)
      : null;
    if (next) return { kind: "write", next, replaced: null };
  }
  if (
    !options.templates &&
    current.mode === "mapped" &&
    isObject(current.expression) &&
    Object.keys(current.expression).length === 1 &&
    typeof current.expression["ref"] === "string"
  )
    return { kind: "write", next: reference, replaced: "the previous mapping" };
  if (current.mode === "mapped" || textField)
    return {
      kind: "refuse",
      reason:
        "This value cannot be extended here. Use Edit as YAML in the More menu.",
    };
  return { kind: "write", next: reference, replaced: shown(current.value) };
}

export function rowKey(ref: string, taken: ReadonlySet<string>): string {
  const parts = parsePointer(ref) ?? [];
  const base =
    parts[0] === "steps" && parts.length === 3
      ? parts[1]
      : (parts.at(-1) ?? "field");
  return uniqueIdentifier(base, taken, Math.max(128, base.length + 16));
}

export const childEntries = (
  entries: ScopeEntry[],
  ref: string,
): ScopeEntry[] =>
  entries.filter(
    (entry) =>
      entry.ref.startsWith(`${ref}/`) &&
      !entry.ref.slice(ref.length + 1).includes("/"),
  );

/** The output of the nearest earlier step, else the workflow input. */
export function nearestRoot(entries: ScopeEntry[]): string {
  const step = [...entries].reverse().find((entry) => entry.source === "step");
  if (!step) return "/input";
  return step.ref.split("/").slice(0, 4).join("/");
}

export function stickyTarget<T>(
  boxes: { item: T; box: Box }[],
  x: number,
  y: number,
  slack = 8,
): T | null {
  let best: { item: T; area: number } | null = null;
  for (const { item, box } of boxes) {
    if (
      x < box.left - slack ||
      x > box.right + slack ||
      y < box.top - slack ||
      y > box.bottom + slack
    )
      continue;
    const area = (box.right - box.left) * (box.bottom - box.top);
    if (!best || area < best.area) best = { item, area };
  }
  return best?.item ?? null;
}

export function autoScrollDelta(
  box: Pick<Box, "top" | "bottom">,
  y: number,
  edge = 48,
  speed = 16,
): number {
  if (y < box.top + edge) return -speed;
  if (y > box.bottom - edge) return speed;
  return 0;
}

export function caretAt(
  text: string,
  x: number,
  measure: (text: string) => number,
): number {
  if (x <= 0) return 0;
  const stops = caretStops(text);
  for (let i = 1; i < stops.length; i++) {
    const width = measure(text.slice(0, stops[i]));
    if (width >= x) {
      const before = measure(text.slice(0, stops[i - 1]));
      return x - before < width - x ? stops[i - 1] : stops[i];
    }
  }
  return text.length;
}

/** Text offsets that keep a displayed character, including emoji, intact. */
export function caretStops(text: string): number[] {
  const segments = new Intl.Segmenter(undefined, {
    granularity: "grapheme",
  }).segment(text);
  return [...segments].map((part) => part.index).concat(text.length);
}

/** References in one typed expression; literal payloads are never traversed. */
export function mappedRefs(value: unknown): string[] {
  const found = new Set<string>();
  const visit = (node: unknown, depth = 0) => {
    if (depth > 64 || !isObject(node) || Object.keys(node).length !== 1) return;
    if (typeof node["ref"] === "string") {
      found.add(node["ref"]);
      return;
    }
    if (isObject(node["object"]))
      Object.values(node["object"]).forEach((child) => visit(child, depth + 1));
    if (Array.isArray(node["array"]))
      node["array"].forEach((child) => visit(child, depth + 1));
    if (isObject(node["op"]) && Array.isArray(node["op"]["args"]))
      node["op"]["args"].forEach((child) => visit(child, depth + 1));
  };
  visit(value);
  return [...found];
}
