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
// Try an input, in Studio: each rule's mark as you type, and the table's
// answer under its hit policy, as compiler/decision_tables.py
// `evaluate_decision_table` gives it. Schemas are not checked here: Weave's
// own check (the Studio host) does that a moment later and has the last word.
// The tests hold this module to cases the Python function answered.
import type { Json } from "../../forms/core/json";
import { evaluate, pointerSegments } from "../formula/evaluate";
import type { TableSpec } from "./rules-model";

export type RowMark = "match" | "no-match" | "error";
export type TableAnswer =
  | { ok: true; output: Json; matched: string[]; usedDefault: boolean }
  | { ok: false; code: string; path: string };
export interface TableTry {
  /** One per rule, each evaluated on its own. */
  marks: RowMark[];
  answer: TableAnswer;
}

const TEXT_OPERATORS = new Set(["concat", "join"]);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
class Refused extends Error {
  constructor(
    readonly code: string,
    readonly path: string,
  ) {
    super(code);
  }
}
const pointerChild = (parent: string, key: string): string =>
  `${parent}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;

/** decision_tables.py `validate_decision_expressions`: rules read only the input and use no text operators. */
function checkExpressions(spec: TableSpec): void {
  const roots: [unknown, string][] = spec.rules.flatMap((rule, index) => [
    [rule.when, `/spec/rules/${index}/when`] as [unknown, string],
    [rule.output, `/spec/rules/${index}/output`] as [unknown, string],
  ]);
  if (spec.defaultOutput !== undefined)
    roots.push([spec.defaultOutput, "/spec/defaultOutput"]);
  for (const [expression, path] of roots) {
    const pending: [unknown, string][] = [[expression, path]];
    while (pending.length) {
      const [current, location] = pending.pop()!;
      if (!isRecord(current)) continue;
      if ("ref" in current) {
        let segments: string[];
        try {
          segments = pointerSegments(current["ref"], location + "/ref");
        } catch {
          throw new Refused("WV-EXPR-INVALID", location + "/ref");
        }
        if (!segments.length || segments[0] !== "input")
          throw new Refused("WV-DECISION-REFERENCE", location + "/ref");
      } else if (isRecord(current["object"])) {
        for (const [key, value] of Object.entries(current["object"]))
          pending.push([value, pointerChild(location + "/object", key)]);
      } else if (Array.isArray(current["array"])) {
        current["array"].forEach((value, i) =>
          pending.push([value, `${location}/array/${i}`]),
        );
      } else if (isRecord(current["op"])) {
        const operation = current["op"];
        if (TEXT_OPERATORS.has(String(operation["name"])))
          throw new Refused("WV-DECISION-OPERATOR", location + "/op/name");
        if (Array.isArray(operation["args"]))
          operation["args"].forEach((value, i) =>
            pending.push([value, `${location}/op/args/${i}`]),
          );
      }
    }
  }
}

function run(
  expression: unknown,
  context: Record<string, Json>,
  path: string,
): Json {
  const result = evaluate(expression, context);
  if (!result.ok) throw new Refused(result.code, path + result.path);
  return result.value;
}

function answerOf(spec: TableSpec, input: Json): TableAnswer {
  const ids = spec.rules.map((rule) => rule.id);
  if (
    !spec.rules.length ||
    new Set(ids).size !== ids.length ||
    (spec.hitPolicy === "collect" && spec.defaultOutput !== undefined)
  )
    return { ok: false, code: "WV-DECISION-CONTRACT", path: "/spec" };
  const context = { input };
  try {
    checkExpressions(spec);
    const matched: string[] = [];
    const outputs: Json[] = [];
    let selected: [unknown, string] | null = null;
    for (const [index, rule] of spec.rules.entries()) {
      const path = `/spec/rules/${index}`;
      const predicate = run(rule.when, context, path + "/when");
      if (typeof predicate !== "boolean")
        throw new Refused("WV-DECISION-PREDICATE", path + "/when");
      if (!predicate) continue;
      matched.push(rule.id);
      if (spec.hitPolicy === "unique" && matched.length > 1)
        throw new Refused("WV-DECISION-MULTIPLE_MATCHES", path + "/when");
      selected = [rule.output, path + "/output"];
      if (spec.hitPolicy === "first") break;
      if (spec.hitPolicy === "collect")
        outputs.push(run(rule.output, context, path + "/output"));
    }
    if (spec.hitPolicy === "collect")
      return { ok: true, output: outputs, matched, usedDefault: false };
    let usedDefault = false;
    if (!selected) {
      if (spec.defaultOutput === undefined)
        throw new Refused("WV-DECISION-NO_MATCH", "/spec");
      selected = [spec.defaultOutput, "/spec/defaultOutput"];
      usedDefault = true;
    }
    return {
      ok: true,
      output: run(selected[0], context, selected[1]),
      matched,
      usedDefault,
    };
  } catch (error) {
    if (error instanceof Refused)
      return { ok: false, code: error.code, path: error.path };
    throw error;
  }
}

/** Each rule's mark and the table's answer for one input. */
export function tryTable(spec: TableSpec, input: Json): TableTry {
  const context = { input };
  const marks = spec.rules.map((rule): RowMark => {
    const result = evaluate(rule.when, context);
    return !result.ok || typeof result.value !== "boolean"
      ? "error"
      : result.value
        ? "match"
        : "no-match";
  });
  return { marks, answer: answerOf(spec, input) };
}
