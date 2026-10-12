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
// Issues of one step: where each shows (the deepest field whose path holds
// it, else the step as a whole), on which tab, and the fix Studio knows for
// it. The header counts them in words; its tone is danger when any is an
// error.
import type { SuggestedChange } from "../../designer/diagnostics-list";
import { pathStartsWith } from "./params/paths";
import type { ParamSpec, Path } from "./registry";
import type { StepDetailsTab } from "./step-details-service";

export const UPDATE_URL =
  "https://fireflyframework.github.io/firefly-weave/guides/platform-overview/";
/** What a field shows while what it holds isn't in the workflow. */
export const NOT_APPLIED = "Not applied";
/** What Notes show while their text isn't saved. */
export const NOT_SAVED = "Not saved";

export type IssueFix =
  | { kind: "focus"; label: "Fix" }
  | { kind: "picker"; label: "Use data…" }
  | { kind: "connection"; label: "Choose a connection" }
  | {
      kind: "suggestion";
      label: "Apply suggested edit";
      change: SuggestedChange;
    }
  | { kind: "answers"; label: "Add paths for answers" }
  | { kind: "update"; label: "Update the platform"; href: string };

export interface PlacedSpec {
  spec: ParamSpec;
  tab: StepDetailsTab;
  /** Why the field can't change now, when it can't. */
  readOnly?: string | null;
}
export interface StepIssue {
  key: string;
  severity: "error" | "warning";
  message: string;
  field: PlacedSpec | null;
  fix: IssueFix | null;
  /** "Not applied" or "Not saved": what the field holds isn't in the workflow yet. */
  status?: string;
}

/** The deepest field whose path holds `at`, or null. */
export function fieldFor(at: Path, specs: PlacedSpec[]): PlacedSpec | null {
  let best: PlacedSpec | null = null;
  for (const placed of specs) {
    if (!placed.spec.path.length || !pathStartsWith(at, placed.spec.path))
      continue;
    if (!best || placed.spec.path.length > best.spec.path.length) best = placed;
  }
  return best;
}

/** A field whose value must be data from earlier in the workflow. */
const takesData = (spec: ParamSpec): boolean =>
  spec.mapping === "mapped" ||
  spec.type === "formula" ||
  spec.type === "fileRef";

export function fixFor(
  code: string,
  suggestion: SuggestedChange | null,
  field: PlacedSpec | null,
): IssueFix | null {
  if (suggestion)
    return {
      kind: "suggestion",
      label: "Apply suggested edit",
      change: suggestion,
    };
  if (/UNSUPPORTED_FEATURE$/.test(code))
    return { kind: "update", label: "Update the platform", href: UPDATE_URL };
  if (!field) return null;
  if (field.spec.type === "connection" || /CONNECTION/.test(code))
    return { kind: "connection", label: "Choose a connection" };
  if (/(UNAVAILABLE|MISSING)_REFERENCE$/.test(code))
    return { kind: "picker", label: "Use data…" };
  if (code === "required")
    return takesData(field.spec)
      ? { kind: "picker", label: "Use data…" }
      : { kind: "focus", label: "Fix" };
  return null;
}

export const issueCount = (issues: StepIssue[]): string =>
  `${issues.length} ${issues.length === 1 ? "issue" : "issues"}`;
export const issueTone = (issues: StepIssue[]): "danger" | "warning" =>
  issues.some((issue) => issue.severity === "error") ? "danger" : "warning";

/** The field to open on: the first error that has a field, else the first issue that has one. */
export function firstIssueField(issues: StepIssue[]): PlacedSpec | null {
  return (
    issues.find((issue) => issue.field && issue.severity === "error")?.field ??
    issues.find((issue) => issue.field)?.field ??
    null
  );
}

/** An entry not applied yet, and the step that holds it when it isn't the open one. */
export interface HeldLabel {
  label: string;
  step?: string;
}
/** What a toast says when step details close while entries don't apply yet, on any step. */
export function leavingText(entries: HeldLabel[]): string {
  if (!entries.length) return "";
  const named = entries.map(({ label, step }) =>
    step ? `${label} in ${step}` : label,
  );
  return `${NOT_APPLIED} yet: ${named.join(", ")}. What you typed is kept until you finish ${entries.length === 1 ? "it" : "them"}.`;
}
