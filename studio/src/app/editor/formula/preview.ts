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
// Previews of a formula: what it resolves to with the data step details
// show (the trigger's test input today; step outputs once test data keeps
// them), computed in the browser by the evaluator port. Nothing is sent
// anywhere. A formula that reads a value marked secret is never evaluated,
// and the test input reaching here has had secret values removed already.
import { canonicalJson, type Json } from "../../forms/core/json";
import {
  secretAt,
  type ReferenceScope,
  type Schema,
} from "../../forms/core/scope";
import { typeLabels } from "../../forms/core/type-labels";
import type {
  Expression,
  NdvContext,
  Path,
  PreviewResult,
} from "../ndv/registry";
import type { PreviewHook } from "../ndv/params/preview";
import { AMBIGUOUS, evaluate, jsonType, type EvalFailure } from "./evaluate";
import { opName, printFormula, printRef } from "./printer";

export interface PreviewData {
  /** The workflow input as test data, secret values removed; undefined when there is none. */
  input(): Json | undefined;
  /** A step's output as test data, secret values removed; undefined while it has none. */
  output(stepId: string): Json | undefined;
  /** Changes whenever the data or the workflow changes. */
  stamp(): readonly unknown[];
}
export interface PreviewAt {
  scope: ReferenceScope;
  inputSchema: Schema;
  data: PreviewData;
}
export const WAITING = "WV-PREVIEW-WAITING";
export const SECRET = "WV-PREVIEW-SECRET";
export const UNAVAILABLE = "WV-PREVIEW-UNAVAILABLE";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const segmentsOf = (pointer: string): string[] =>
  pointer === ""
    ? []
    : pointer
        .slice(1)
        .split("/")
        .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
const NOUNS: Record<string, string> = {
  string: "text",
  number: "a number",
  integer: "a number",
  boolean: "a Yes/No value",
  null: "an empty value",
  array: "a list",
  object: "a group",
};
const noun = (type: string | undefined) => NOUNS[type ?? ""] ?? "a value";
const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const failed = (code: string, message: string): PreviewResult => ({
  ok: false,
  error: { code, message },
});

/** Every pointer the formula reads, in reading order. */
export function referencesOf(expression: unknown): string[] {
  const found: string[] = [];
  const visit = (node: unknown) => {
    if (Array.isArray(node)) node.forEach(visit);
    else if (isRecord(node)) {
      if (Object.keys(node).length === 1 && typeof node["ref"] === "string") {
        found.push(node["ref"]);
        return;
      }
      if (Object.keys(node).length === 1 && "literal" in node) return;
      Object.values(node).forEach(visit);
    }
  };
  visit(expression);
  return found;
}

function readsSecret(pointer: string, at: PreviewAt): boolean {
  const segments = segmentsOf(pointer);
  if (segments[0] === "input")
    return secretAt(at.inputSchema, at.inputSchema, segments.slice(1));
  if (segments[0] === "steps" && segments[2] === "output") {
    const schema = at.scope.steps.find(
      (step) => step.id === segments[1],
    )?.schema;
    return !!schema && secretAt(schema, schema, segments.slice(3));
  }
  return false;
}

function nodeAt(expression: unknown, path: string): unknown {
  let node = expression;
  for (const segment of segmentsOf(path)) {
    if (Array.isArray(node)) node = node[Number(segment)];
    else if (isRecord(node)) node = node[segment];
    else return undefined;
  }
  return node;
}

/** An evaluation error in plain words. */
export function wordsFor(failure: EvalFailure, expression: unknown): string {
  if (failure.code === AMBIGUOUS)
    return "Studio can't preview this value exactly.";
  const node = nodeAt(expression, failure.path);
  if (
    failure.code === "WV-EXPR-MISSING" &&
    isRecord(node) &&
    typeof node["ref"] === "string"
  )
    return `\`${printRef(node["ref"])}\` is missing in the test data.`;
  if (failure.code === "WV-EXPR-RESOURCE_LIMIT")
    return "This result is too large to preview.";
  if (failure.code === "WV-EXPR-INVALID_JSON")
    return "The test data holds a value Weave can't read.";
  if (failure.code !== "WV-EXPR-TYPE") return "This formula isn't complete.";
  const [first, second] = failure.types ?? [];
  const own = opName(node);
  if (own && ["lt", "lte", "gt", "gte"].includes(own))
    return `${capital(noun(first))} can't be compared with ${noun(second)}.`;
  if (own === "startsWith" || own === "endsWith")
    return `\`${own}\` needs text on both sides.`;
  if (own === "contains" || own === "notContains")
    return first === "string"
      ? `\`${own}\` needs text on both sides.`
      : `\`${own}\` needs a list or text first.`;
  if (own === "in" || own === "notIn") return `\`${own}\` needs a list second.`;
  const match = /^(.*)\/op\/args\/(\d+)$/.exec(failure.path);
  const parent = match ? opName(nodeAt(expression, match[1])) : null;
  const index = match ? Number(match[2]) : 0;
  if (parent === "and" || parent === "or" || parent === "not")
    return `\`${parent}\` needs Yes/No values; this one is ${noun(first)}.`;
  if (parent === "join" && index === 1)
    return "`join` needs text as the separator.";
  if (parent === "join")
    return first === "array"
      ? `\`join\` can't turn ${noun(second)} into text.`
      : "`join` needs a list first.";
  if (parent === "concat")
    return `\`concat\` can't turn ${noun(first)} into text.`;
  return `This formula can't use ${noun(first)} here.`;
}

/** A value on one line: text as it is, anything else as compact JSON; cut at `limit` characters. */
export function previewText(
  value: Json,
  limit = 200,
): { text: string; truncated: boolean } {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > limit
    ? { text: text.slice(0, limit), truncated: true }
    : { text, truncated: false };
}

export function valueTypeLabel(value: Json): string {
  return typeLabels[jsonType(value)] ?? typeLabels["any"];
}

/** Where a formula's data comes from, for the Result panel. */
export function sourceLabel(expression: unknown, data: PreviewData): string {
  return referencesOf(expression).some(
    (pointer) => segmentsOf(pointer)[0] === "input",
  ) && data.input() !== undefined
    ? "From test input"
    : "";
}

/** What a formula resolves to at a field, or why it can't be shown. */
export function previewFormula(
  expression: Expression,
  at: PreviewAt,
): PreviewResult {
  const pointers = referencesOf(expression);
  if (pointers.some((pointer) => readsSecret(pointer, at)))
    return failed(
      SECRET,
      "This formula reads a secret value, so Studio doesn't preview it.",
    );
  const visible = new Set(at.scope.steps.map((step) => step.id));
  const steps: Record<string, { output: Json }> = {};
  for (const pointer of pointers) {
    const [root, id] = segmentsOf(pointer);
    if (root === "input" && at.data.input() === undefined)
      return failed(
        WAITING,
        "Can't compute yet: there's no test input. Add it in the trigger's Test event.",
      );
    if (root === "steps" && id !== undefined) {
      const name = printRef(
        `/steps/${id.replace(/~/g, "~0").replace(/\//g, "~1")}`,
      );
      if (!visible.has(id))
        return failed(UNAVAILABLE, `\`${name}\` isn't available here.`);
      const output = at.data.output(id);
      if (output === undefined)
        return failed(WAITING, `Can't compute yet: ${name} has no data.`);
      steps[id] = { output };
    }
    if (root === "item" || root === "index" || root === "loops")
      return failed(
        WAITING,
        "Can't compute yet: the current item exists only while the loop runs.",
      );
  }
  const context: Record<string, Json> = { input: at.data.input() ?? {}, steps };
  const result = evaluate(expression, context);
  return result.ok
    ? { ok: true, value: result.value }
    : failed(result.code, wordsFor(result, expression));
}

/** Step details' preview hook: formulas evaluated in the browser, cached until the data or workflow changes. */
export function formulaPreviews(
  data: (ctx: NdvContext) => PreviewData,
): PreviewHook {
  let stamp: readonly unknown[] = [];
  const cache = new Map<string, PreviewResult>();
  return {
    preview(expression: Expression, at: Path, ctx: NdvContext): PreviewResult {
      const source = data(ctx);
      const next = [ctx.workflow, ...source.stamp()];
      if (
        next.length !== stamp.length ||
        next.some((item, i) => !Object.is(item, stamp[i]))
      ) {
        stamp = next;
        cache.clear();
      }
      const key = `${ctx.step.id}\n${JSON.stringify(at)}\n${canonicalJson(expression)}`;
      const cached = cache.get(key);
      if (cached) return cached;
      const result = previewFormula(expression, {
        scope: ctx.scope(at),
        inputSchema: ((ctx.workflow.spec as Record<string, unknown>)[
          "inputSchema"
        ] ?? {}) as Schema,
        data: source,
      });
      if (cache.size > 256) cache.clear();
      cache.set(key, result);
      return result;
    },
  };
}
