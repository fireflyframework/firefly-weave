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
// Turns a compiler diagnostic pointer into the step and field an author
// should open: `/spec/steps/i`, `/cases/j/steps/k`, `/default/steps/k` and
// `/branches/<name>/steps/k` select the innermost step; `/spec/...` and
// `/metadata/...` select the workflow settings.
import { WORKFLOW } from "../forms/core/scope";

export interface DiagnosticLocation {
  /** The step to select, or `WORKFLOW` for workflow settings. */
  stepId: string;
  /** Absolute pointer of that step (`/spec/steps/1/cases/0/steps/2`); "" for the workflow. */
  stepPointer: string;
  /**
   * The rest of the pointer, relative to the step, or to the whole document
   * for `WORKFLOW` (`/spec/inputSchema/...`, `/metadata/version`).
   */
  fieldPath: string;
  /**
   * The property-grid field that owns the pointer: `["with"]`,
   * `["cases", 0, "when"]`, `["branches", "a", "output"]`,
   * `["spec", "inputSchema"]`, `["metadata", "version"]`; `[]` for the
   * step or workflow as a whole.
   */
  field: (string | number)[];
  /**
   * Inside an expression field, the data keys with `object`/`array`/`literal`
   * wrappers removed (`["parameters", "customerId"]`); otherwise `[]`. For
   * `spec.connections` it names the connection slot.
   */
  dataPath: string[];
  /** False when the pointer no longer matches the definition and an ancestor was chosen. */
  exact: boolean;
}

type JsonObject = Record<string, unknown>;
const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const escape = (segment: string) =>
  segment.replace(/~/g, "~0").replace(/\//g, "~1");
const join = (segments: string[]) =>
  segments.map((s) => "/" + escape(s)).join("");
const index = (segment: string | undefined) =>
  segment !== undefined && /^(0|[1-9][0-9]*)$/.test(segment)
    ? Number(segment)
    : -1;

const EXPRESSION_FIELDS = new Set(["with", "value", "title", "context"]);

/** Strips Weave Expression wrappers to the target data keys. */
function dataKeys(segments: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < segments.length; i++) {
    const tag = segments[i];
    if (tag === "literal") return [...out, ...segments.slice(i + 1)];
    if ((tag === "object" || tag === "array") && i + 1 < segments.length)
      out.push(segments[++i]);
    else break;
  }
  return out;
}

function stepField(rest: string[]): {
  field: (string | number)[];
  dataPath: string[];
} {
  const [first, second, third] = rest;
  if (first === undefined) return { field: [], dataPath: [] };
  if (first === "cases" && index(second) >= 0) {
    if (third === "when" || third === "output")
      return {
        field: ["cases", index(second), third],
        dataPath: dataKeys(rest.slice(3)),
      };
    return { field: ["cases", index(second)], dataPath: [] };
  }
  if (first === "default" && second === "output")
    return { field: ["default", "output"], dataPath: dataKeys(rest.slice(2)) };
  if (first === "branches" && second !== undefined && third === "output")
    return {
      field: ["branches", second, "output"],
      dataPath: dataKeys(rest.slice(3)),
    };
  return {
    field: [first],
    dataPath: EXPRESSION_FIELDS.has(first) ? dataKeys(rest.slice(1)) : [],
  };
}

function workflowLocation(
  segments: string[],
  exact: boolean,
): DiagnosticLocation {
  const [top, name, slot] = segments;
  const base = {
    stepId: WORKFLOW,
    stepPointer: "",
    fieldPath: join(segments),
    exact,
  };
  if (top === "metadata" && name !== undefined)
    return { ...base, field: ["metadata", name], dataPath: [] };
  if (top === "spec" && name !== undefined) {
    const dataPath =
      name === "output"
        ? dataKeys(segments.slice(2))
        : name === "connections" && slot !== undefined
          ? [slot]
          : [];
    return { ...base, field: ["spec", name], dataPath };
  }
  return { ...base, field: top === undefined ? [] : [top], dataPath: [] };
}

/** The nested step list a pointer continues into, and where its index sits. */
function descend(
  step: JsonObject,
  rest: string[],
): { steps: unknown; skip: number } | null {
  const [owner, key, list, next] = rest;
  if (owner === "default" && key === "steps" && list !== undefined)
    return {
      steps: isObject(step["default"]) ? step["default"]["steps"] : undefined,
      skip: 2,
    };
  if (list !== "steps" || next === undefined || key === undefined) return null;
  if (owner === "cases") {
    const branch = Array.isArray(step["cases"])
      ? step["cases"][index(key)]
      : undefined;
    return { steps: isObject(branch) ? branch["steps"] : undefined, skip: 3 };
  }
  if (owner === "branches") {
    const branch = isObject(step["branches"])
      ? step["branches"][key]
      : undefined;
    return { steps: isObject(branch) ? branch["steps"] : undefined, skip: 3 };
  }
  return null;
}

/**
 * Locates a diagnostic `path` (RFC 6901) in a workflow definition. Pointers
 * that no longer match (a stale result after an edit) fall back to the
 * nearest existing step, or to the workflow, with `exact: false`.
 */
export function locateDiagnostic(
  pointer: string | null | undefined,
  definition: unknown,
): DiagnosticLocation {
  const text = pointer ?? "";
  if (text !== "" && !/^(?:\/(?:[^~/]|~[01])*)*$/.test(text))
    return workflowLocation([], false);
  const segments = text
    .split("/")
    .slice(1)
    .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
  // `/spec/steps` itself (e.g. an empty list) belongs to workflow settings.
  if (segments[0] !== "spec" || segments[1] !== "steps" || segments.length < 3)
    return workflowLocation(segments, true);
  const spec = isObject(definition) ? definition["spec"] : undefined;
  let steps: unknown = isObject(spec) ? spec["steps"] : undefined;
  let found: { step: JsonObject; end: number } | null = null;
  let exact = true;
  for (let at = 2; ; ) {
    const step = Array.isArray(steps) ? steps[index(segments[at])] : undefined;
    if (!isObject(step) || typeof step["id"] !== "string") {
      exact = false;
      break;
    }
    found = { step, end: at + 1 };
    const next = descend(step, segments.slice(at + 1));
    if (!next) break;
    steps = next.steps;
    at += 1 + next.skip;
  }
  if (!found)
    return {
      ...workflowLocation(segments, false),
      field: ["spec", "steps"],
      dataPath: [],
    };
  const rest = segments.slice(found.end);
  return {
    stepId: found.step["id"] as string,
    stepPointer: join(segments.slice(0, found.end)),
    fieldPath: join(rest),
    ...stepField(rest),
    exact,
  };
}
