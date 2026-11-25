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
// The rules a single field checks itself (required, range, length, list
// size, a version's form), in one sentence each, and when they show: on a
// step inserted a moment ago, "required" waits until the field is left
// empty or everything is revealed (Execute step, opening from an issue);
// nothing else waits.
// Out-of-range values are stored and flagged, never clamped.
import { validVersion } from "../../../forms/core/identifiers";
import type { ParamSpec } from "../registry";
import type { Entries, ParamValue } from "./value-io";

export interface RuleProblem {
  code:
    | "required"
    | "range"
    | "length"
    | "items"
    | "positive"
    | "whole"
    | "format";
  message: string;
  severity: "error" | "warning";
}
export interface RuleTiming {
  /** The step was inserted in this session and not yet executed or revealed. */
  fresh: boolean;
  /** Fields the person left at least once. */
  touched: ReadonlySet<string>;
  /** Every message shows (opened from an issue, Execute step pressed). */
  revealAll: boolean;
}

export function requiredMessage(spec: ParamSpec): string {
  if (spec.requiredText) return spec.requiredText;
  switch (spec.type) {
    case "select":
    case "multiSelect":
    case "resource":
    case "model":
      return "Choose one.";
    case "connection":
      return "Choose a connection.";
    case "list":
    case "keyValue":
    case "fields":
      return "Add at least one.";
    case "schema":
      return "Add at least one field.";
    case "formula":
    case "fileRef":
      return "Map a value.";
    case "conditions":
      return "Choose when this path applies.";
    default:
      return spec.mapping === "both"
        ? "Enter a value or map one."
        : "Enter a value.";
  }
}

const empty = (value: ParamValue, entries: Entries): boolean => {
  if (value.mode === "absent") return true;
  if (value.mode === "mapped") return false;
  const v = value.value;
  if (v === "" || v === null) return true;
  if (entries.kind === "object") return entries.keys.length === 0;
  if (entries.kind === "array") return entries.length === 0;
  return false;
};

const error = (code: RuleProblem["code"], message: string): RuleProblem => ({
  code,
  message,
  severity: "error",
});

/** "1 day", "30 seconds": a duration in the largest unit it is whole in. */
export function durationWords(seconds: number): string {
  const [size, unit] = (
    [
      [86_400, "day"],
      [3_600, "hour"],
      [60, "minute"],
    ] as const
  ).find(([size]) => seconds > 0 && seconds % size === 0) ?? [1, "second"];
  const amount = seconds / size;
  return `${amount} ${unit}${amount === 1 ? "" : "s"}`;
}

/** What a number takes; a duration says it in the units it offers. */
function rangeMessage(spec: ParamSpec, low: boolean): string {
  const { min, max } = spec;
  if (spec.type === "duration" && (spec.units?.length ?? 4) > 1) {
    if (min !== undefined && max !== undefined)
      return `Use ${durationWords(min)} to ${durationWords(max)}.`;
    return low
      ? `Use ${durationWords(min!)} or more.`
      : `Use ${durationWords(max!)} or less.`;
  }
  if (min !== undefined && max !== undefined)
    return `Use a number from ${min} to ${max}.`;
  return low ? `Use ${min} or more.` : `Use ${max} or less.`;
}

export function fieldRules(
  spec: ParamSpec,
  value: ParamValue,
  entries: Entries,
): RuleProblem[] {
  // An absent value the language fills with a default (a human task's answers) is not missing.
  const filledByDefault = value.mode === "absent" && spec.default !== undefined;
  if (spec.required && !filledByDefault && empty(value, entries))
    return [error("required", requiredMessage(spec))];
  if (value.mode !== "fixed") return [];
  const v = value.value;
  const problems: RuleProblem[] = [];
  if (
    spec.type === "duration" &&
    typeof v === "number" &&
    !(Number.isInteger(v) && v > 0)
  )
    problems.push(error("positive", "Enter a whole number greater than 0."));
  if (spec.whole && typeof v === "number" && !Number.isInteger(v))
    problems.push(error("whole", "Enter a whole number."));
  if (
    (spec.type === "number" || spec.type === "duration") &&
    typeof v === "number"
  ) {
    const low = spec.min !== undefined && v < spec.min;
    const high = spec.max !== undefined && v > spec.max;
    if (low || high) problems.push(error("range", rangeMessage(spec, low)));
  }
  if (
    spec.maxLength !== undefined &&
    typeof v === "string" &&
    v.length > spec.maxLength
  )
    problems.push(error("length", `Use at most ${spec.maxLength} characters.`));
  if (
    spec.format === "version" &&
    typeof v === "string" &&
    v !== "" &&
    !validVersion(v)
  )
    problems.push(error("format", "Use a semantic version such as 1.0.0."));
  const count =
    entries.kind === "array"
      ? entries.length
      : entries.kind === "object"
        ? entries.keys.length
        : null;
  if (count !== null && spec.minItems !== undefined && count < spec.minItems)
    problems.push(error("items", `Add at least ${spec.minItems}.`));
  if (count !== null && spec.maxItems !== undefined && count > spec.maxItems)
    problems.push(error("items", `Use at most ${spec.maxItems}.`));
  return problems;
}

export function shownProblems<T extends { code: string }>(
  id: string,
  problems: T[],
  timing: RuleTiming,
): T[] {
  if (!timing.fresh || timing.revealAll || timing.touched.has(id))
    return problems;
  return problems.filter((problem) => problem.code !== "required");
}
