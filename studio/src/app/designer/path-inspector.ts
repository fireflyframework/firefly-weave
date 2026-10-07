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
import {
  Component,
  Input,
  DoCheck,
  ChangeDetectionStrategy,
} from "@angular/core";
import type { App } from "../app";
import type { Step } from "../model";
import { ConditionEditor } from "./condition-editor";
import { ExpressionEditor } from "../property-grid";
import { branchName, parseCondition } from "./conditions";
import { referencesAt } from "../forms/core/reference-context";
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
@Component({
  selector: "weave-path-inspector",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ConditionEditor, ExpressionEditor],
  template: `
    <p class="hint">
      The first path whose rules match runs. Otherwise runs when no path
      matches.
    </p>
    @for (path of paths; track $index; let index = $index) {
      <section class="decision-path">
        <header>
          <h3>{{ title(index) }}</h3>
          <div class="path-actions">
            <button
              type="button"
              aria-label="Move path up"
              [disabled]="locked || index === 0"
              (click)="host.manageBranch('up', String(index))"
            >
              ↑
            </button>
            <button
              type="button"
              aria-label="Move path down"
              [disabled]="locked || index === paths.length - 1"
              (click)="host.manageBranch('down', String(index))"
            >
              ↓
            </button>
            <button
              type="button"
              aria-label="Remove path"
              [disabled]="locked"
              (click)="host.manageBranch('remove', String(index))"
            >
              Remove
            </button>
          </div>
        </header>
        <div [attr.data-field]="'cases/' + index + '/when'">
          @if (formula.has(index) || !rowsFit(path["when"])) {
            <weave-expression-editor
              [value]="path['when']"
              [references]="refs(index, 'when')"
              [readOnly]="locked"
              [conditionOnly]="true"
              [expectedSchema]="{ type: 'boolean' }"
              [label]="'Path ' + (index + 1) + ' rule'"
              (valueChange)="edit(index, 'when', $event)"
              (validityChange)="validity(index, 'when', $event)"
            />
            @if (rowsFit(path["when"])) {
              <button
                type="button"
                class="text-link"
                (click)="formula.delete(index)"
              >
                Use rule rows
              </button>
            }
          } @else {
            <weave-condition-editor
              [value]="path['when']"
              [references]="refs(index, 'when')"
              [readOnly]="locked"
              [label]="'Path ' + (index + 1) + ' rules'"
              (valueChange)="edit(index, 'when', $event)"
              (validityChange)="validity(index, 'when', $event)"
              (formula)="formula.add(index)"
            />
          }
        </div>
        <details [attr.data-field]="'cases/' + index + '/output'">
          <summary>
            Path result ·
            {{ unset(path["output"]) ? "Not set (optional)" : "Configured" }}
          </summary>
          <weave-expression-editor
            [value]="path['output']"
            [references]="refs(index, 'output')"
            [readOnly]="locked"
            [label]="'Path ' + (index + 1) + ' result'"
            (valueChange)="edit(index, 'output', $event)"
            (validityChange)="validity(index, 'output', $event)"
          />
        </details>
      </section>
    }
    <button
      type="button"
      [disabled]="locked"
      (click)="host.manageBranch('add')"
    >
      + Add path
    </button>
    <section class="decision-path otherwise">
      <h3>Otherwise</h3>
      <p class="hint">Take this path when no rules above match.</p>
      <details data-field="default/output">
        <summary>
          Path result ·
          {{ unset(fallback["output"]) ? "Not set (optional)" : "Configured" }}
        </summary>
        <weave-expression-editor
          [value]="fallback['output']"
          [references]="refs(-1, 'output')"
          [readOnly]="locked"
          label="Otherwise result"
          (valueChange)="edit(-1, 'output', $event)"
          (validityChange)="validity(-1, 'output', $event)"
        />
      </details>
    </section>
  `,
  styles: [
    `
      :host {
        display: grid;
        gap: 12px;
        min-width: 0;
      }
      .decision-path {
        display: grid;
        gap: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 12px;
        min-width: 0;
      }
      .decision-path header {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .decision-path h3 {
        margin: 0;
        overflow-wrap: anywhere;
      }
      .path-actions {
        display: flex;
        gap: 6px;
      }
      .path-actions button {
        min-width: 28px;
        padding: 4px 8px;
      }
      .hint {
        margin: 0;
      }
      .decision-path details {
        min-width: 0;
      }
      .decision-path summary {
        font-size: 12px;
        margin-bottom: 8px;
      }
    `,
  ],
})
export class PathInspector implements DoCheck {
  @Input({ required: true }) host!: App;
  String = String;
  formula = new Set<number>();
  private invalid = new Set<string>();
  private previous: unknown;
  private source = "";
  private current!: Step;
  get step() {
    if (this.source !== this.host.inspectorBuffer) {
      this.source = this.host.inspectorBuffer;
      this.current = JSON.parse(this.source) as Step;
    }
    return this.current;
  }
  get paths() {
    return (this.step["cases"] ?? []) as Record<string, unknown>[];
  }
  get fallback() {
    return object(this.step["default"]);
  }
  get locked() {
    return this.host.model.readonly || this.host.editingLocked;
  }
  ngDoCheck() {
    if (this.previous === this.host.propertyStep) return;
    this.previous = this.host.propertyStep;
    this.invalid.clear();
    this.formula.clear();
  }
  title(index: number) {
    const title = branchName(
      this.step,
      "case " + (index + 1),
      this.host.model.definition,
    );
    return title === "Condition not set" ? "Path " + (index + 1) : title;
  }
  unset(value: unknown) {
    const literal = object(value)["literal"];
    return (
      !!literal &&
      typeof literal === "object" &&
      !Array.isArray(literal) &&
      !Object.keys(literal).length
    );
  }
  rowsFit(value: unknown) {
    return parseCondition(value) !== null;
  }
  refs(index: number, field: string) {
    return referencesAt(
      this.host.referenceContext,
      index < 0 ? "/default/" + field : "/cases/" + index + "/" + field,
    );
  }
  validity(index: number, field: string, valid: boolean) {
    const path =
      index < 0 ? "default/" + field : "cases/" + index + "/" + field;
    valid ? this.invalid.delete(path) : this.invalid.add(path);
    this.host.propertyValid.set(!this.invalid.size);
    this.host.inspectorFieldValidity({ path, valid });
  }
  edit(index: number, field: string, value: unknown) {
    const step = structuredClone(this.step);
    const path =
      index < 0 ? "default/" + field : "cases/" + index + "/" + field;
    const branch =
      index < 0
        ? object(step["default"])
        : (step["cases"] as Record<string, unknown>[])[index];
    if (value === undefined) delete branch[field];
    else branch[field] = value;
    this.host.stepEdit(step, path);
  }
}
