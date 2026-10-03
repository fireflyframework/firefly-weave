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
// A decision case's condition as rule rows: [data] [is greater than] [1000].
// Rows write the expression tree the language already has; the property grid
// shows anything rows can't hold in the formula editor ("Edit as formula").
import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
} from "@angular/core";
import { Icon } from "../icon";
import {
  ReferenceCombobox,
  type ReferenceOption,
} from "../forms/ui/reference-combobox";
import {
  buildCondition,
  conditionOperators,
  missingConditionText,
  parseCondition,
  unary,
  type ConditionJoin,
  type ConditionOperator,
  type ConditionRow,
} from "./conditions";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** A row as typed: the value stays text until the row is complete. */
interface DraftRow {
  ref: string;
  operator: ConditionOperator;
  text: string;
}
/** How a row's value is entered, from the schema of the data it tests. */
export type ValueInput =
  | { kind: "number"; integer: boolean }
  | { kind: "boolean" }
  | { kind: "choice"; options: unknown[] }
  | { kind: "text" };

const ordering = new Set<ConditionOperator>(["gt", "gte", "lt", "lte"]);

/** The value control for data with this schema. */
export function valueInput(schema: unknown): ValueInput {
  if (!isRecord(schema)) return { kind: "text" };
  if (Array.isArray(schema["enum"]) && schema["enum"].length)
    return { kind: "choice", options: schema["enum"] };
  const types = Array.isArray(schema["type"])
    ? schema["type"].map(String)
    : [String(schema["type"] ?? "")];
  if (types.includes("integer")) return { kind: "number", integer: true };
  if (types.includes("number")) return { kind: "number", integer: false };
  if (types.includes("boolean")) return { kind: "boolean" };
  return { kind: "text" };
}

/**
 * The literal a row compares with, from the typed text: a number for number
 * data (or, when the data's type is unknown, for "greater than" and the
 * like), true or false for yes/no data, the chosen option, otherwise text.
 * Undefined while the text can't be that value yet.
 */
export function typedValue(
  text: string,
  input: ValueInput,
  operator: ConditionOperator,
): ConditionRow["value"] | undefined {
  if (input.kind === "choice") {
    if (!text) return undefined;
    try {
      return JSON.parse(text) as ConditionRow["value"];
    } catch {
      return undefined;
    }
  }
  if (input.kind === "boolean")
    return text === "true" ? true : text === "false" ? false : undefined;
  const numeric = input.kind === "number" || ordering.has(operator);
  if (numeric) {
    if (!text.trim()) return undefined;
    const value = Number(text);
    if (!Number.isFinite(value))
      return input.kind === "number" ? undefined : text;
    if (input.kind === "number" && input.integer && !Number.isInteger(value))
      return undefined;
    return value;
  }
  return text === "" ? undefined : text;
}

/** The text a stored literal shows as. */
const textOf = (row: ConditionRow, input: ValueInput) =>
  row.value === undefined
    ? ""
    : input.kind === "choice"
      ? JSON.stringify(row.value)
      : String(row.value);

let sequence = 0;

/**
 * Inputs: the condition (`value`, undefined while unset), its field label
 * and the data it may read. Outputs: the new expression (undefined clears
 * it), validity, and `formula` when the person asks for the formula editor.
 */
@Component({
  selector: "weave-condition-editor",
  standalone: true,
  imports: [Icon, ReferenceCombobox],
  template: `<div
    class="condition-editor"
    role="group"
    [attr.aria-labelledby]="labelId || null"
    [attr.aria-label]="labelId ? null : label"
    [attr.aria-describedby]="problem ? prefix + '-error' : null"
  >
    @if (rows.length > 1) {
      <div class="condition-join" role="group" aria-label="Match">
        @for (option of joins; track option.value) {
          <button
            type="button"
            [attr.aria-pressed]="join === option.value"
            [disabled]="readOnly"
            (click)="setJoin(option.value)"
          >
            {{ option.label }}
          </button>
        }
      </div>
    }
    @for (row of rows; track $index) {
      @let input = inputFor(row);
      @let n = $index + 1;
      <div class="condition-row" [attr.data-row]="$index">
        <weave-reference-combobox
          class="condition-data"
          [value]="row.ref"
          [options]="references"
          [ariaLabel]="'Condition ' + n + ' data'"
          placeholder="Choose data"
          [disabled]="readOnly"
          (valueChange)="setRef($index, $event)"
        />
        <select
          class="condition-operator"
          [attr.aria-label]="'Condition ' + n + ' test'"
          [disabled]="readOnly"
          (change)="setOperator($index, $event)"
        >
          @for (option of operators; track option.value) {
            <option
              [value]="option.value"
              [selected]="option.value === row.operator"
            >
              {{ option.label }}
            </option>
          }
        </select>
        @if (!isUnary(row.operator)) {
          @switch (input.kind) {
            @case ("boolean") {
              <select
                class="condition-value"
                [attr.aria-label]="'Condition ' + n + ' value'"
                [disabled]="readOnly"
                (change)="setText($index, $event)"
              >
                <option value="" [selected]="row.text === ''">Choose…</option>
                <option value="true" [selected]="row.text === 'true'">
                  true
                </option>
                <option value="false" [selected]="row.text === 'false'">
                  false
                </option>
              </select>
            }
            @case ("choice") {
              <select
                class="condition-value"
                [attr.aria-label]="'Condition ' + n + ' value'"
                [disabled]="readOnly"
                (change)="setText($index, $event)"
              >
                <option value="" [selected]="row.text === ''">Choose…</option>
                @for (choice of choices(input); track choice.text) {
                  <option
                    [value]="choice.text"
                    [selected]="choice.text === row.text"
                  >
                    {{ choice.label }}
                  </option>
                }
              </select>
            }
            @default {
              <input
                class="condition-value"
                [type]="input.kind === 'number' ? 'number' : 'text'"
                [attr.inputmode]="input.kind === 'number' ? 'decimal' : null"
                [attr.aria-label]="'Condition ' + n + ' value'"
                placeholder="Value"
                [value]="row.text"
                [disabled]="readOnly"
                (input)="setText($index, $event)"
              />
            }
          }
        }
        <button
          type="button"
          class="icon-button condition-remove"
          [attr.aria-label]="'Remove condition ' + n"
          [disabled]="readOnly"
          (click)="remove($index)"
        >
          <weave-icon name="close" [size]="16" />
        </button>
      </div>
    }
    <div class="condition-tools">
      <button
        type="button"
        class="tertiary sm"
        [disabled]="readOnly"
        (click)="add()"
      >
        <weave-icon name="plus" [size]="16" />Add condition
      </button>
      <button
        type="button"
        class="text-link"
        [disabled]="readOnly"
        (click)="formula.emit()"
      >
        Edit as formula
      </button>
    </div>
    @if (problem) {
      <p class="field-error" [id]="prefix + '-error'">
        <weave-icon name="failCircle" [size]="16" />{{ problem }}
      </p>
    }
  </div>`,
  styles: [
    `
      :host {
        display: block;
        container-type: inline-size;
      }
      .condition-editor {
        display: grid;
        gap: var(--space-2);
      }
      .condition-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 32px;
        grid-template-areas:
          "data data remove"
          "test value value";
        gap: var(--space-2);
        align-items: start;
        padding: var(--space-2);
        border: 1px solid var(--line);
        border-radius: var(--radius-md);
        background: var(--sunken);
      }
      .condition-data {
        grid-area: data;
      }
      .condition-operator {
        grid-area: test;
        min-width: 0;
      }
      .condition-value {
        grid-area: value;
        min-width: 0;
        width: 100%;
      }
      .condition-remove {
        grid-area: remove;
        width: 32px;
        height: 32px;
        min-height: 32px;
      }
      @container (min-width: 460px) {
        .condition-row {
          grid-template-columns:
            minmax(0, 1.4fr) minmax(0, 1fr) minmax(0, 1fr)
            32px;
          grid-template-areas: "data test value remove";
        }
      }
      .condition-join {
        display: inline-flex;
        justify-self: start;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        overflow: hidden;
      }
      .condition-join button {
        min-height: 28px;
        padding: 0 10px;
        border: 0;
        border-radius: 0;
        font: var(--type-caption);
        font-weight: 600;
      }
      .condition-join button + button {
        border-left: 1px solid var(--border);
      }
      .condition-join button[aria-pressed="true"] {
        background: var(--forest);
        color: var(--on-dark);
      }
      .condition-tools {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-2);
      }
      .field-error {
        margin: 0;
      }
    `,
  ],
})
export class ConditionEditor implements OnChanges {
  @Input() value: unknown = undefined;
  @Input() label = "Condition";
  /** The id of a visible label that names the group, when there is one. */
  @Input() labelId = "";
  @Input() references: readonly ReferenceOption[] = [];
  @Input() readOnly = false;
  @Output() valueChange = new EventEmitter<unknown>();
  @Output() validityChange = new EventEmitter<boolean>();
  /** The person asked to edit this condition as a formula. */
  @Output() formula = new EventEmitter<void>();

  readonly prefix = `condition-${++sequence}`;
  readonly operators = conditionOperators;
  readonly joins: { value: ConditionJoin; label: string }[] = [
    { value: "and", label: "All of" },
    { value: "or", label: "Any of" },
  ];
  rows: DraftRow[] = [];
  join: ConditionJoin = "and";
  /** The value last shown or emitted; "unset" for no condition. */
  private known: string | null = null;

  ngOnChanges() {
    const incoming = JSON.stringify(this.value) ?? "unset";
    if (incoming === this.known) return;
    this.known = incoming;
    const condition = parseCondition(this.value);
    this.join = condition?.join ?? "and";
    // An unset condition starts with one empty row to fill in.
    this.rows = condition?.rows.length
      ? condition.rows.map((row) => ({
          ref: row.ref,
          operator: row.operator,
          text: textOf(row, this.inputFor({ ...row, text: "" })),
        }))
      : [{ ref: "", operator: "eq", text: "" }];
  }

  /** What is wrong, in the words the validator uses. */
  get problem() {
    const typed = this.rows.filter((row) => row.ref || row.text);
    if (!typed.length) return missingConditionText;
    if (typed.some((row) => !this.complete(row)))
      return "Finish this condition, or remove it.";
    return "";
  }
  isUnary(operator: ConditionOperator) {
    return unary(operator);
  }
  inputFor(row: DraftRow): ValueInput {
    const option = this.references.find((o) => o.ref === row.ref);
    return valueInput(option?.schema);
  }
  choices(input: ValueInput) {
    return input.kind === "choice"
      ? input.options.map((value) => ({
          text: JSON.stringify(value),
          label: value === null ? "null" : String(value),
        }))
      : [];
  }
  private row(draft: DraftRow): ConditionRow {
    return {
      ref: draft.ref.trim(),
      operator: draft.operator,
      ...(unary(draft.operator)
        ? {}
        : {
            value: typedValue(draft.text, this.inputFor(draft), draft.operator),
          }),
    };
  }
  private complete(draft: DraftRow) {
    const row = this.row(draft);
    return (
      /^(?:\/(?:[^~/]|~[01])*)+$/.test(row.ref) &&
      (unary(row.operator) || row.value !== undefined)
    );
  }
  setJoin(join: ConditionJoin) {
    if (this.readOnly || join === this.join) return;
    this.join = join;
    this.emit();
  }
  setRef(index: number, ref: string) {
    this.rows[index] = { ...this.rows[index], ref };
    this.emit();
  }
  setOperator(index: number, event: Event) {
    const operator = (event.target as HTMLSelectElement)
      .value as ConditionOperator;
    this.rows[index] = { ...this.rows[index], operator };
    this.emit();
  }
  setText(index: number, event: Event) {
    const text = (event.target as HTMLInputElement).value;
    this.rows[index] = { ...this.rows[index], text };
    this.emit();
  }
  add() {
    if (this.readOnly) return;
    this.rows = [...this.rows, { ref: "", operator: "eq", text: "" }];
    this.emit();
  }
  remove(index: number) {
    if (this.readOnly) return;
    this.rows = this.rows.filter((_, i) => i !== index);
    if (!this.rows.length) this.rows = [{ ref: "", operator: "eq", text: "" }];
    this.emit();
  }
  /**
   * Complete rows write the condition; an empty editor clears it (the case is
   * then reported as unset). A half-filled row blocks applying until it is
   * finished or removed.
   */
  private emit() {
    if (this.readOnly) return;
    const typed = this.rows.filter((row) => row.ref || row.text);
    if (typed.length && typed.some((row) => !this.complete(row))) {
      this.validityChange.emit(false);
      return;
    }
    const value = buildCondition({
      join: this.join,
      rows: typed.map((row) => this.row(row)),
    });
    this.known = JSON.stringify(value) ?? "unset";
    this.validityChange.emit(true);
    this.valueChange.emit(value);
  }
}
