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
// Plain-language text for canvas nodes and duration fields: one summary line
// per step kind, the placeholders that still need a choice, and durations
// shown in the largest whole unit (seconds stay the stored value).
import { defaultDecisions, type Step } from "../model";
import { conditionSummary } from "./conditions";

export type DurationUnit = "s" | "min" | "h" | "d";
export const durationUnits: {
  unit: DurationUnit;
  seconds: number;
  label: string;
}[] = [
  { unit: "s", seconds: 1, label: "seconds" },
  { unit: "min", seconds: 60, label: "minutes" },
  { unit: "h", seconds: 3600, label: "hours" },
  { unit: "d", seconds: 86400, label: "days" },
];
const factor = (unit: string) =>
  durationUnits.find((u) => u.unit === unit)?.seconds ?? 1;

/** The largest unit that divides the value evenly; minutes for an empty value. */
export function splitDuration(seconds: unknown): {
  amount: number | null;
  unit: DurationUnit;
} {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0)
    return { amount: null, unit: "min" };
  for (const { unit, seconds: size } of [...durationUnits].reverse())
    if (seconds % size === 0) return { amount: seconds / size, unit };
  return { amount: seconds, unit: "s" };
}
export function formatDuration(seconds: unknown) {
  const { amount, unit } = splitDuration(seconds);
  return amount === null ? "" : `${amount} ${unit}`;
}
/**
 * Whole seconds for an amount in a unit. Empty text gives undefined; anything
 * that is not a positive whole number of seconds gives NaN.
 */
export function durationSeconds(text: string, unit: string) {
  if (!text.trim()) return undefined;
  const amount = Number(text);
  if (!Number.isFinite(amount)) return NaN;
  const seconds = Math.round(amount * factor(unit) * 1000) / 1000;
  return Number.isSafeInteger(seconds) && seconds > 0 ? seconds : NaN;
}

export const placeholderAction = "your-action@1.0.0";
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const plural = (count: number, word: string) =>
  `${count} ${word}${count === 1 ? "" : "s"}`;

function expressionSummary(expression: unknown) {
  const value = record(expression);
  if (typeof value["ref"] === "string") return `Copies ${value["ref"]}`;
  if ("object" in value)
    return `Builds ${plural(Object.keys(record(value["object"])).length, "field")}`;
  if (Array.isArray(value["array"]))
    return `Builds ${plural(value["array"].length, "item")}`;
  if ("op" in value) return "Computes a value";
  return "Fixed value";
}

/**
 * One short line describing what a step is configured to do. With the
 * workflow definition, a decision names its input fields by their titles.
 */
export function stepSummary(step: Step, definition?: unknown): string {
  switch (step.kind) {
    case "decisionTable":
      return String(step["uses"] ?? "Choose a decision table");
    case "llm":
      return `${step["profile"] || "Choose a profile"} · ${step["connection"] || "Choose a connection"}`;
    case "action": {
      const uses = String(step["uses"] ?? "");
      if (!uses || uses === placeholderAction) return "Choose an action";
      return step["connection"] ? `${uses} · ${step["connection"]}` : uses;
    }
    case "wait":
      return formatDuration(step["durationSeconds"]);
    case "signal":
      return [step["name"], formatDuration(step["timeoutSeconds"])]
        .filter(Boolean)
        .join(" · ");
    case "humanTask": {
      // Without a list, a human task offers approve and reject.
      const decisions = Array.isArray(step["decisions"])
        ? (step["decisions"] as unknown[]).join("/")
        : step["decisions"] === undefined
          ? defaultDecisions.join("/")
          : "";
      return [step["assignment"], decisions].filter(Boolean).join(" · ");
    }
    case "switch": {
      // "Amount > 1000 · otherwise": each path's condition, then the default.
      const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
      return [
        ...cases.map((branch) =>
          conditionSummary(record(branch)["when"], definition),
        ),
        "otherwise",
      ].join(" · ");
    }
    case "parallel": {
      const count = Object.keys(record(step["branches"])).length;
      const limit = step["concurrency"];
      return `${count === 1 ? "1 branch" : `${count} branches`}${limit ? ` · max ${limit}` : ""}`;
    }
    case "fail":
      return String(step["code"] ?? "");
    case "transform":
      return expressionSummary(step["value"]);
    default:
      return "";
  }
}

/** Choices a newly added step still needs before it does anything useful. */
export function stepIssues(step: Step): string[] {
  const issues: string[] = [];
  if (step.kind === "action") {
    const uses = String(step["uses"] ?? "");
    if (!uses || uses === placeholderAction)
      issues.push("Choose the action to call.");
  }
  if (step.kind === "switch" && Array.isArray(step["cases"]))
    (step["cases"] as { when?: unknown }[]).forEach((branch, i) => {
      if (branch.when === undefined)
        issues.push(`Case ${i + 1}: choose when this path applies.`);
    });
  return issues;
}

/**
 * The chip an incomplete step shows on the canvas: what to choose next, in
 * two or three words. Empty when the step needs nothing.
 */
export function incompleteChip(step: Step): string {
  if (step.kind === "action") {
    const uses = String(step["uses"] ?? "");
    if (!uses || uses === placeholderAction) return "Choose an action";
  }
  if (
    step.kind === "switch" &&
    Array.isArray(step["cases"]) &&
    (step["cases"] as { when?: unknown }[]).some((b) => b.when === undefined)
  )
    return "Choose a condition";
  return "";
}
