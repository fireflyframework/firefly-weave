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
// What the data panes show without data: the fields a source offers (the
// workflow input or an earlier step), their types and depth, a filter, the
// counts, the views the person last chose, and why some steps have no output.
import { formatDuration } from "../../../designer/canvas-summary";
import {
  escapeSegment,
  schemaTypes,
  typeLabel,
  type Schema,
  type ScopeEntry,
} from "../../../forms/core/scope";
import {
  resolveSchema,
  type ResolveOptions,
} from "../../../forms/core/resolve";
import type { Step } from "../../../model";
import { uiKey } from "../../state/browser-store";

export type DataView = "schema" | "table" | "json";
export const VIEWS_KEY = uiKey("weave.ndv.views.v1");
const DEFAULT_VIEWS = {
  input: "schema" as DataView,
  output: "schema" as DataView,
};
const isView = (v: unknown): v is DataView =>
  v === "schema" || v === "table" || v === "json";
const defaultStorage = (): Storage | null => {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
};

export function readViews(
  storage: Pick<Storage, "getItem"> | null = defaultStorage(),
): { input: DataView; output: DataView } {
  try {
    const raw = JSON.parse(storage?.getItem(VIEWS_KEY) ?? "null") as {
      input?: unknown;
      output?: unknown;
    } | null;
    if (raw)
      return {
        input: isView(raw.input) ? raw.input : "schema",
        output: isView(raw.output) ? raw.output : "schema",
      };
  } catch {
    // An unreadable choice means none was kept.
  }
  return { ...DEFAULT_VIEWS };
}
export function writeViews(
  views: { input: DataView; output: DataView },
  storage: Pick<Storage, "setItem"> | null = defaultStorage(),
): boolean {
  try {
    if (!storage) return false;
    storage.setItem(VIEWS_KEY, JSON.stringify(views));
    return true;
  } catch {
    return false;
  }
}

export interface TreeRow {
  key: string;
  label: string;
  depth: number;
  typeLabel: string;
  types: string[];
  ref?: string;
  breadcrumb?: string;
  schema?: Schema;
}

const isObject = (value: unknown): value is Schema =>
  !!value && typeof value === "object" && !Array.isArray(value);

export function schemaRows(schema: unknown, maxDepth = 4): TreeRow[] {
  const rows: TreeRow[] = [];
  const visit = (
    rawNode: unknown,
    depth: number,
    prefix: string,
    options: ResolveOptions,
  ) => {
    const resolved = resolveSchema(rawNode, options);
    const node = resolved.schema;
    const properties = isObject(node["properties"]) ? node["properties"] : {};
    for (const [key, raw] of Object.entries(properties)) {
      if (rows.length >= 200) return;
      const child = resolveSchema(raw, resolved.context).schema;
      const title =
        typeof child["title"] === "string" && child["title"].trim()
          ? child["title"].trim()
          : key;
      rows.push({
        key: `${prefix}/${escapeSegment(key)}`,
        label: title,
        depth,
        typeLabel: typeLabel(child),
        types: schemaTypes(child),
        schema: child,
      });
      if (depth + 1 < maxDepth && isObject(child["properties"]))
        visit(
          raw,
          depth + 1,
          `${prefix}/${escapeSegment(key)}`,
          resolved.context,
        );
    }
  };
  if (isObject(schema)) {
    visit(schema, 0, "", { root: schema });
    if (
      !rows.length &&
      schemaTypes(schema).length &&
      !schemaTypes(schema).includes("object")
    )
      rows.push({
        key: "",
        label: "Value",
        depth: 0,
        typeLabel: typeLabel(schema),
        types: schemaTypes(schema),
        schema,
      });
  }
  return rows;
}

export interface Source {
  id: string;
  label: string;
  detail: string;
  icon: string;
}

export function inputSources(scope: {
  steps: { id: string; kind: string }[];
}): Source[] {
  const steps = [...scope.steps].reverse().map((step, i) => ({
    id: `step:${step.id}`,
    label: step.id,
    detail: i === 0 ? "1 step back" : `${i + 1} steps back`,
    icon: step.kind,
  }));
  return [
    ...steps,
    { id: "input", label: "Workflow input", detail: "", icon: "source" },
  ];
}
export const defaultSource = (sources: Source[]): string =>
  sources[0]?.id ?? "input";

const rootOf = (source: string): string =>
  source === "input"
    ? "/input"
    : `/steps/${source.slice(5).replace(/~/g, "~0").replace(/\//g, "~1")}/output`;

export function sourceRows(entries: ScopeEntry[], source: string): TreeRow[] {
  const root = rootOf(source);
  const descendants = entries.filter((entry) =>
    entry.ref.startsWith(`${root}/`),
  );
  return (
    descendants.length
      ? descendants
      : entries.filter((entry) => entry.ref === root)
  ).map((entry) => ({
    key: entry.ref,
    label: entry.label,
    depth: Math.max(0, entry.path.length - 1),
    typeLabel: entry.typeLabel,
    types: entry.types,
    ref: entry.ref,
    breadcrumb: entry.breadcrumb.replace(/^Workflow input(?= ›|$)/, "Input"),
    schema: entry.schema,
  }));
}

export function filterRows(rows: TreeRow[], query: string): TreeRow[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return rows;
  return rows.filter((row) =>
    words.every((w) =>
      `${row.label} ${row.key} ${row.typeLabel}`.toLowerCase().includes(w),
    ),
  );
}

export function countLabel(
  total: number,
  shown?: number,
  noun = "field",
): string {
  const plural = (n: number) => `${n} ${noun}${n === 1 ? "" : "s"}`;
  return shown === undefined || shown === total
    ? plural(total)
    : `${shown} of ${plural(total)}`;
}

export function typeIcon(types: string[], label: string): string {
  if (/date/i.test(label)) return "typeDate";
  if (/file/i.test(label)) return "typeFile";
  const type = types.find((t) => t !== "null");
  switch (type) {
    case "string":
      return "typeText";
    case "number":
    case "integer":
      return "typeNumber";
    case "boolean":
      return "typeBoolean";
    case "array":
      return "typeList";
    case "object":
      return "typeObject";
    default:
      return "typeAny";
  }
}

export function noOutputSentence(step: Step): string | null {
  if (step.kind === "wait")
    return `Waits ${formatDuration(step["durationSeconds"]) || "a while"}, then continues. It has no output.`;
  if (step.kind === "fail")
    return `Stops the run with error ${String(step["code"] ?? "")}.`;
  return null;
}

/** Only an explicit saved choice overrides a data-dependent default. */
export function savedView(pane: "input" | "output"): DataView | undefined {
  try {
    const raw = JSON.parse(defaultStorage()?.getItem(VIEWS_KEY) ?? "null");
    return isView(raw?.[pane]) ? raw[pane] : undefined;
  } catch {
    return undefined;
  }
}
export function writeView(pane: "input" | "output", view: DataView): void {
  try {
    const storage = defaultStorage();
    const other = pane === "input" ? "output" : "input";
    const previous = savedView(other);
    storage?.setItem(
      VIEWS_KEY,
      JSON.stringify({
        ...(previous ? { [other]: previous } : {}),
        [pane]: view,
      }),
    );
  } catch {
    /* Preferences are optional. */
  }
}
export function defaultOutputView(value: unknown): DataView {
  if (Array.isArray(value) && value.length && value.every(isObject))
    return "table";
  return value !== undefined && !isObject(value) && !Array.isArray(value)
    ? "json"
    : "schema";
}
export function radioKey(event: KeyboardEvent): void {
  if (
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    ![
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "ArrowDown",
      "Home",
      "End",
    ].includes(event.key)
  )
    return;
  const radios = [
    ...(event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>(
      '[role="radio"]',
    ),
  ];
  const at = radios.indexOf(document.activeElement as HTMLButtonElement);
  if (at < 0) return;
  event.preventDefault();
  const next =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? radios.length - 1
        : (at +
            (["ArrowRight", "ArrowDown"].includes(event.key)
              ? 1
              : radios.length - 1)) %
          radios.length;
  radios[next].click();
  radios[next].focus();
}

/** Drag metadata describes shape without copying schema-provided example values. */
export function dragSchema(schema: Schema): Schema {
  const omit = new Set(["default", "examples", "const", "enum"]);
  const maps = new Set([
    "properties",
    "$defs",
    "definitions",
    "patternProperties",
  ]);
  let left = 500;
  const visit = (value: unknown, depth: number): unknown => {
    if (--left < 0 || depth > 10) return {};
    if (Array.isArray(value))
      return value.map((child) => visit(child, depth + 1));
    if (!isObject(value)) return value;
    return Object.fromEntries(
      Object.entries(value)
        .filter(([key]) => !omit.has(key))
        .map(([key, child]) => [
          key,
          maps.has(key) && isObject(child)
            ? Object.fromEntries(
                Object.entries(child).map(([name, node]) => [
                  name,
                  visit(node, depth + 1),
                ]),
              )
            : visit(child, depth + 1),
        ]),
    );
  };
  return visit(schema, 0) as Schema;
}

export function sampleCount(value: unknown): string {
  if (Array.isArray(value)) return countLabel(value.length, undefined, "item");
  if (isObject(value)) return countLabel(Object.keys(value).length);
  return typeLabel({ type: value === null ? "null" : typeof value });
}
