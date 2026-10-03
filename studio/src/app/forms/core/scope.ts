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
// Which data an expression may reference, computed with the compiler's own
// lexical dominance rules (compiler/analyzer.py `block`). A step's output is
// visible only after the step completes on every path that reaches it:
// - a sequence sees earlier siblings;
// - a branch sees what was visible at its group plus earlier steps in the
//   same branch, never the group itself or sibling branches;
// - a case `when` sees only what was visible before the group;
// - a branch `output` sees that branch's own steps;
// - after a group, only the group's id becomes visible;
// - `fail` (and a group whose paths cannot complete) ends reachability.
// The compiler stays authoritative; this module only drives suggestions.

type JsonObject = Record<string, unknown>;
export type Schema = JsonObject;

/** The workflow itself as a diagnostic or scope target (its `spec.output`). */
export const WORKFLOW = "$workflow";

export interface VisibleStep {
  id: string;
  kind: string;
  /** Best known output schema; `{}` when unknown. */
  schema: Schema;
}

export interface ScopeEntry {
  /** RFC 6901 pointer usable as `{ref}`, e.g. `/steps/check/output/eligible`. */
  ref: string;
  source: "input" | "step";
  stepId?: string;
  stepKind?: string;
  /** Property path below `/input` or `/steps/<id>/output` (unescaped keys). */
  path: string[];
  /** Field title when declared, otherwise its key; the step id for roots. */
  label: string;
  /** "Workflow input › Customer ID" or "check › eligible". */
  breadcrumb: string;
  schema: Schema;
  /** JSON types the value may have; empty when unknown. */
  types: string[];
  /** Plain-language type, e.g. "Text", "Whole number", "Any value". */
  typeLabel: string;
  /** True when the value may be absent at run time. */
  optional: boolean;
}

export interface ReferenceScope {
  /** False when the step or field does not exist (nothing is suggested). */
  found: boolean;
  /**
   * False for an output the compiler never evaluates because every path
   * before it fails. Suggestions are still offered; nothing is checked there.
   */
  evaluated: boolean;
  steps: VisibleStep[];
  entries: ScopeEntry[];
  truncated: boolean;
}

export interface ScopeOptions {
  /** Output schema of a published action by its `uses` reference. */
  actionOutput?: (uses: string) => unknown;
  /** Deepest property path expanded below a root (default 4). */
  maxDepth?: number;
  /** Total suggestions, roots included (default 200). */
  maxEntries?: number;
}

const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const schemaOf = (value: unknown): Schema =>
  isObject(value) ? value : value === false ? { not: {} } : {};

export const escapeSegment = (segment: string) =>
  segment.replace(/~/g, "~0").replace(/\//g, "~1");
const unescapeSegment = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");
function segmentsOf(pointer: string): string[] {
  const trimmed = pointer.startsWith("/") ? pointer.slice(1) : pointer;
  return trimmed === "" ? [] : trimmed.split("/").map(unescapeSegment);
}

type Target =
  | { stepId: string; kind: "step" }
  | { stepId: string; kind: "branch"; branch: string }
  | { stepId: typeof WORKFLOW; kind: "workflow" };

function targetOf(stepId: string, fieldPath: string): Target | null {
  const segments = segmentsOf(fieldPath);
  if (stepId === WORKFLOW) {
    const joined = segments.join("/");
    return joined === "spec/output" || joined === "output"
      ? { stepId: WORKFLOW, kind: "workflow" }
      : null;
  }
  if (segments[0] === "cases" && segments[2] === "output")
    return { stepId, kind: "branch", branch: `case:${segments[1]}` };
  if (segments[0] === "default" && segments[1] === "output")
    return { stepId, kind: "branch", branch: "default" };
  if (segments[0] === "branches" && segments[2] === "output")
    return { stepId, kind: "branch", branch: `branch:${segments[1]}` };
  return { stepId, kind: "step" };
}

interface Capture {
  visible: Map<string, VisibleStep>;
  evaluated: boolean;
}

/** Literal JSON → the schema the compiler would infer for its shape. */
function literalSchema(value: unknown, depth = 0): Schema {
  if (value === null) return { type: "null" };
  if (typeof value === "boolean") return { type: "boolean" };
  if (typeof value === "number")
    return { type: Number.isInteger(value) ? "integer" : "number" };
  if (typeof value === "string") return { type: "string" };
  if (Array.isArray(value)) return { type: "array" };
  if (!isObject(value) || depth > 8) return {};
  return closedObject(
    Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, literalSchema(v, depth + 1)]),
    ),
  );
}
const closedObject = (properties: Record<string, Schema>): Schema => ({
  type: "object",
  properties,
  required: Object.keys(properties),
  additionalProperties: false,
});

interface Property {
  schema: Schema;
  required: boolean;
}

/** Properties of an object-shaped schema: allOf merge, local $ref, anyOf intersection. */
function objectProperties(
  schema: Schema,
  root: Schema,
  seen: Set<unknown> = new Set(),
): Map<string, Property> | null {
  if (seen.has(schema) || seen.size > 32) return null;
  seen.add(schema);
  const resolved = resolveRef(schema, root);
  if (resolved !== schema) return objectProperties(resolved, root, seen);
  const alternatives = (schema["anyOf"] ?? schema["oneOf"]) as unknown;
  if (Array.isArray(alternatives) && alternatives.length) {
    const views = alternatives.map((v) =>
      objectProperties(schemaOf(v), root, new Set(seen)),
    );
    if (views.some((v) => v === null)) return null;
    const [first, ...rest] = views as Map<string, Property>[];
    // A field set on only some paths is rejected by the compiler.
    return new Map(
      [...first].filter(([key]) => rest.every((view) => view.has(key))),
    );
  }
  const out = new Map<string, Property>();
  const required = new Set(
    Array.isArray(schema["required"]) ? (schema["required"] as string[]) : [],
  );
  if (isObject(schema["properties"]))
    for (const [key, value] of Object.entries(schema["properties"]))
      out.set(key, { schema: schemaOf(value), required: required.has(key) });
  if (Array.isArray(schema["allOf"]))
    for (const part of schema["allOf"]) {
      const view = objectProperties(schemaOf(part), root, new Set(seen));
      for (const [key, property] of view ?? [])
        out.set(key, {
          schema: property.schema,
          required:
            property.required ||
            required.has(key) ||
            (out.get(key)?.required ?? false),
        });
    }
  for (const key of out.keys())
    if (required.has(key)) out.set(key, { ...out.get(key)!, required: true });
  return out;
}

function resolveRef(schema: Schema, root: Schema): Schema {
  let current = schema;
  for (let hops = 0; hops < 16; hops++) {
    const target = current["$ref"];
    if (typeof target !== "string" || !target.startsWith("#/")) return current;
    let node: unknown = root;
    for (const segment of segmentsOf(target.slice(1)))
      node = isObject(node) ? node[segment] : undefined;
    if (!isObject(node)) return current;
    current = node;
  }
  return current;
}

/** JSON types a schema admits; empty when unknown. */
export function schemaTypes(schema: Schema, root: Schema = schema): string[] {
  const resolved = resolveRef(schema, root);
  if ("const" in resolved) return [jsonType(resolved["const"])];
  if (Array.isArray(resolved["enum"]))
    return unique((resolved["enum"] as unknown[]).map(jsonType));
  const type = resolved["type"];
  if (typeof type === "string") return [type];
  if (Array.isArray(type)) return unique(type as string[]);
  const alternatives = (resolved["anyOf"] ?? resolved["oneOf"]) as unknown;
  if (Array.isArray(alternatives) && alternatives.length) {
    const sets = alternatives.map((v) => schemaTypes(schemaOf(v), root));
    return sets.some((s) => !s.length) ? [] : unique(sets.flat());
  }
  if (isObject(resolved["properties"])) return ["object"];
  if (isObject(resolved["items"])) return ["array"];
  return [];
}
const unique = (values: string[]) => [...new Set(values)];
function jsonType(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number")
    return Number.isInteger(value) ? "integer" : "number";
  return typeof value === "object" ? "object" : typeof value;
}

const singular: Record<string, string> = {
  string: "Text",
  integer: "Whole number",
  number: "Number",
  boolean: "Yes or no",
  object: "Object",
  array: "List",
  null: "Empty",
};
const plural: Record<string, string> = {
  string: "text",
  integer: "whole numbers",
  number: "numbers",
  boolean: "yes-or-no values",
  object: "objects",
  array: "lists",
  null: "empty values",
};

/** A plain-language description of a schema's type for suggestion badges. */
export function typeLabel(schema: Schema, root: Schema = schema): string {
  const resolved = resolveRef(schema, root);
  if ("const" in resolved) return "Fixed value";
  if (Array.isArray(resolved["enum"])) return "Choice";
  const types = schemaTypes(resolved, root);
  if (!types.length) return "Any value";
  if (
    types.includes("string") &&
    resolved["format"] === "date-time" &&
    types.length === 1
  )
    return "Date and time";
  const named = types
    .filter((t) => t !== "null" || types.length === 1)
    .map((t) => {
      if (t !== "array") return singular[t] ?? t;
      const items = isObject(resolved["items"])
        ? schemaTypes(resolved["items"], root)
        : [];
      return items.length === 1 && plural[items[0]]
        ? `List of ${plural[items[0]]}`
        : "List";
    });
  const label = named
    .map((text, i) => (i ? text.toLowerCase() : text))
    .join(" or ");
  return types.includes("null") && types.length > 1
    ? `${label} or empty`
    : label;
}

/**
 * A light check of whether a value of `source` fits `target`: type sets and
 * enum containment only. The compiler's type check stays authoritative.
 */
export function compatibility(
  source: Schema,
  target: Schema,
): "compatible" | "incompatible" | "unknown" {
  const from = schemaTypes(source);
  const to = schemaTypes(target);
  if (!from.length || !to.length) return "unknown";
  const fits = (type: string) =>
    to.includes(type) || (type === "integer" && to.includes("number"));
  const overlaps = (type: string) =>
    fits(type) || (type === "number" && to.includes("integer"));
  if (!from.some(overlaps)) return "incompatible";
  if (!from.every(fits)) return "unknown";
  const allowed = target["enum"];
  if (Array.isArray(allowed)) {
    const values = Array.isArray(source["enum"])
      ? (source["enum"] as unknown[])
      : "const" in source
        ? [source["const"]]
        : null;
    if (!values) return "unknown";
    const key = (v: unknown) => JSON.stringify(v);
    const permitted = new Set(allowed.map(key));
    if (values.every((v) => permitted.has(key(v)))) return "compatible";
    return values.some((v) => permitted.has(key(v)))
      ? "unknown"
      : "incompatible";
  }
  return "compatible";
}

class Walker {
  capture: Capture | null = null;
  constructor(
    private readonly target: Target,
    private readonly input: Schema,
    private readonly options: ScopeOptions,
  ) {}

  private take(visible: Map<string, VisibleStep>, evaluated: boolean) {
    if (this.capture) return;
    this.capture = { visible: new Map(visible), evaluated };
  }

  /** Mirrors `_Analyzer.block`: returns the scope after `steps` and whether they complete. */
  block(
    steps: unknown,
    incoming: Map<string, VisibleStep>,
  ): { visible: Map<string, VisibleStep>; completes: boolean } {
    const visible = new Map(incoming);
    let reachable = true;
    for (const step of Array.isArray(steps) ? steps : []) {
      if (!isObject(step) || typeof step["id"] !== "string") continue;
      const id = step["id"];
      const kind = String(step["kind"]);
      if (this.target.kind === "step" && this.target.stepId === id)
        this.take(visible, true);
      let schema: Schema = { type: "null" };
      let completes = kind !== "fail";
      if (kind === "transform") schema = this.infer(step["value"], visible);
      else if (kind === "action")
        schema = schemaOf(
          typeof step["uses"] === "string"
            ? this.options.actionOutput?.(step["uses"])
            : undefined,
        );
      else if (kind === "humanTask")
        schema = {
          type: "object",
          properties: {
            decision: {
              type: "string",
              enum: Array.isArray(step["decisions"]) ? step["decisions"] : [],
            },
            data: schemaOf(step["formSchema"]),
          },
          required: ["decision", "data"],
          additionalProperties: false,
        };
      else if (kind === "signal") schema = schemaOf(step["payloadSchema"]);
      else if (kind === "switch" || kind === "parallel") {
        const outputs: { name: string; schema: Schema; completes: boolean }[] =
          [];
        for (const [name, label, branch] of branchesOf(step)) {
          const result = this.block(branch["steps"], visible);
          if (
            this.target.kind === "branch" &&
            this.target.stepId === id &&
            this.target.branch === name
          )
            this.take(result.visible, result.completes);
          outputs.push({
            name: label,
            completes: result.completes,
            schema: result.completes
              ? this.infer(branch["output"], result.visible)
              : {},
          });
        }
        if (kind === "parallel") {
          schema = closedObject(
            Object.fromEntries(outputs.map((o) => [o.name, o.schema])),
          );
          completes = outputs.every((o) => o.completes);
        } else {
          const alternatives = outputs
            .filter((o) => o.completes)
            .map((o) => o.schema);
          schema =
            alternatives.length === 1
              ? alternatives[0]
              : alternatives.length
                ? { anyOf: alternatives }
                : {};
          completes = alternatives.length > 0;
        }
      }
      if (reachable && completes) visible.set(id, { id, kind, schema });
      reachable = reachable && completes;
    }
    return { visible, completes: reachable };
  }

  /** The shape the compiler infers for simple expressions; `{}` otherwise. */
  infer(expression: unknown, visible: Map<string, VisibleStep>): Schema {
    if (!isObject(expression)) return {};
    if ("literal" in expression) return literalSchema(expression["literal"]);
    if (isObject(expression["object"]))
      return closedObject(
        Object.fromEntries(
          Object.entries(expression["object"]).map(([k, v]) => [
            k,
            this.infer(v, visible),
          ]),
        ),
      );
    if (Array.isArray(expression["array"])) return { type: "array" };
    if (isObject(expression["op"])) {
      const name = expression["op"]["name"];
      return name === "coalesce" ? {} : { type: "boolean" };
    }
    if (typeof expression["ref"] === "string") {
      const [head, id, output, ...rest] = segmentsOf(expression["ref"]);
      if (head === "input")
        return schemaAt(this.input, [id, output, ...rest].filter(defined));
      if (head === "steps" && output === "output" && visible.has(id!))
        return schemaAt(visible.get(id!)!.schema, rest);
    }
    return {};
  }
}
const defined = (value: string | undefined): value is string =>
  value !== undefined;

function schemaAt(schema: Schema, path: string[]): Schema {
  let current = schema;
  for (const key of path) {
    const property = objectProperties(current, schema)?.get(key);
    if (!property) return {};
    current = resolveRef(property.schema, schema);
  }
  return current;
}

/** `[target name, output field name, branch]` in the analyzer's terms. */
function branchesOf(step: JsonObject): [string, string, JsonObject][] {
  if (step["kind"] === "switch")
    return [
      ...(Array.isArray(step["cases"]) ? step["cases"] : []).map(
        (c, i): [string, string, JsonObject] => [
          `case:${i}`,
          `case:${i}`,
          schemaOf(c),
        ],
      ),
      ["default", "default", schemaOf(step["default"])],
    ];
  if (step["kind"] === "parallel" && isObject(step["branches"]))
    return Object.entries(step["branches"]).map(
      ([name, b]): [string, string, JsonObject] => [
        `branch:${name}`,
        name,
        schemaOf(b),
      ],
    );
  return [];
}

function stepsAndInput(definition: unknown) {
  const spec = isObject(definition) ? definition["spec"] : undefined;
  return {
    steps: isObject(spec) ? spec["steps"] : [],
    input: schemaOf(isObject(spec) ? spec["inputSchema"] : undefined),
  };
}

function walk(
  definition: unknown,
  stepId: string,
  fieldPath: string,
  options: ScopeOptions,
): { capture: Capture | null; input: Schema } {
  const target = targetOf(stepId, fieldPath);
  const { steps, input } = stepsAndInput(definition);
  if (!target) return { capture: null, input };
  const walker = new Walker(target, input, options);
  const result = walker.block(steps, new Map());
  if (target.kind === "workflow")
    return {
      capture: { visible: result.visible, evaluated: result.completes },
      input,
    };
  return { capture: walker.capture, input };
}

/**
 * Steps whose output may be referenced at `fieldPath` of `stepId`, in
 * execution order, or `null` when that position does not exist. `fieldPath`
 * is a pointer relative to the step (`/with`, `/cases/0/when`,
 * `/branches/a/output`), or `/spec/output` with `WORKFLOW`.
 */
export function visibleSteps(
  definition: unknown,
  stepId: string,
  fieldPath: string,
  options: ScopeOptions = {},
): VisibleStep[] | null {
  const { capture } = walk(definition, stepId, fieldPath, options);
  return capture ? [...capture.visible.values()] : null;
}

/** The full typed scope at one expression position. */
export function referenceScope(
  definition: unknown,
  stepId: string,
  fieldPath: string,
  options: ScopeOptions = {},
): ReferenceScope {
  const { capture, input } = walk(definition, stepId, fieldPath, options);
  if (!capture)
    return {
      found: false,
      evaluated: false,
      steps: [],
      entries: [],
      truncated: false,
    };
  const steps = [...capture.visible.values()];
  const { entries, truncated } = expand(input, steps, options);
  return {
    found: true,
    evaluated: capture.evaluated,
    steps,
    entries,
    truncated,
  };
}

/** Typed reference suggestions at one expression position. */
export function visibleRefs(
  definition: unknown,
  stepId: string,
  fieldPath: string,
  options: ScopeOptions = {},
): ScopeEntry[] {
  return referenceScope(definition, stepId, fieldPath, options).entries;
}

interface Pending {
  entry: ScopeEntry;
  order: number[];
  root: Schema;
}

function expand(
  input: Schema,
  steps: VisibleStep[],
  options: ScopeOptions,
): { entries: ScopeEntry[]; truncated: boolean } {
  const maxDepth = Math.max(0, options.maxDepth ?? 4);
  const maxEntries = Math.max(1, options.maxEntries ?? 200);
  const make = (
    base: Omit<ScopeEntry, "types" | "typeLabel">,
    root: Schema,
  ): ScopeEntry => ({
    ...base,
    types: schemaTypes(base.schema, root),
    typeLabel: typeLabel(base.schema, root),
  });
  const roots: Pending[] = [
    {
      root: input,
      order: [0],
      entry: make(
        {
          ref: "/input",
          source: "input",
          path: [],
          label: "Workflow input",
          breadcrumb: "Workflow input",
          schema: input,
          optional: false,
        },
        input,
      ),
    },
    ...steps.map((step, i) => ({
      root: step.schema,
      order: [i + 1],
      entry: make(
        {
          ref: `/steps/${escapeSegment(step.id)}/output`,
          source: "step" as const,
          stepId: step.id,
          stepKind: step.kind,
          path: [],
          label: step.id,
          breadcrumb: step.id,
          schema: step.schema,
          optional: false,
        },
        step.schema,
      ),
    })),
  ];
  let truncated = roots.length > maxEntries;
  const accepted: Pending[] = roots.slice(0, maxEntries);
  let level = accepted.slice();
  for (
    let depth = 1;
    depth <= maxDepth && level.length && !truncated;
    depth++
  ) {
    const next: Pending[] = [];
    for (const parent of level) {
      const properties = objectProperties(
        resolveRef(parent.entry.schema, parent.root),
        parent.root,
      );
      let index = 0;
      for (const [key, property] of properties ?? []) {
        if (accepted.length >= maxEntries) {
          truncated = true;
          break;
        }
        const schema = resolveRef(property.schema, parent.root);
        const title =
          typeof schema["title"] === "string" && schema["title"].trim()
            ? schema["title"].trim()
            : key;
        const nullable = parent.entry.types.includes("null");
        const child: Pending = {
          root: parent.root,
          order: [...parent.order, index++],
          entry: make(
            {
              ...parent.entry,
              ref: `${parent.entry.ref}/${escapeSegment(key)}`,
              path: [...parent.entry.path, key],
              label: title,
              breadcrumb: `${parent.entry.breadcrumb} › ${title}`,
              schema,
              optional: parent.entry.optional || nullable || !property.required,
            },
            parent.root,
          ),
        };
        accepted.push(child);
        next.push(child);
      }
      if (truncated) break;
    }
    level = next;
  }
  accepted.sort((a, b) => {
    for (let i = 0; i < Math.min(a.order.length, b.order.length); i++)
      if (a.order[i] !== b.order[i]) return a.order[i] - b.order[i];
    return a.order.length - b.order.length;
  });
  return { entries: accepted.map((p) => p.entry), truncated };
}
