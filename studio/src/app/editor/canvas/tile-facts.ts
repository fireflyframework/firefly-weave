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
// What a tile shows besides its step: one corner badge picked by priority,
// the four border states, and the words that read them out.
import type { TileRun } from "./run-state";

export interface StepFacts {
  /** Compile and validation errors, first one first. */
  errors: readonly string[];
  warnings: readonly string[];
  /** What the step still needs, as short instructions ("map Customer ID"). */
  setup: readonly string[];
}
export interface TileFacts extends StepFacts {
  run: TileRun;
  /** Why the last run failed here. */
  failure: { code?: string; message?: string } | null;
  /** Why the step's data is out of date, or null. */
  stale: string | null;
  pinned: boolean;
  /** Step details hold an edit not applied yet. */
  unapplied: boolean;
  selected: boolean;
}
export type BadgeKind =
  | "failed"
  | "error"
  | "setup"
  | "warning"
  | "stale"
  | "pinned-stale"
  | "pinned"
  | "done";
export type BadgeTone = "danger" | "warning" | "info" | "success";
export interface TileBadge {
  kind: BadgeKind;
  tone: BadgeTone;
  /** `<weave-icon>` names, drawn side by side. */
  icons: readonly string[];
  /** The tooltip and the accessible text. */
  text: string;
}

/** The one corner badge: the first match from a failed run down to a finished one. */
export function tileBadge(facts: TileFacts): TileBadge | null {
  if (facts.run === "live" || facts.run === "waiting") return null;
  if (facts.run === "failed") {
    const detail = [facts.failure?.code, facts.failure?.message]
      .filter(Boolean)
      .join(", ");
    return {
      kind: "failed",
      tone: "danger",
      icons: ["critical"],
      text: detail ? `Failed: ${detail}` : "Failed",
    };
  }
  if (facts.errors.length)
    return {
      kind: "error",
      tone: "danger",
      icons: ["failCircle"],
      text: facts.errors[0],
    };
  if (facts.setup.length)
    return {
      kind: "setup",
      tone: "warning",
      icons: ["connections"],
      text: `Setup needed: ${facts.setup[0]}`,
    };
  if (facts.warnings.length)
    return {
      kind: "warning",
      tone: "warning",
      icons: ["warning"],
      text: facts.warnings[0],
    };
  if (facts.stale !== null)
    return facts.pinned
      ? {
          kind: "pinned-stale",
          tone: "warning",
          icons: ["pin", "stale"],
          text: `Pinned test data, out of date: ${facts.stale}`,
        }
      : {
          kind: "stale",
          tone: "warning",
          icons: ["stale"],
          text: `Output may change when this step runs again: ${facts.stale}`,
        };
  if (facts.pinned)
    return {
      kind: "pinned",
      tone: "info",
      icons: ["pin"],
      text: "Pinned test data",
    };
  if (facts.run === "done")
    return {
      kind: "done",
      tone: "success",
      icons: ["check"],
      text: "Finished in the last run",
    };
  return null;
}

export interface TileBorders {
  selected: boolean;
  live: boolean;
  unapplied: boolean;
  error: boolean;
}
/** The only states a border shows; every other state is the badge. */
export function tileBorders(facts: TileFacts): TileBorders {
  return {
    selected: facts.selected,
    live: facts.run === "live" || facts.run === "waiting",
    unapplied: facts.unapplied,
    error: facts.run === "failed" || facts.errors.length > 0,
  };
}

/** What the live border means, for the accessible name. */
export function runText(run: TileRun, kind: string): string {
  if (run === "live") return "Running";
  if (run !== "waiting") return "";
  if (kind === "signal") return "Waiting for a signal";
  if (kind === "humanTask") return "Waiting for a person";
  return "Waiting";
}

/** "Needs: Customer ID" → "map Customer ID"; "Needs a connection" → "choose a connection". */
export function setupPhrase(label: string): string {
  const field = /^Needs: (.+)$/.exec(label);
  if (field) return `map ${field[1]}`;
  const choice = /^Needs (.+)$/.exec(label);
  if (choice) return `choose ${choice[1]}`;
  return label.charAt(0).toLowerCase() + label.slice(1);
}

export interface TileNameParts {
  id: string;
  kindLabel: string;
  summary: string;
  /** The live state or the badge's text. */
  status: string;
  index: number;
  count: number;
  place: string;
}
/** "check-customer, Call an action, Customer lookup, Setup needed: map Customer ID, step 2 of 6 in Main sequence". */
export function tileName(parts: TileNameParts): string {
  return [
    parts.id,
    parts.kindLabel,
    parts.summary,
    parts.status,
    `step ${parts.index + 1} of ${parts.count} in ${parts.place}`,
  ]
    .filter(Boolean)
    .join(", ");
}

/** "Manual form · 3 fields": the workflow's input form starts it. */
export function triggerSubtitle(workflow: {
  spec: Record<string, unknown>;
}): string {
  const schema = workflow.spec["inputSchema"];
  const properties =
    schema && typeof schema === "object"
      ? (schema as Record<string, unknown>)["properties"]
      : undefined;
  const count =
    properties && typeof properties === "object"
      ? Object.keys(properties).length
      : 0;
  return count
    ? `Manual form · ${count} ${count === 1 ? "field" : "fields"}`
    : "Manual form";
}
