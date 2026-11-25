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
export { typeIcon } from "../../../forms/core/scope";
import {
  resolveSchema,
  type ResolveOptions,
} from "../../../forms/core/resolve";
import type { Step } from "../../../model";
import type { ErrorEntry } from "../../state/test-data";
import type { Json } from "../registry";
import {
  PLATFORM_FAILURES,
  genericFailureMessage,
} from "../../state/failure-codes";
import { simulatedOutcome } from "./simulate-failure";
import { uiKey, type BrowserStorage } from "../../state/browser-store";
import { studioStorage } from "../../state/studio-storage";
import { isFileSchema } from "../../../forms/core/file-reference";
import { fileValue } from "./file-value";

export type DataView = "schema" | "table" | "json";
export const VIEWS_KEY = uiKey("weave.ndv.views.v1");
const DEFAULT_VIEWS = {
  input: "schema" as DataView,
  output: "schema" as DataView,
};
const isView = (v: unknown): v is DataView =>
  v === "schema" || v === "table" || v === "json";
const defaultStorage = (): BrowserStorage | null => {
  try {
    return studioStorage();
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
  title?: string;
  depth: number;
  typeLabel: string;
  types: string[];
  ref?: string;
  breadcrumb?: string;
  schema?: Schema;
  /** The field's value in the test data, on one line (Schema view with data). */
  sample?: string;
  /** Its group may be without it; said of an input with a name of its own (the platform event). */
  optional?: boolean;
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
      // A file is one value: its fields are never listed.
      const file = isFileSchema(child);
      rows.push({
        key: `${prefix}/${escapeSegment(key)}`,
        label: key,
        title,
        depth,
        typeLabel: file ? "File" : typeLabel(child),
        types: schemaTypes(child),
        schema: child,
      });
      if (!file && depth + 1 < maxDepth && isObject(child["properties"]))
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

/** The workflow input without a name of its own. */
const WORKFLOW_INPUT = "Workflow input";
export function inputSources(scope: {
  steps: { id: string; kind: string }[];
  entries?: readonly {
    source: string;
    stepId?: string;
    path: unknown[];
    optional?: boolean;
    label?: string;
  }[];
}): Source[] {
  const input = (scope.entries ?? []).find(
    (entry) => entry.source === "input" && !entry.path.length,
  );
  // Inside a step's error path the step has failed: its error comes first.
  const errors = (scope.entries ?? [])
    .filter(
      (entry) =>
        entry.source === "error" && !entry.path.length && !entry.optional,
    )
    .map((entry) => ({
      id: `error:${entry.stepId}`,
      label: `Error of ${entry.stepId}`,
      detail: "",
      icon: "warning",
    }));
  const steps = [...scope.steps].reverse().map((step, i) => ({
    id: `step:${step.id}`,
    label: step.id,
    detail: i === 0 ? "1 step back" : `${i + 1} steps back`,
    icon: step.kind,
  }));
  return [
    ...errors,
    ...steps,
    {
      id: "input",
      label: input?.label ?? WORKFLOW_INPUT,
      detail: "",
      icon:
        (input?.label ?? WORKFLOW_INPUT) === WORKFLOW_INPUT
          ? "source"
          : "signal",
    },
  ];
}
/**
 * A simulated failure of `step` as the platform reports its error: its
 * message, outcome, attempts and instance. A transform's or a decision
 * table's failure didn't start, whatever outcome the test data holds.
 */
export function errorData(
  failure: ErrorEntry,
  step: Step | null,
  instanceKey: string,
): Record<string, Json> {
  return {
    code: failure.code,
    message:
      PLATFORM_FAILURES.find((item) => item.code === failure.code)?.message ??
      genericFailureMessage(failure.code),
    outcome: (step && simulatedOutcome(step)) ?? failure.outcome ?? "failed",
    attempts: failure.attempts ?? 1,
    instanceKey,
  };
}
/** An entry of what a step can read: the offline scope's, or the host's inside a loop body, with the loop's item and position. */
export type PaneEntry = Omit<ScopeEntry, "source"> & {
  source: string;
  /** The loop whose item or position this is. */
  loop?: string;
};
const LOOP_WORDS: Record<string, string> = {
  item: "Current item",
  index: "Item position",
};
/** What a loop's item or position is called: the innermost loop's plainly, another one's with its name. */
const loopLabel = (entry: PaneEntry): string =>
  entry.ref.startsWith("/loops/")
    ? `Loop ${entry.loop} › ${LOOP_WORDS[entry.source]}`
    : LOOP_WORDS[entry.source];

/**
 * Inside a loop body: the innermost loop's Current item and Item position,
 * then those of each loop around it, then the workflow input and the steps
 * before, nearest first.
 */
export function loopSources(
  entries: readonly PaneEntry[],
  steps: { id: string; kind: string }[],
): Source[] {
  const roots = entries.filter(
    (entry) => !entry.path.length && entry.loop && entry.source in LOOP_WORDS,
  );
  const named = (entry: PaneEntry) => entry.ref.startsWith("/loops/");
  const loops = [
    ...roots.filter((entry) => !named(entry)),
    ...roots.filter(named),
  ].map((entry) => ({
    id: named(entry)
      ? `${entry.source === "item" ? "loop" : "loop-index"}:${entry.loop}`
      : entry.source,
    label: loopLabel(entry),
    detail: "",
    icon: "forEach",
  }));
  const [input, ...before] = [...inputSources({ steps })].reverse();
  return [...loops, input, ...before.reverse()];
}
/** A loop source's parts: whose item (null for the innermost loop) and whether it is the position. */
export function loopSource(
  source: string,
): { loop: string | null; index: boolean } | null {
  if (source === "item" || source === "index")
    return { loop: null, index: source === "index" };
  const named = /^loop(-index)?:(.+)$/.exec(source);
  return named ? { loop: named[2], index: !!named[1] } : null;
}
export const defaultSource = (sources: Source[]): string =>
  sources[0]?.id ?? "input";

const escaped = (id: string) => id.replace(/~/g, "~0").replace(/\//g, "~1");
export function rootOf(source: string): string {
  if (source === "input") return "/input";
  if (source === "item" || source === "index") return `/${source}`;
  const loop = /^loop(-index)?:(.*)$/.exec(source);
  if (loop) return `/loops/${escaped(loop[2])}/${loop[1] ? "index" : "item"}`;
  if (source.startsWith("error:"))
    return `/steps/${escaped(source.slice(6))}/error`;
  return `/steps/${escaped(source.slice(5))}/output`;
}

/** Whether a field may be missing from the group that holds it. */
function onlySome(entry: PaneEntry, entries: readonly PaneEntry[]): boolean {
  if (!entry.optional) return false;
  const holder = entry.ref.slice(0, entry.ref.lastIndexOf("/"));
  const group = entries.find((other) => other.ref === holder)?.schema;
  const required = group?.["required"];
  return !(
    Array.isArray(required) && required.includes(entry.path.at(-1) ?? "")
  );
}

export function sourceRows(
  entries: readonly PaneEntry[],
  source: string,
): TreeRow[] {
  const root = rootOf(source);
  // An input with a name of its own (the platform event) names its fields' root.
  const label = entries.find((entry) => entry.ref === "/input")?.label;
  const named = label === undefined || label === WORKFLOW_INPUT ? null : label;
  const descendants = entries.filter((entry) =>
    entry.ref.startsWith(`${root}/`),
  );
  return (
    descendants.length
      ? descendants
      : entries.filter((entry) => entry.ref === root)
  ).map((entry) => ({
    key: entry.ref,
    label: entry.path.at(-1) ?? entry.label,
    title: entry.label,
    depth: Math.max(0, entry.path.length - 1),
    typeLabel: entry.typeLabel,
    types: entry.types,
    ref: entry.ref,
    breadcrumb: [
      entry.source === "input"
        ? (named ?? "Input")
        : entry.source in LOOP_WORDS
          ? loopLabel(entry)
          : entry.stepId,
      ...entry.path,
    ].join(" › "),
    schema: entry.schema,
    ...(named && entry.source === "input" && onlySome(entry, entries)
      ? { optional: true }
      : {}),
  }));
}

/** The value at a JSON pointer below `root` in `data`, or undefined. */
function valueAt(data: unknown, root: string, ref: string): unknown {
  if (ref !== root && !ref.startsWith(`${root}/`)) return undefined;
  let value = data;
  for (const raw of ref.slice(root.length).split("/").slice(1)) {
    const segment = raw.replace(/~1/g, "/").replace(/~0/g, "~");
    if (Array.isArray(value)) value = value[Number(segment)];
    else if (
      value &&
      typeof value === "object" &&
      Object.hasOwn(value, segment)
    )
      value = (value as Record<string, unknown>)[segment];
    else return undefined;
  }
  return value;
}
/**
 * Rows with each field's value from the source's data, as the Schema view
 * shows it: text in quotes, numbers and Yes/No as written, groups and lists
 * by their size; 60 characters at most.
 */
export function withSamples(
  rows: TreeRow[],
  data: unknown,
  root: string,
): TreeRow[] {
  if (data === undefined) return rows;
  return rows.map((row) => {
    // Input rows carry their reference; output rows their pointer below the output.
    const value = valueAt(data, root, row.ref ?? row.key);
    if (value === undefined) return row;
    const file = fileValue(value, "sample");
    const text = file
      ? file.detail
      : Array.isArray(value)
        ? `${value.length} ${value.length === 1 ? "item" : "items"}`
        : value && typeof value === "object"
          ? `${Object.keys(value).length} ${Object.keys(value).length === 1 ? "field" : "fields"}`
          : JSON.stringify(value);
    return {
      ...row,
      sample: text.length > 60 ? `${text.slice(0, 59)}…` : text,
    };
  });
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
