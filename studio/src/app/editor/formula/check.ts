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
// Warnings a formula gets from the schemas around its field: steps that
// aren't readable there, fields a closed schema doesn't have, and operand
// types that don't fit an operator. They never stop a formula from being
// written; the compiler stays the authority and its messages show on the
// same field.
import type {
  ReferenceScope,
  Schema,
  ScopeEntry,
} from "../../forms/core/scope";
import { schemaChild, schemaTypes } from "../../forms/core/scope";
import { typeLabels } from "../../forms/core/type-labels";
import { jsonType } from "./evaluate";
import { SYMBOLS } from "./grammar";
import { opName, printFormula, printRef } from "./printer";

export interface LoopEntry {
  ref: string;
  schema: Schema;
}
export interface CheckScope {
  /** What the field can read: visible steps and typed entries. */
  scope: ReferenceScope;
  /** Every step ID in the workflow, to tell later steps from unknown ones. */
  stepIds: ReadonlySet<string>;
  inputSchema: Schema;
  /** The current item, its position and enclosing loops, inside loop bodies. */
  loopEntries?: readonly (LoopEntry | ScopeEntry)[];
}
export interface FormulaWarning {
  code: "WV-FORMULA-SCOPE" | "WV-FORMULA-FIELD" | "WV-FORMULA-TYPE";
  message: string;
  /** The part of the tree the warning is about (the parser's spans place it). */
  node: object;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const segmentsOf = (pointer: string): string[] =>
  pointer === ""
    ? []
    : pointer
        .slice(1)
        .split("/")
        .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
const pointerOf = (segments: string[]) =>
  segments
    .map((segment) => `/${segment.replace(/~/g, "~0").replace(/\//g, "~1")}`)
    .join("");
const NUMERIC = new Set(["number", "integer"]);
const label = (types: string[]) => typeLabels[types[0]] ?? "Any value";
const quoted = (node: unknown) => {
  const text = printFormula(node) ?? "…";
  return `\`${text.length > 40 ? `${text.slice(0, 39)}…` : text}\``;
};

/** The schema a reference reads, walked from its root, or null when unknown. */
export function schemaOfRef(
  pointer: string,
  scope: CheckScope,
): { schema: Schema; root: Schema } | null {
  const segments = segmentsOf(pointer);
  let schema: Schema | null = null;
  let rest: string[] = [];
  if (segments[0] === "input") {
    schema = scope.inputSchema;
    rest = segments.slice(1);
  } else if (segments[0] === "steps" && segments[2] === "output") {
    schema =
      scope.scope.steps.find((step) => step.id === segments[1])?.schema ?? null;
    rest = segments.slice(3);
  } else if (scope.loopEntries) {
    for (let length = segments.length; length > 0 && !schema; length--) {
      const entry = scope.loopEntries.find(
        (item) => item.ref === pointerOf(segments.slice(0, length)),
      );
      if (entry) {
        schema = entry.schema;
        rest = segments.slice(length);
      }
    }
  }
  if (!schema) return null;
  const root = schema;
  for (const segment of rest) {
    const child = schemaChild(schema, root, segment);
    if (child.kind !== "found") return null;
    schema = child.schema;
  }
  return { schema, root };
}

/** JSON types a part of the formula may have, or null when unknown. */
export function typesOf(node: unknown, scope: CheckScope): string[] | null {
  if (!isRecord(node) || Object.keys(node).length !== 1) return null;
  if ("literal" in node) return [jsonType(node["literal"] as never)];
  if ("ref" in node && typeof node["ref"] === "string") {
    const found = schemaOfRef(node["ref"], scope);
    const types = found ? schemaTypes(found.schema, found.root) : [];
    return types.length ? types : null;
  }
  if ("object" in node) return ["object"];
  if ("array" in node) return ["array"];
  const name = opName(node);
  if (!name || name === "coalesce") return null;
  return name === "concat" || name === "join" ? ["string"] : ["boolean"];
}

function referenceWarning(
  node: { ref: string },
  scope: CheckScope,
): FormulaWarning | null {
  const segments = segmentsOf(node.ref);
  if (segments[0] === "steps" && segments.length >= 2) {
    const id = segments[1];
    const step = printRef(pointerOf(["steps", id]));
    if (!scope.scope.steps.some((visible) => visible.id === id))
      return {
        code: "WV-FORMULA-SCOPE",
        message: scope.stepIds.has(id)
          ? `\`${step}\` isn't available here: it runs later or on another path.`
          : `\`${step}\` isn't a step of this workflow.`,
        node,
      };
    if (segments.length >= 3 && segments[2] !== "output")
      return {
        code: "WV-FORMULA-FIELD",
        message: `\`${printRef(node.ref)}\` isn't part of a step's data; read \`${step}.output\`.`,
        node,
      };
  }
  const roots: Record<
    string,
    { schema: Schema | null; skip: number; owner: string }
  > = {
    input: { schema: scope.inputSchema, skip: 1, owner: "the workflow input" },
    steps: {
      schema:
        scope.scope.steps.find((step) => step.id === segments[1])?.schema ??
        null,
      skip: 3,
      owner: `${segments[1]}'s output`,
    },
    item: {
      schema:
        scope.loopEntries?.find((entry) => entry.ref === "/item")?.schema ??
        null,
      skip: 1,
      owner: "the current item",
    },
  };
  const root = roots[segments[0]];
  if (!root?.schema) return null;
  let schema = root.schema;
  for (let i = root.skip; i < segments.length; i++) {
    const child = schemaChild(schema, root.schema, segments[i]);
    if (child.kind === "absent")
      return {
        code: "WV-FORMULA-FIELD",
        message: `\`${printRef(pointerOf(segments.slice(0, i + 1)))}\` isn't a field of ${root.owner}.`,
        node,
      };
    if (child.kind === "unknown") return null;
    schema = child.schema;
  }
  return null;
}

function operatorWarning(
  name: string,
  args: unknown[],
  node: object,
  scope: CheckScope,
): FormulaWarning | null {
  if (name === "and" || name === "or" || name === "not") {
    for (const arg of args) {
      const types = typesOf(arg, scope);
      if (types && !types.includes("boolean"))
        return {
          code: "WV-FORMULA-TYPE",
          message: `\`${name}\` needs Yes/No values; ${quoted(arg)} is ${label(types)}.`,
          node: arg as object,
        };
    }
    return null;
  }
  if (["lt", "lte", "gt", "gte"].includes(name) && args.length === 2) {
    const [left, right] = args.map((arg) => typesOf(arg, scope));
    if (!left || !right) return null;
    const numbers =
      left.some((t) => NUMERIC.has(t)) && right.some((t) => NUMERIC.has(t));
    const texts = left.includes("string") && right.includes("string");
    if (numbers || texts) return null;
    return {
      code: "WV-FORMULA-TYPE",
      message: `\`${SYMBOLS[name]}\` needs two numbers or two texts; ${quoted(args[0])} is ${label(left)} and ${quoted(args[1])} is ${label(right)}.`,
      node,
    };
  }
  if ((name === "startsWith" || name === "endsWith") && args.length === 2)
    for (const arg of args) {
      const types = typesOf(arg, scope);
      if (types && !types.includes("string"))
        return {
          code: "WV-FORMULA-TYPE",
          message: `\`${name}\` needs text; ${quoted(arg)} is ${label(types)}.`,
          node: arg as object,
        };
    }
  return null;
}

/** Every warning for a parsed formula, in reading order. */
export function checkFormula(
  expression: unknown,
  scope: CheckScope,
): FormulaWarning[] {
  const found: FormulaWarning[] = [];
  // Past the depth workflows allow the compiler reports the tree; warnings
  // stop there instead of exhausting the stack.
  let depth = 0;
  const visit = (node: unknown): void => {
    if (depth >= 128 || !isRecord(node) || Object.keys(node).length !== 1)
      return;
    depth++;
    try {
      visitNode(node);
    } finally {
      depth--;
    }
  };
  const visitNode = (node: Record<string, unknown>): void => {
    if (typeof node["ref"] === "string") {
      const warning = referenceWarning(node as { ref: string }, scope);
      if (warning) found.push(warning);
      return;
    }
    if (isRecord(node["object"])) Object.values(node["object"]).forEach(visit);
    if (Array.isArray(node["array"])) node["array"].forEach(visit);
    const name = opName(node);
    if (!name) return;
    const args = (node["op"] as { args?: unknown }).args;
    if (!Array.isArray(args)) return;
    args.forEach(visit);
    const warning = operatorWarning(name, args, node, scope);
    if (warning) found.push(warning);
  };
  visit(expression);
  return found;
}
