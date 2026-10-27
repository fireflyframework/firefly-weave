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
// What a finished step says about itself on the canvas and in the Output
// pane: the summary line under its tile ("3 fields", "12 items", "approve",
// "Path: amount > 1000", "Waited 5 min") and, for a decision, which path was
// taken, which weren't, and which were never checked. DOM-free.
import { formatDuration } from "../../designer/canvas-summary";
import { conditionSummary, shortLabel } from "../../designer/conditions";
import type { Step } from "../../model";
import type { Json } from "../ndv/registry";

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

export type PathOutcome = "taken" | "not-taken" | "not-checked";
export interface PathRow {
  label: string;
  condition: string;
  outcome: PathOutcome;
}
export const OUTCOME_WORDS: Record<PathOutcome, string> = {
  taken: "Taken",
  "not-taken": "Not taken",
  "not-checked": "Not checked",
};

/**
 * Each path of a decision and what the run did with it, from the run's
 * variables (`branches`, owned by the decision). Cases are checked from the
 * top: those before the taken one weren't taken, those after it weren't
 * checked, and Otherwise wasn't taken unless no case applied. Null when the
 * run didn't reach the decision.
 */
export function pathRows(
  step: Step,
  variables: Record<string, Json> | undefined,
  definition?: unknown,
): PathRow[] | null {
  if (step.kind !== "switch") return null;
  const branches = isObject(variables?.["branches"])
    ? Object.values(variables["branches"])
    : [];
  const taken = branches.find((b) => isObject(b) && b["owner"] === step.id) as
    | Record<string, unknown>
    | undefined;
  if (!taken) return null;
  const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
  const name = String(taken["name"]);
  const index = name.startsWith("case:") ? Number(name.slice(5)) : -1;
  const rows: PathRow[] = cases.map((item, at) => ({
    label: `Path ${at + 1}`,
    condition: shortLabel(
      conditionSummary(isObject(item) ? item["when"] : undefined, definition),
    ),
    outcome:
      at === index
        ? "taken"
        : index < 0 || at < index
          ? "not-taken"
          : "not-checked",
  }));
  rows.push({
    label: "Otherwise",
    condition: "",
    outcome: index < 0 ? "taken" : "not-taken",
  });
  return rows;
}

/** The line under a step that ran, or null when it has nothing to say. */
export function summaryLine(
  step: Step,
  output: Json | undefined,
  variables?: Record<string, Json>,
  definition?: unknown,
): string | null {
  switch (step.kind as string) {
    case "wait":
      return `Waited ${formatDuration(step["durationSeconds"]) || "a while"}`;
    case "signal":
      return "Signal received";
    case "humanTask":
      return isObject(output) && typeof output["decision"] === "string"
        ? output["decision"]
        : null;
    case "switch": {
      const taken = pathRows(step, variables, definition)?.find(
        (row) => row.outcome === "taken",
      );
      return taken
        ? taken.label === "Otherwise"
          ? "Path: Otherwise"
          : `Path: ${taken.condition}`
        : null;
    }
  }
  if (output === undefined) return null;
  if (Array.isArray(output)) return plural(output.length, "item");
  if (isObject(output)) return plural(Object.keys(output).length, "field");
  return shortLabel(JSON.stringify(output));
}
