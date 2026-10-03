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
// The rows of the designer's diagnostics panel, loaded lazily. Each problem
// reads in plain words (diagnostic-copy), names the step and field it belongs
// to (diagnostic-location), and opens that field when clicked. A compiler
// suggestion becomes an "Apply suggested edit" button that hands the host the
// edited step or workflow; the host applies it as one undoable change.
import { Component, computed, input, output } from "@angular/core";
import { Icon } from "../icon";
import { describeDiagnostic, type DiagnosticView } from "./diagnostic-copy";
import {
  locateDiagnostic,
  type DiagnosticLocation,
} from "./diagnostic-location";

const WORKFLOW = "$workflow";

export interface DiagnosticInput {
  message: string;
  code?: string;
  severity?: string;
  path?: unknown;
  hint?: unknown;
  suggestedEdit?: unknown;
  [key: string]: unknown;
}

/** An edited copy of one step (by its ID) or of the whole workflow (`$workflow`). */
export interface SuggestedChange {
  stepId: string;
  value: unknown;
}

interface Row {
  view: DiagnosticView;
  location: DiagnosticLocation | null;
  where: string;
  change: SuggestedChange | null;
}

const stepFields: Record<string, string> = {
  id: "Step name",
  with: "Input",
  value: "Value",
  uses: "Action version",
  connection: "Connection slot",
  durationSeconds: "Duration",
  timeoutSeconds: "Timeout",
  name: "Signal name",
  payloadSchema: "Payload schema",
  assignment: "Assignment binding",
  title: "Title",
  context: "Context",
  decisions: "Decisions",
  dueSeconds: "Due after",
  expirySeconds: "Expires after",
  formSchema: "Form schema",
  concurrency: "Concurrency",
  code: "Error code",
  message: "Message",
};
const workflowFields: Record<string, string> = {
  inputSchema: "Input schema",
  outputSchema: "Output schema",
  output: "Workflow output",
  connections: "Connection slots",
  timeoutSeconds: "Workflow timeout",
  steps: "Steps",
};

/** The plain name of a property-grid field path. */
export function fieldName(field: readonly (string | number)[]): string {
  const [first, second, third] = field;
  if (first === "spec") return workflowFields[String(second)] ?? "";
  if (first === "metadata")
    return second === "version"
      ? "Version"
      : second === "name"
        ? "Name"
        : "Details";
  if (first === "cases")
    return `Case ${Number(second) + 1} ${third === "when" ? "condition" : "output"}`;
  if (first === "default") return "Otherwise output";
  if (first === "branches") return `Branch ${String(second)} output`;
  return stepFields[String(first)] ?? "";
}

/** "Step check · Input", or "Workflow settings · Input schema". */
export function placeOf(location: DiagnosticLocation): string {
  const field = fieldName(location.field);
  const owner =
    location.stepId === WORKFLOW
      ? "Workflow settings"
      : `Step ${location.stepId}`;
  return field ? `${owner} · ${field}` : owner;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const segmentsOf = (pointer: string) =>
  pointer
    .split("/")
    .slice(1)
    .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));

/** A copy of `target` with `value` written at `pointer`; null when the parent is missing. */
export function withPointer(
  target: unknown,
  pointer: string,
  value: unknown,
): unknown {
  const segments = segmentsOf(pointer);
  if (!segments.length) return structuredClone(value);
  const copy = structuredClone(target);
  let parent: unknown = copy;
  for (const segment of segments.slice(0, -1)) {
    parent = Array.isArray(parent)
      ? parent[Number(segment)]
      : isRecord(parent)
        ? parent[segment]
        : undefined;
    if (!parent || typeof parent !== "object") return null;
  }
  const last = segments.at(-1)!;
  if (Array.isArray(parent)) {
    const index = last === "-" ? parent.length : Number(last);
    if (!Number.isInteger(index) || index < 0 || index > parent.length)
      return null;
    parent[index] = structuredClone(value);
  } else (parent as Record<string, unknown>)[last] = structuredClone(value);
  return copy;
}

/** The edited step or workflow a compiler suggestion stands for. */
export function suggestedChange(
  edit: unknown,
  definition: unknown,
): SuggestedChange | null {
  if (!isRecord(edit) || typeof edit["path"] !== "string" || !("value" in edit))
    return null;
  const path = edit["path"];
  const where = locateDiagnostic(path, definition);
  if (!where.exact) return null;
  if (where.stepId === WORKFLOW) {
    const value = withPointer(definition, path, edit["value"]);
    return value ? { stepId: WORKFLOW, value } : null;
  }
  const step = segmentsOf(where.stepPointer).reduce<unknown>(
    (node, segment) =>
      Array.isArray(node)
        ? node[Number(segment)]
        : isRecord(node)
          ? node[segment]
          : undefined,
    definition,
  );
  // A suggestion may change the step's data, never its identity or kind.
  if (["/id", "/kind"].includes(where.fieldPath) || !isRecord(step))
    return null;
  const value = withPointer(step, where.fieldPath, edit["value"]);
  return value ? { stepId: where.stepId, value } : null;
}

/** Diagnostic rows: errors and warnings as rows, notes summarized after them. */
export function diagnosticRows(
  diagnostics: readonly DiagnosticInput[],
  definition: unknown,
): { problems: Row[]; notes: Row[] } {
  const problems: Row[] = [],
    notes: Row[] = [];
  for (const diagnostic of diagnostics) {
    const view = describeDiagnostic(diagnostic, (key) => fieldName([key]));
    const path = diagnostic.path;
    const location =
      typeof path === "string" ? locateDiagnostic(path, definition) : null;
    const row: Row = {
      view,
      location,
      where: location ? placeOf(location) : "",
      change: suggestedChange(diagnostic.suggestedEdit, definition),
    };
    (view.severity === "info" ? notes : problems).push(row);
  }
  return { problems, notes };
}

const severityLabels = { error: "Error", warning: "Warning", info: "Note" };

/**
 * Clickable diagnostic rows. Inputs: the diagnostics to show and the workflow
 * definition their pointers refer to. Outputs: `locate` (open that step or the
 * workflow settings and focus the field) and `apply` (a suggested edit).
 */
@Component({
  selector: "weave-diagnostics-list",
  standalone: true,
  imports: [Icon],
  template: `
    @if (rows().problems.length) {
      <ul class="diagnostic-rows" aria-label="Problems found">
        @for (row of rows().problems; track $index) {
          <li class="diagnostic-row" [attr.data-severity]="row.view.severity">
            <p class="diagnostic-target">
              <weave-icon
                class="diagnostic-icon"
                [name]="
                  row.view.severity === 'warning' ? 'warning' : 'failCircle'
                "
                [size]="16"
              />
              <span class="diagnostic-severity">{{
                severity(row.view.severity)
              }}</span>
              <span class="diagnostic-text">{{ row.view.text }}</span>
              @if (row.location) {
                <button
                  type="button"
                  class="text-link diagnostic-where"
                  (click)="locate.emit(row.location)"
                >
                  {{ row.where }}
                </button>
              }
            </p>
            @if (row.view.hint) {
              <p class="diagnostic-hint">{{ row.view.hint }}</p>
            }
            @if (row.change) {
              <p class="diagnostic-detail">
                <button
                  type="button"
                  class="text-link"
                  [disabled]="readonly()"
                  (click)="apply.emit(row.change)"
                >
                  Apply suggested edit
                </button>
              </p>
            }
            @if (
              row.view.code ||
              (row.view.technical && row.view.technical !== row.view.text)
            ) {
              <details class="diagnostic-details">
                <summary>Details</summary>
                @if (
                  row.view.technical && row.view.technical !== row.view.text
                ) {
                  <p class="diagnostic-technical">{{ row.view.technical }}</p>
                }
                @if (row.view.code) {
                  <small class="support-code"
                    >Support code: {{ row.view.code }}</small
                  >
                }
              </details>
            }
          </li>
        }
      </ul>
    }
    @if (rows().notes.length) {
      <details class="diagnostic-notes">
        <summary>
          {{ rows().notes.length }}
          {{ rows().notes.length === 1 ? "note" : "notes" }}
        </summary>
        <ul>
          @for (row of rows().notes; track $index) {
            <li>
              {{ row.view.text }}
              @if (row.where) {
                <span class="diagnostic-where">{{ row.where }}</span>
              }
            </li>
          }
        </ul>
      </details>
    }
  `,
})
export class DiagnosticsList {
  diagnostics = input<readonly DiagnosticInput[]>([]);
  /** The workflow definition the diagnostic pointers refer to. */
  definition = input<unknown>(null);
  readonly = input(false);
  locate = output<DiagnosticLocation>();
  apply = output<SuggestedChange>();
  rows = computed(() => diagnosticRows(this.diagnostics(), this.definition()));
  severity(value: keyof typeof severityLabels) {
    return severityLabels[value];
  }
}
