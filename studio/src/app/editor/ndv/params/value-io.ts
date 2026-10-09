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
// What a step details field reads and writes. A field's path names a plain
// key of the step (`durationSeconds`), of the workflow (`metadata.name`) or
// of the workflow-owned action (`method`); a path inside one of the step's
// expression fields is a data path in that expression, read and written
// through the shape-preserving binding, so untouched parts keep their YAML.
import {
  decode,
  encode,
  get,
  literalValue,
  remove,
  set,
  value as literalBound,
  type Bound,
} from "../../../forms/core/binding";
import { getAt, isJsonObject, jsonEqual } from "../../../forms/core/json";
import type { Step, Workflow } from "../../../model";
import { deleteAt, writeAt } from "../edits";
import type { Expression, Json, ParamSpec, Path } from "../registry";
import { WORKFLOW_ROOTS, expressionRoot } from "./paths";

export type FormScope = "step" | "workflow" | "action";
/** What a form reads; `step` is null for the workflow settings. */
export interface FormSubject {
  step: Step | null;
  workflow: Workflow;
  /** The workflow-owned action's recipe, or null. */
  action: Json | null;
  /** The step's expression fields (its descriptor's `fields`). */
  roots: readonly Path[];
  /** The schema an expression field targets, to decode it by shape. */
  schemaOf?(root: Path): unknown;
}
/** A stored value: none, a fixed JSON value, or a mapping (any other expression). */
export type ParamValue =
  | { mode: "absent" }
  | { mode: "fixed"; value: Json }
  | { mode: "mapped"; expression: Expression };
/** One write; `value: undefined` deletes the key. */
export interface FormChange {
  scope: FormScope;
  path: Path;
  value: Json | Expression | undefined;
}
/** The keys or length of a structured value, or why it has none. */
export type Entries =
  | { kind: "absent" }
  | { kind: "object"; keys: string[] }
  | { kind: "array"; length: number }
  | { kind: "mapped"; expression: Expression };

export class FormWriteError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FormWriteError";
  }
}

export const ABSENT: ParamValue = { mode: "absent" };
export const fixed = (value: Json): ParamValue => ({ mode: "fixed", value });
export const mapped = (expression: Expression): ParamValue => ({
  mode: "mapped",
  expression,
});

type Located =
  | { kind: "plain"; scope: FormScope; base: unknown; path: Path }
  | {
      kind: "expression";
      scope: FormScope;
      base: unknown;
      root: Path;
      rest: Path;
      schema: unknown;
    };

type ValueSpec = Pick<ParamSpec, "path" | "scope" | "referenceProjection">;

function locate(
  subject: FormSubject,
  spec: Pick<ParamSpec, "path" | "scope">,
): Located {
  const scope = spec.scope ?? "step";
  if (scope === "action")
    return { kind: "plain", scope, base: subject.action, path: spec.path };
  const base = scope === "workflow" ? subject.workflow : subject.step;
  const roots = scope === "workflow" ? WORKFLOW_ROOTS : subject.roots;
  const root = expressionRoot(spec.path, roots);
  if (!root) return { kind: "plain", scope, base, path: spec.path };
  return {
    kind: "expression",
    scope,
    base,
    root,
    rest: spec.path.slice(root.length),
    schema: subject.schemaOf?.(root),
  };
}

/** A decoded node as a stored value. */
export function fromBound(node: Bound): ParamValue {
  if (node.kind === "value") return fixed(node.value as Json);
  if (node.kind === "ref") return mapped({ ref: node.pointer });
  if (node.kind === "formula") return mapped(node.expression as Expression);
  const literal = literalValue(node);
  return literal === undefined
    ? mapped(encode(node) as Expression)
    : fixed(literal as Json);
}

function rootNode(
  located: Extract<Located, { kind: "expression" }>,
  spec: ValueSpec,
): Bound | undefined {
  const expression = getAt(located.base, located.root);
  if (expression === undefined) return undefined;
  const bound = decode(expression, located.schema);
  return located.rest.length &&
    bound.kind === "ref" &&
    bound.pointer === spec.referenceProjection?.pointer
    ? decode({ object: spec.referenceProjection.values }, located.schema)
    : bound;
}

function nodeAt(
  located: Extract<Located, { kind: "expression" }>,
  spec: ValueSpec,
): Bound | undefined {
  const bound = rootNode(located, spec);
  if (!bound) return undefined;
  return located.rest.length ? get(bound, located.rest) : bound;
}

export function readParam(subject: FormSubject, spec: ValueSpec): ParamValue {
  const located = locate(subject, spec);
  if (located.kind === "plain") {
    const value = getAt(located.base, located.path);
    return value === undefined ? ABSENT : fixed(value as Json);
  }
  const node = nodeAt(located, spec);
  return node ? fromBound(node) : ABSENT;
}

export function readEntries(subject: FormSubject, spec: ValueSpec): Entries {
  const located = locate(subject, spec);
  const node: Bound | undefined =
    located.kind === "plain"
      ? (() => {
          const value = getAt(located.base, located.path);
          return value === undefined ? undefined : literalBound(value);
        })()
      : nodeAt(located, spec);
  if (!node) return { kind: "absent" };
  if (node.kind === "object")
    return { kind: "object", keys: Object.keys(node.entries) };
  if (node.kind === "array")
    return { kind: "array", length: node.items.length };
  if (node.kind === "ref")
    return { kind: "mapped", expression: { ref: node.pointer } };
  if (node.kind === "formula")
    return { kind: "mapped", expression: node.expression as Expression };
  return { kind: "absent" };
}

const asJson = (next: ParamValue): Json | Expression | undefined =>
  next.mode === "absent"
    ? undefined
    : next.mode === "fixed"
      ? next.value
      : next.expression;

function asBound(next: ParamValue): Bound {
  if (next.mode === "fixed") return literalBound(next.value);
  if (next.mode === "mapped") return decode(next.expression);
  throw new TypeError("An absent value has no node.");
}

export function writeParam(
  subject: FormSubject,
  spec: ValueSpec,
  next: ParamValue,
): FormChange[] {
  // Removing what is already absent changes nothing: no key is created,
  // and a literal keeps its YAML.
  if (next.mode === "absent" && readParam(subject, spec).mode === "absent")
    return [];
  const located = locate(subject, spec);
  if (located.kind === "plain")
    return [{ scope: located.scope, path: located.path, value: asJson(next) }];
  const { scope, root, rest } = located;
  if (!rest.length)
    return [
      {
        scope,
        path: root,
        value:
          next.mode === "absent"
            ? undefined
            : next.mode === "fixed"
              ? { literal: next.value }
              : next.expression,
      },
    ];
  // A field editor can write its displayed value back on blur. Keep the whole
  // reference until the person actually changes a projected row.
  if (spec.referenceProjection && jsonEqual(readParam(subject, spec), next))
    return [];
  const bound: Bound = rootNode(located, spec) ?? {
    kind: "object",
    entries: {},
  };
  if (bound.kind !== "object" && bound.kind !== "array")
    throw new FormWriteError(
      "This input is mapped as a whole. Choose Map field by field first.",
    );
  const updated =
    next.mode === "absent"
      ? remove(bound, rest)
      : set(bound, rest, asBound(next));
  return [{ scope, path: root, value: encode(updated) as Expression }];
}

/** True when the field holds nothing, or exactly its default. */
export function isDefault(subject: FormSubject, spec: ParamSpec): boolean {
  const located = locate(subject, spec);
  if (
    spec.referenceProjection &&
    located.kind === "expression" &&
    located.rest.length
  ) {
    const bound = decode(getAt(located.base, located.root));
    if (
      bound.kind === "ref" &&
      bound.pointer === spec.referenceProjection.pointer
    )
      return true;
  }
  const current = readParam(subject, spec);
  if (current.mode === "absent") return true;
  if (current.mode === "mapped") return false;
  if (spec.default !== undefined) return jsonEqual(current.value, spec.default);
  // An emptied object or list (every row removed) counts as never added.
  return (
    (isJsonObject(current.value) && !Object.keys(current.value).length) ||
    (Array.isArray(current.value) && !current.value.length)
  );
}

/**
 * Back to the default: delete the key, or write the default where the field
 * can't be omitted (a required field, or one that asks for it with
 * `whenRemoved: "default"` because the language needs its key).
 */
export function resetChanges(
  subject: FormSubject,
  spec: ParamSpec,
): FormChange[] {
  const keep = spec.required || spec.whenRemoved === "default";
  return writeParam(
    subject,
    spec,
    keep && spec.default !== undefined ? fixed(spec.default) : ABSENT,
  );
}

/**
 * The subject after one change, so the next change is computed from it. The
 * writes are the saved edit's own (edits.ts), so the preview can't drift from
 * what is saved.
 */
export function applyChange(
  subject: FormSubject,
  change: FormChange,
): FormSubject {
  const write = (base: unknown): unknown => {
    if (change.value !== undefined)
      return writeAt(base ?? {}, change.path, change.value);
    // An empty path names nothing to delete.
    return change.path.length ? deleteAt(base, change.path) : base;
  };
  if (change.scope === "action")
    return { ...subject, action: write(subject.action) as Json };
  if (change.scope === "workflow")
    return { ...subject, workflow: write(subject.workflow) as Workflow };
  return {
    ...subject,
    step: subject.step ? (write(subject.step) as Step) : null,
  };
}
