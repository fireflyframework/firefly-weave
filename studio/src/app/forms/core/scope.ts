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
// - `fail` (and a group whose paths cannot complete) ends reachability;
// - a step that handles its failures (On error Continue or Continue with
//   error output) may hold an error after it, `/steps/<id>/error`; inside
//   its error path the error is always there and the step has no output
//   (compiler/step_errors.py, analyzer `error_handling`);
// - a transaction's steps see what it sees and the earlier ones among them;
//   after it, only its result is visible (analyzer `transaction_block`);
// - a step's undo sees what the step saw, its operation key, its outcome and
//   its transaction, and the step's own output: always there in a confirm,
//   maybe empty in a reverse, never in a release (analyzer `undo_phase`).
// The compiler stays authoritative; this module only drives suggestions.

import { callOutputSchema } from "./call-output";
import { typeLabels, pluralTypeLabels } from "./type-labels";

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
  /** "error": a failed step's error, `/steps/<id>/error`; "undo": what only a step's undo reads. */
  source: "input" | "step" | "error" | "undo";
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
  decisionOutput?: (uses: string) => unknown;
  /** Output schema of a called workflow version by its `uses` reference. */
  workflowOutput?: (uses: string) => unknown;
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

/** The steps of a step's undo, as the language names them. */
export type UndoPhase = "confirm" | "cancel" | "compensate";
const UNDO_PHASES: readonly string[] = ["confirm", "cancel", "compensate"];
/** A step's undo around a field: the step, and which of its steps the field is in. */
export interface UndoPlace {
  step: string;
  phase: UndoPhase;
}

type Target =
  | { stepId: string; kind: "step" }
  | { stepId: string; kind: "undo"; phase: UndoPhase }
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
  if (segments[0] === "body" && segments[1] === "output")
    return { stepId, kind: "branch", branch: "body" };
  if (segments[0] === "errorPath" && segments[1] === "output")
    return { stepId, kind: "branch", branch: "errorPath" };
  // Only a transaction has a result of its own steps.
  if (segments[0] === "output")
    return { stepId, kind: "branch", branch: "steps" };
  if (UNDO_PHASES.includes(segments[0]) && segments[1] === "with")
    return { stepId, kind: "undo", phase: segments[0] as UndoPhase };
  return { stepId, kind: "step" };
}

/** A step whose error a position may read; `always` inside its own error path. */
interface ErrorRoot {
  id: string;
  kind: string;
  always: boolean;
}
interface Capture {
  visible: Map<string, VisibleStep>;
  evaluated: boolean;
  /** Inside error paths first (outermost first), then handled steps in the order they run. */
  errors: ErrorRoot[];
  /** Inside a step's undo. */
  undo: UndoPlace | null;
}

/** The error object of a step that handled its failure (contracts/step_errors.py). */
export const STEP_ERROR_SCHEMA: Schema = {
  type: "object",
  properties: {
    code: { type: "string", minLength: 1, maxLength: 128 },
    message: { type: "string", minLength: 1, maxLength: 512 },
    outcome: { type: "string", enum: ["not_started", "failed", "unknown"] },
    attempts: { type: "integer", minimum: 0 },
    instanceKey: { type: "string", minLength: 1 },
    childRunId: { type: "string", format: "uuid" },
  },
  required: ["code", "message", "outcome", "attempts", "instanceKey"],
  additionalProperties: false,
};
const NULL_SCHEMA: Schema = { type: "null" };
/** What only a step's undo reads (analyzer PARTICIPANT_SCHEMA and TRANSACTION_SCHEMA). */
const undoRoots = (step: string): [string, string, Schema][] => [
  ["/participant/operationKey", "Operation key", { type: "string" }],
  [
    "/participant/outcome",
    `Outcome of ${step}`,
    { type: "string", enum: ["done", "unknown"] },
  ],
  ["/transaction/id", "Transaction ID", { type: "string" }],
  ["/transaction/businessKey", "Business key", { type: ["string", "null"] }],
];
const empty = (schema: Schema) => Object.keys(schema).length === 0;

/** Literal JSON → the schema the compiler would infer for its shape. */
function literalSchema(value: unknown, depth = 0): Schema {
  if (value === null) return { type: "null", const: null };
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

/**
 * The schema of one key or array index below `schema`, or why there is none:
 * `absent` only when an object schema closes its properties.
 */
export function schemaChild(
  schema: Schema,
  root: Schema,
  segment: string,
):
  | { kind: "found"; schema: Schema }
  | { kind: "absent" }
  | { kind: "unknown" } {
  const resolved = resolveRef(schema, root);
  const items = resolved["items"];
  if (/^(?:0|[1-9][0-9]*)$/.test(segment) && isObject(items))
    return { kind: "found", schema: items };
  const properties = objectProperties(resolved, root);
  const property = properties?.get(segment);
  if (property) return { kind: "found", schema: property.schema };
  return properties !== null && resolved["additionalProperties"] === false
    ? { kind: "absent" }
    : { kind: "unknown" };
}

/** Every schema that may describe a value: the schema, its $ref target and each anyOf, oneOf and allOf part. */
function describing(schemas: readonly Schema[], root: Schema): Schema[] {
  const out: Schema[] = [];
  const seen = new Set<unknown>();
  const queue = [...schemas];
  while (queue.length && out.length < 256) {
    const schema = queue.shift()!;
    if (seen.has(schema)) continue;
    seen.add(schema);
    out.push(schema);
    const resolved = resolveRef(schema, root);
    if (resolved !== schema) queue.push(resolved);
    for (const word of ["anyOf", "oneOf", "allOf"]) {
      const parts = schema[word];
      if (Array.isArray(parts))
        for (const part of parts) if (isObject(part)) queue.push(part);
    }
  }
  return out;
}

const own = (record: Record<string, unknown>, key: string) =>
  Object.prototype.hasOwnProperty.call(record, key);

/** Every schema that may describe one key or array index of a value `schema` describes. */
function childSchemas(schema: Schema, segment: string): Schema[] {
  const out: Schema[] = [];
  const properties = isObject(schema["properties"]) ? schema["properties"] : {};
  const named = own(properties, segment);
  if (named && isObject(properties[segment])) out.push(properties[segment]);
  let patterned = false;
  if (isObject(schema["patternProperties"]))
    for (const [pattern, child] of Object.entries(
      schema["patternProperties"],
    )) {
      let matches = false;
      try {
        matches = new RegExp(pattern, "u").test(segment);
      } catch {
        // A pattern JavaScript can't read may still match: count it.
        matches = true;
      }
      if (matches && isObject(child)) {
        out.push(child);
        patterned = true;
      }
    }
  if (!named && !patterned && isObject(schema["additionalProperties"]))
    out.push(schema["additionalProperties"]);
  if (/^(?:0|[1-9][0-9]*)$/.test(segment)) {
    const index = Number(segment);
    const prefix = Array.isArray(schema["prefixItems"])
      ? schema["prefixItems"]
      : Array.isArray(schema["items"])
        ? schema["items"]
        : [];
    if (index < prefix.length) {
      if (isObject(prefix[index])) out.push(prefix[index]);
    } else {
      if (isObject(schema["items"])) out.push(schema["items"]);
      if (isObject(schema["additionalItems"]))
        out.push(schema["additionalItems"]);
    }
  }
  return out;
}

/**
 * True when the value at `segments` below `schema`, or a value around it, is
 * marked secret by any schema that may describe it: through $ref, every
 * anyOf, oneOf and allOf part, additionalProperties, patternProperties,
 * prefixItems and items.
 */
export function secretAt(
  schema: Schema,
  root: Schema,
  segments: readonly string[],
): boolean {
  let level: Schema[] = [schema];
  for (let i = 0; ; i++) {
    const all = describing(level, root);
    if (
      all.some(
        (node) => node["x-secret"] === true || node["writeOnly"] === true,
      )
    )
      return true;
    if (i === segments.length) return false;
    level = all.flatMap((node) => childSchemas(node, segments[i]));
    if (!level.length) return false;
  }
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

/** A plain-language description of a schema's type for suggestion badges. */
export function typeLabel(schema: Schema, root: Schema = schema): string {
  const resolved = resolveRef(schema, root);
  if ("const" in resolved) return typeLabels["fixed"];
  if (Array.isArray(resolved["enum"])) return typeLabels["choice"];
  const types = schemaTypes(resolved, root);
  if (!types.length) return typeLabels["any"];
  if (
    types.includes("string") &&
    resolved["format"] === "date-time" &&
    types.length === 1
  )
    return typeLabels["date-time"];
  const named = types
    .filter((t) => t !== "null" || types.length === 1)
    .map((t) => {
      if (t !== "array") return typeLabels[t] ?? t;
      const items = isObject(resolved["items"])
        ? schemaTypes(resolved["items"], root)
        : [];
      return items.length === 1 && pluralTypeLabels[items[0]]
        ? `List of ${pluralTypeLabels[items[0]]}`
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
  /** Every step's inferred output schema, reachable or not (stepOutputSchema). */
  readonly outputs = new Map<string, Schema>();
  constructor(
    private readonly target: Target,
    private readonly input: Schema,
    private readonly options: ScopeOptions,
    private readonly profiles: JsonObject = {},
  ) {}

  /** The steps whose error paths enclose the walk now, outermost first. */
  private errorOwners: { id: string; kind: string }[] = [];
  /** Steps that handle their failures (Continue, Continue with error output). */
  private readonly handled = new Set<string>();

  /** Mirrors `_Analyzer.observe`: the scope here, with the errors it may read. */
  take(
    visible: Map<string, VisibleStep>,
    evaluated: boolean,
    undo: UndoPlace | null = null,
  ) {
    if (this.capture) return;
    this.capture = {
      visible: new Map(visible),
      evaluated,
      undo,
      errors: [
        ...this.errorOwners
          .filter(({ id }) => !visible.has(id))
          .map(({ id, kind }) => ({ id, kind, always: true })),
        ...[...visible.values()]
          .filter(({ id }) => this.handled.has(id))
          .map(({ id, kind }) => ({ id, kind, always: false })),
      ],
    };
  }

  /**
   * Mirrors `_Analyzer.error_handling`: the output later steps see after a
   * step that handles its failures. Continue makes it null too; an error path
   * runs with the step's error and no output of its own, and its result
   * joins the step's output unless that already holds it.
   */
  private handle(
    step: JsonObject,
    kind: string,
    output: Schema,
    visible: Map<string, VisibleStep>,
  ): Schema {
    const id = String(step["id"]);
    const policy = step["onError"];
    if (policy !== "continue" && policy !== "errorOutput") return output;
    if (policy === "continue") {
      this.handled.add(id);
      return empty(output) ? output : { anyOf: [output, NULL_SCHEMA] };
    }
    const path = schemaOf(step["errorPath"]);
    const outer = this.errorOwners;
    this.errorOwners = [...outer, { id, kind }];
    let result: Schema = {};
    let completes: boolean;
    try {
      const inner = this.block(path["steps"], visible);
      completes = inner.completes;
      if (
        this.target.kind === "branch" &&
        this.target.stepId === id &&
        this.target.branch === "errorPath"
      )
        this.take(inner.visible, completes);
      if (completes) result = this.infer(path["output"], inner.visible);
    } finally {
      this.errorOwners = outer;
    }
    this.handled.add(id);
    const contained =
      !completes ||
      (!empty(output) &&
        !empty(result) &&
        compatibility(result, output) === "compatible");
    if (contained || empty(output)) return output;
    return empty(result) ? {} : { anyOf: [output, result] };
  }

  /**
   * Mirrors `_Analyzer.undo_phase`: the scope inside a step's undo, with the
   * step's own output where it may be read. A release runs without it.
   */
  private takeUndo(
    own: VisibleStep,
    phase: UndoPhase,
    visible: Map<string, VisibleStep>,
  ) {
    const scope = new Map(visible);
    if (phase !== "cancel")
      scope.set(own.id, {
        ...own,
        schema:
          phase === "compensate"
            ? { anyOf: [own.schema, NULL_SCHEMA] }
            : own.schema,
      });
    this.take(scope, true, { step: own.id, phase });
  }

  /** Whether `/steps/<id>/error` may be read here: inside its error path, or after a step that handles failures. */
  private errorReadable(id: string, visible: Map<string, VisibleStep>) {
    return (
      (this.errorOwners.some((owner) => owner.id === id) && !visible.has(id)) ||
      (visible.has(id) && this.handled.has(id))
    );
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
      else if (kind === "action" || kind === "decisionTable")
        schema = schemaOf(
          typeof step["uses"] === "string"
            ? (kind === "decisionTable"
                ? this.options.decisionOutput
                : this.options.actionOutput)?.(step["uses"])
            : undefined,
        );
      else if (kind === "callWorkflow") {
        const called =
          typeof step["uses"] === "string"
            ? this.options.workflowOutput?.(step["uses"])
            : undefined;
        schema = callOutputSchema(
          step["mode"],
          step["onFailure"],
          isObject(called) ? called : null,
        );
      } else if (kind === "llm") {
        const profile = this.profiles[String(step["profile"] ?? "")];
        schema = {
          type: "object",
          properties: {
            result: schemaOf(
              isObject(profile) ? profile["outputSchema"] : undefined,
            ),
            usage: { type: "object" },
            provider: { type: "string" },
            model: { type: "string" },
          },
          required: ["result", "usage", "provider", "model"],
        };
      } else if (kind === "humanTask")
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
      else if (kind === "forEach") {
        // Mirrors `_Analyzer.loop`: the body sees the steps before the loop,
        // its result per item the whole body; the loop itself completes.
        const body = schemaOf(step["body"]);
        const result = this.block(body["steps"], visible);
        if (
          this.target.kind === "branch" &&
          this.target.stepId === id &&
          this.target.branch === "body"
        )
          this.take(result.visible, result.completes);
        schema = loopOutput(
          result.completes ? this.infer(body["output"], result.visible) : {},
          step["maxItems"],
          step["collect"],
        );
      } else if (kind === "transaction") {
        // Mirrors `_Analyzer.transaction_block`: its result reads its own
        // steps, which nothing after it does.
        const result = this.block(step["steps"], visible);
        if (
          this.target.kind === "branch" &&
          this.target.stepId === id &&
          this.target.branch === "steps"
        )
          this.take(result.visible, result.completes);
        schema = result.completes
          ? this.infer(step["output"], result.visible)
          : {};
        completes = result.completes;
      } else if (kind === "switch" || kind === "parallel") {
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
      if (this.target.kind === "undo" && this.target.stepId === id)
        this.takeUndo({ id, kind, schema }, this.target.phase, visible);
      schema = this.handle(step, kind, schema, visible);
      this.outputs.set(id, schema);
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
      if (
        head === "steps" &&
        output === "error" &&
        this.errorReadable(id!, visible)
      )
        return schemaAt(STEP_ERROR_SCHEMA, rest);
    }
    return {};
  }
}
const defined = (value: string | undefined): value is string =>
  value !== undefined;

/**
 * A loop's output, as `loops.output_schema` writes it: one result per item
 * in item order, up to its Up to; "Skip empty results" leaves the nulls out,
 * and a result that is only ever null leaves an empty list.
 */
function loopOutput(each: Schema, maxItems: unknown, collect: unknown): Schema {
  const most = typeof maxItems === "number" ? maxItems : 1000;
  const items = collect === "nonNull" ? withoutNull(each) : each;
  return items
    ? { type: "array", items, maxItems: most }
    : { type: "array", maxItems: 0 };
}

/**
 * A schema without null, as the compiler's `_nonnull` narrows it: its const,
 * else its enum, else its type; null when only null is left, and any value
 * (`{}`) when its types aren't listed directly.
 */
function withoutNull(schema: Schema): Schema | null {
  if ("const" in schema) return schema["const"] === null ? null : schema;
  if ("enum" in schema) {
    const values = Array.isArray(schema["enum"])
      ? schema["enum"].filter((value) => value !== null)
      : [];
    return values.length ? { ...schema, enum: values } : null;
  }
  const type = schema["type"];
  if (type === "null") return null;
  if (Array.isArray(type)) {
    const kinds = type.filter((name) => name !== "null");
    if (!kinds.length) return null;
    return { ...schema, type: kinds.length === 1 ? kinds[0] : kinds };
  }
  return typeof type === "string" ? schema : {};
}

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
  const spec = isObject(definition) ? definition["spec"] : undefined;
  const profiles =
    isObject(spec) && isObject(spec["llmProfiles"]) ? spec["llmProfiles"] : {};
  const walker = new Walker(target, input, options, profiles);
  const result = walker.block(steps, new Map());
  if (target.kind === "workflow") walker.take(result.visible, result.completes);
  return { capture: walker.capture, input };
}

/**
 * The output schema the compiler infers for one step, wherever it is nested,
 * or null when the definition has no step with that ID. `{}` means any value.
 */
export function stepOutputSchema(
  definition: unknown,
  stepId: string,
  options: ScopeOptions = {},
): Schema | null {
  const { steps, input } = stepsAndInput(definition);
  const spec = isObject(definition) ? definition["spec"] : undefined;
  const profiles =
    isObject(spec) && isObject(spec["llmProfiles"]) ? spec["llmProfiles"] : {};
  const walker = new Walker(
    { stepId: WORKFLOW, kind: "workflow" },
    input,
    options,
    profiles,
  );
  walker.block(steps, new Map());
  return walker.outputs.get(stepId) ?? null;
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
  const { entries, truncated } = expand(
    input,
    steps,
    options,
    capture.errors,
    capture.undo,
  );
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
  errors: readonly ErrorRoot[] = [],
  undo: UndoPlace | null = null,
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
  // A failed step's error, as scope_at's `_error_root` names it.
  const errorRoot = ({ id, kind, always }: ErrorRoot): ScopeEntry =>
    make(
      {
        ref: `/steps/${escapeSegment(id)}/error`,
        source: "error",
        stepId: id,
        stepKind: kind,
        path: [],
        label: "Error",
        breadcrumb: `${id} › Error`,
        schema: STEP_ERROR_SCHEMA,
        optional: !always,
      },
      STEP_ERROR_SCHEMA,
    );
  const after = new Map(
    errors.filter(({ always }) => !always).map((error) => [error.id, error]),
  );
  const listed: { entry: ScopeEntry; root: Schema }[] = [
    {
      root: input,
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
    // Inside a step's undo, its operation key, its outcome and its transaction.
    ...(undo ? undoRoots(undo.step) : []).map(([ref, label, schema]) => ({
      root: schema,
      entry: make(
        {
          ref,
          source: "undo" as const,
          path: [],
          label,
          breadcrumb: `This step's undo › ${label}`,
          schema,
          optional: false,
        },
        schema,
      ),
    })),
    // Inside an error path, the failed step's error comes first: the step has no output there.
    ...errors
      .filter(({ always }) => always)
      .map((error) => ({ root: STEP_ERROR_SCHEMA, entry: errorRoot(error) })),
    ...steps.flatMap((step) => {
      const error = after.get(step.id);
      return [
        {
          root: step.schema,
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
              // A reverse also runs when its step's outcome is unknown.
              optional: undo?.phase === "compensate" && undo.step === step.id,
            },
            step.schema,
          ),
        },
        // After a step that handles its failures, its error may be there.
        ...(error
          ? [{ root: STEP_ERROR_SCHEMA, entry: errorRoot(error) }]
          : []),
      ];
    }),
  ];
  const roots: Pending[] = listed.map((item, i) => ({ ...item, order: [i] }));
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

/** The icon of a field's type, from its JSON types and its type label. */
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
