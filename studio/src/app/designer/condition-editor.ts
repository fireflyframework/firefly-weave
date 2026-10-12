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
// Step details use the compact look: "Runs when all of these are true" only
// from the second row, data as a pill, a value mode on the value, a trash
// button, "Add condition", and a line under a row that received dropped data.
import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  ViewChildren,
  ElementRef,
  QueryList,
  AfterViewChecked,
  OnDestroy,
  inject,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Select } from "../forms/ui/select";
import { Icon } from "../icon";
import {
  ReferenceCombobox,
  type ReferenceOption,
} from "../forms/ui/reference-combobox";
import {
  buildCondition,
  conditionOperators,
  membership,
  rowComplete,
  missingConditionText,
  parseCondition,
  unary,
  type ConditionJoin,
  type ConditionOperator,
  type ConditionRow,
  type ConditionScalar,
} from "./conditions";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** A row as typed: the value stays text until the row is complete. */
interface DraftRow {
  ref: string;
  operator: ConditionOperator;
  text: string;
  compareRef?: string;
  items?: string[];
  source?: ConditionRow;
  itemSources?: (ConditionScalar | undefined)[];
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
): ConditionScalar | undefined {
  if (input.kind === "choice") {
    if (!text) return undefined;
    try {
      return JSON.parse(text) as ConditionScalar;
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
  imports: [Icon, NgTemplateOutlet, ReferenceCombobox, Select],
  template: `<div
    class="condition-editor"
    [class.is-compact]="compact"
    (focusout)="touched = true"
    role="group"
    [attr.aria-labelledby]="labelId || null"
    [attr.aria-label]="labelId ? null : label"
    [attr.aria-describedby]="problem && !compact ? prefix + '-error' : null"
  >
    @if (!compact) {
      <div class="condition-intro">
        Take this path when
        <weave-select
          label="Match rules"
          [hideLabel]="true"
          [options]="joinOptions"
          [value]="join"
          [disabled]="readOnly"
          (choose)="setJoin($any($event))"
        />
        of these are true:
      </div>
    } @else if (rows.length > 1) {
      <div class="condition-intro">
        <span>{{ label }}</span>
        <weave-select
          class="condition-join"
          label="Match rules"
          icon="chevronDown"
          [hideLabel]="true"
          [options]="compactJoinOptions"
          [value]="join"
          [disabled]="readOnly"
          (choose)="setJoin($any($event))"
        />
      </div>
    }
    @for (row of rows; track $index) {
      @let input = inputFor(row);
      @let n = $index + 1;
      @let invalid = rowInvalid(row);
      <div class="condition-row" [attr.data-row]="$index">
        @if (compact) {
          @let pill = pillFor(row.ref);
          @let cell = $index + ":data";
          <div
            class="condition-data"
            [class.has-pill]="!!pill"
            [class.is-editing]="editing() === cell"
            [attr.title]="pill ? pill.parts.join(' › ') : null"
            (keydown)="cellKey($event, cell)"
            (click)="cellClick($event, cell)"
            (focusout)="cellLeft($event, cell)"
          >
            <weave-reference-combobox
              [value]="row.ref"
              [options]="references"
              [ariaLabel]="'Condition ' + n + ' data'"
              placeholder="Choose data"
              [disabled]="readOnly"
              [openOnClick]="true"
              [invalid]="invalid"
              [describedByIds]="invalid ? errorId : ''"
              (valueChange)="setRef($index, $event)"
              (picked)="editing.set(null)"
            />
            @if (pill) {
              <ng-container
                [ngTemplateOutlet]="pillTemplate"
                [ngTemplateOutletContext]="{ $implicit: pill }"
              />
            }
          </div>
        } @else {
          <weave-reference-combobox
            class="condition-data"
            [value]="row.ref"
            [options]="references"
            [ariaLabel]="'Condition ' + n + ' data'"
            placeholder="Choose data"
            [disabled]="readOnly"
            (valueChange)="setRef($index, $event)"
          />
        }
        <weave-select
          class="condition-operator"
          [label]="'Condition ' + n + ' test'"
          [hideLabel]="true"
          [icon]="compact ? 'chevronDown' : 'chevron'"
          [attr.data-map-focus]="adapted()?.index === $index ? '' : null"
          [invalid]="invalid"
          [describedBy]="invalid ? errorId : null"
          [options]="operatorsFor(row)"
          [value]="row.operator"
          [disabled]="readOnly"
          (choose)="setOperator($index, $event)"
        />
        @if (isMembership(row.operator)) {
          <div class="condition-value condition-list">
            @for (
              item of row.items ?? [];
              track $index;
              let itemIndex = $index
            ) {
              <div class="condition-list-item">
                <div class="condition-list-label">
                  <span>Item {{ itemIndex + 1 }}</span>
                  @switch (input.kind) {
                    @case ("boolean") {
                      <weave-select
                        #listItem
                        [attr.data-item]="n + '-' + itemIndex"
                        [label]="'Condition ' + n + ' item ' + (itemIndex + 1)"
                        [hideLabel]="true"
                        [options]="booleanOptions"
                        [value]="item"
                        [disabled]="readOnly"
                        (choose)="setItem(n - 1, itemIndex, $event)"
                      />
                    }
                    @case ("choice") {
                      <weave-select
                        #listItem
                        [attr.data-item]="n + '-' + itemIndex"
                        [label]="'Condition ' + n + ' item ' + (itemIndex + 1)"
                        [hideLabel]="true"
                        [options]="choiceOptions(input)"
                        [value]="item"
                        [disabled]="readOnly"
                        (choose)="setItem(n - 1, itemIndex, $event)"
                      />
                    }
                    @default {
                      <input
                        #listItem
                        [attr.data-item]="n + '-' + itemIndex"
                        [type]="input.kind === 'number' ? 'number' : 'text'"
                        [attr.aria-label]="
                          'Condition ' + n + ' item ' + (itemIndex + 1)
                        "
                        [value]="item"
                        [disabled]="readOnly"
                        (input)="setItem(n - 1, itemIndex, $event)"
                      />
                    }
                  }
                </div>
                <button
                  type="button"
                  [disabled]="readOnly"
                  [attr.aria-label]="'Remove item ' + (itemIndex + 1)"
                  (click)="removeItem(n - 1, itemIndex)"
                >
                  ×
                </button>
              </div>
            }
            <button
              type="button"
              [disabled]="readOnly"
              (click)="addItem(n - 1)"
            >
              Add item
            </button>
          </div>
        } @else if (!isUnary(row.operator) && compact) {
          @let mapped = row.compareRef !== undefined;
          <div class="condition-value" [class.is-mapped]="mapped">
            @if (mapped) {
              @let compare = pillFor(row.compareRef ?? "");
              @let cell = $index + ":value";
              <div
                class="condition-data"
                [class.has-pill]="!!compare"
                [class.is-editing]="editing() === cell"
                [attr.title]="compare ? compare.parts.join(' › ') : null"
                (keydown)="cellKey($event, cell)"
                (click)="cellClick($event, cell)"
                (focusout)="cellLeft($event, cell)"
              >
                <weave-reference-combobox
                  [value]="row.compareRef ?? ''"
                  [options]="references"
                  [target]="comparisonSchema(row)"
                  [ariaLabel]="'Condition ' + n + ' value'"
                  placeholder="Choose data"
                  [disabled]="readOnly"
                  [openOnClick]="true"
                  [invalid]="invalid"
                  [describedByIds]="invalid ? errorId : ''"
                  (valueChange)="setComparisonRef(n - 1, $event)"
                  (picked)="editing.set(null)"
                />
                @if (compare) {
                  <ng-container
                    [ngTemplateOutlet]="pillTemplate"
                    [ngTemplateOutletContext]="{ $implicit: compare }"
                  />
                }
              </div>
            } @else {
              @switch (input.kind) {
                @case ("boolean") {
                  <weave-select
                    [label]="'Condition ' + n + ' value'"
                    [hideLabel]="true"
                    icon="chevronDown"
                    [options]="booleanOptions"
                    [value]="row.text"
                    [disabled]="readOnly"
                    [invalid]="invalid"
                    [describedBy]="invalid ? errorId : null"
                    (choose)="setText($index, $event)"
                  />
                }
                @case ("choice") {
                  <weave-select
                    [label]="'Condition ' + n + ' value'"
                    [hideLabel]="true"
                    icon="chevronDown"
                    [options]="choiceOptions(input)"
                    [value]="row.text"
                    [disabled]="readOnly"
                    [invalid]="invalid"
                    [describedBy]="invalid ? errorId : null"
                    (choose)="setText($index, $event)"
                  />
                }
                @default {
                  <input
                    class="condition-input"
                    [type]="input.kind === 'number' ? 'number' : 'text'"
                    [attr.inputmode]="
                      input.kind === 'number' ? 'decimal' : null
                    "
                    [attr.aria-label]="'Condition ' + n + ' value'"
                    [attr.aria-invalid]="invalid || null"
                    [attr.aria-describedby]="invalid ? errorId : null"
                    placeholder="Value"
                    [value]="row.text"
                    [disabled]="readOnly"
                    (input)="setText($index, $event)"
                  />
                }
              }
            }
            <div
              class="param-mode condition-mode"
              role="radiogroup"
              [attr.aria-label]="'Value mode for Condition ' + n + ' value'"
              (keydown)="modeKey($event, n - 1)"
            >
              <button
                type="button"
                role="radio"
                [attr.aria-checked]="!mapped"
                [attr.tabindex]="mapped ? -1 : 0"
                [disabled]="readOnly"
                (click)="useComparisonData(n - 1, false)"
              >
                Fixed
              </button>
              <button
                type="button"
                role="radio"
                [attr.aria-checked]="mapped"
                [attr.tabindex]="mapped ? 0 : -1"
                [disabled]="readOnly"
                (click)="useComparisonData(n - 1, true)"
              >
                Mapped
              </button>
            </div>
          </div>
        } @else if (!isUnary(row.operator)) {
          <div class="condition-value">
            @if (row.compareRef !== undefined) {
              <weave-reference-combobox
                [value]="row.compareRef"
                [options]="references"
                [target]="comparisonSchema(row)"
                [ariaLabel]="'Rule ' + n + ' comparison data'"
                [disabled]="readOnly"
                [removable]="true"
                (removed)="useComparisonData(n - 1, false)"
                (valueChange)="setComparisonRef(n - 1, $event)"
              />
            } @else {
              @switch (input.kind) {
                @case ("boolean") {
                  <weave-select
                    class="condition-value"
                    [label]="'Condition ' + n + ' value'"
                    [hideLabel]="true"
                    [options]="booleanOptions"
                    [value]="row.text"
                    [disabled]="readOnly"
                    (choose)="setText($index, $event)"
                  />
                }
                @case ("choice") {
                  <weave-select
                    class="condition-value"
                    [label]="'Condition ' + n + ' value'"
                    [hideLabel]="true"
                    [options]="choiceOptions(input)"
                    [value]="row.text"
                    [disabled]="readOnly"
                    (choose)="setText($index, $event)"
                  />
                }
                @default {
                  <input
                    class="condition-value"
                    [type]="input.kind === 'number' ? 'number' : 'text'"
                    [attr.inputmode]="
                      input.kind === 'number' ? 'decimal' : null
                    "
                    [attr.aria-label]="'Condition ' + n + ' value'"
                    placeholder="Value"
                    [value]="row.text"
                    [disabled]="readOnly"
                    (input)="setText($index, $event)"
                  />
                }
              }
              <button
                type="button"
                class="text-link"
                [disabled]="readOnly"
                (click)="useComparisonData(n - 1, true)"
              >
                Use data
              </button>
            }
          </div>
        }
        <button
          type="button"
          class="icon-button condition-remove"
          [attr.aria-label]="'Remove condition ' + n"
          [disabled]="readOnly"
          (click)="remove($index)"
        >
          <weave-icon [name]="compact ? 'trash' : 'close'" [size]="16" />
        </button>
        @if (adapted()?.index === $index) {
          <p class="condition-adapted" aria-hidden="true">
            <weave-icon name="ok" [size]="16" />{{ adapted()!.text }}
          </p>
        }
      </div>
    }
    <div class="condition-tools">
      <button
        type="button"
        [class]="compact ? 'text-link param-add' : 'tertiary sm'"
        [disabled]="readOnly"
        (click)="add()"
      >
        <weave-icon name="plus" [size]="16" />{{
          compact ? "Add condition" : "Add rule"
        }}
      </button>
      @if (compact) {
        @for (action of actions; track action.label) {
          <button
            type="button"
            class="text-link param-add"
            [disabled]="readOnly"
            (click)="action.run()"
          >
            <weave-icon name="plus" [size]="16" />{{ action.label }}
          </button>
        }
      }
      @if (!compact) {
        <button
          type="button"
          class="text-link"
          [disabled]="readOnly"
          (click)="editAsFormula()"
        >
          Edit as formula
        </button>
      }
    </div>
    @if (compact) {
      <p class="sr-only" role="status">{{ adapted()?.text ?? "" }}</p>
    } @else if (touched && problem) {
      <p class="field-error" [id]="prefix + '-error'">
        <weave-icon name="failCircle" [size]="16" />{{ problem }}
      </p>
    }
    <ng-template #pillTemplate let-pill>
      <span class="ref-pill condition-pill" aria-hidden="true">
        <weave-icon [name]="pill.icon" [size]="16" />
        @for (part of pill.parts; track $index; let last = $last) {
          <span>{{ part }}</span>
          @if (!last) {
            <span class="ref-sep">›</span>
          }
        }
      </span>
    </ng-template>
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
      .condition-adapted {
        grid-column: 1 / -1;
        margin: 0;
        font: var(--type-small);
        color: var(--success-ink);
      }
      .condition-list {
        display: grid;
        gap: var(--space-2);
      }
      .condition-list-item {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 28px;
        align-items: end;
        gap: var(--space-1);
      }
      .condition-list-label {
        display: grid;
        gap: var(--space-1);
        min-width: 0;
        font: var(--type-caption);
      }
      .condition-list-item :is(input, select) {
        width: 100%;
        min-width: 0;
      }
      .condition-list-item button {
        min-height: 28px;
      }
      .condition-list > button {
        justify-self: start;
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
      .condition-intro {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        font: var(--type-caption);
      }
      .condition-intro weave-select {
        width: 90px;
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
        background: var(--accent);
        color: var(--on-accent);
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
export class ConditionEditor implements OnChanges, AfterViewChecked, OnDestroy {
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
  /** Step details look: the join line only from the second row, "Add condition", no inline formula button. */
  @Input() compact = false;
  /** The id of the message about unfinished rows, which describes their controls. */
  @Input() errorId = "";
  /** More add actions on the Add condition line (a path's own options). */
  @Input() actions: readonly { label: string; run: () => void }[] = [];
  /**
   * The data cell being edited ("1:data", "1:value"): its pill gives way to
   * text only once the person types, opens the list or deletes.
   */
  readonly editing = signal<string | null>(null);
  /** After a drop: which row received data and how its tests changed, for 2 seconds. */
  readonly adapted = signal<{ index: number; text: string } | null>(null);
  private adaptedTimer: ReturnType<typeof setTimeout> | undefined;

  readonly prefix = `condition-${++sequence}`;
  @ViewChildren("listItem", { read: ElementRef }) listItems?: QueryList<
    ElementRef<HTMLElement>
  >;
  private pendingItem: string | null = null;
  readonly joins: { value: ConditionJoin; label: string }[] = [
    { value: "and", label: "All of" },
    { value: "or", label: "Any of" },
  ];
  rows: DraftRow[] = [];
  touched = false;
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private pendingRow = false;
  join: ConditionJoin = "and";
  /** The value last shown or emitted; "unset" for no condition. */
  private known: string | null = null;

  ngOnChanges() {
    const incoming = JSON.stringify(this.value) ?? "unset";
    if (incoming === this.known) return;
    this.known = incoming;
    // Rows from elsewhere (Undo, another path) aren't the ones a drop changed.
    this.clearAdapted();
    this.editing.set(null);
    const condition = parseCondition(this.value);
    this.join = condition?.join ?? "and";
    // An unset condition starts with one empty row to fill in.
    this.rows = condition?.rows.length
      ? condition.rows.map((row) => {
          const input = this.inputFor({ ...row, text: "" });
          return {
            ref: row.ref,
            operator: row.operator,
            compareRef: row.compareRef,
            text: membership(row.operator) ? "" : textOf(row, input),
            items: Array.isArray(row.value)
              ? row.value.map((value) => textOf({ ...row, value }, input))
              : undefined,
            itemSources: Array.isArray(row.value) ? [...row.value] : undefined,
            source: row,
          };
        })
      : [{ ref: "", operator: "eq", text: "" }];
  }

  ngAfterViewChecked() {
    if (this.pendingRow) {
      const input = [
        ...this.host.nativeElement.querySelectorAll<HTMLElement>(
          ".condition-data input[role=combobox]",
        ),
      ].at(-1);
      if (input) {
        this.pendingRow = false;
        input.focus();
      }
    }
    if (!this.pendingItem) return;
    const input = this.listItems?.find(
      (item) => item.nativeElement.dataset["item"] === this.pendingItem,
    );
    if (input) {
      this.pendingItem = null;
      (
        input.nativeElement.querySelector<HTMLInputElement>("input") ??
        input.nativeElement
      ).focus();
    }
  }
  ngOnDestroy() {
    clearTimeout(this.adaptedTimer);
  }
  private clearAdapted() {
    clearTimeout(this.adaptedTimer);
    this.adaptedTimer = undefined;
    this.adapted.set(null);
  }
  /**
   * Puts dropped data in a row (the given one, else the first without data,
   * else a new one) and says how its tests changed. False when it can't: read
   * only, data that isn't offered here, or a row that no longer exists.
   */
  dropRef(ref: string, index: number | null): boolean {
    const option = this.references.find((reference) => reference.ref === ref);
    if (this.readOnly || !option) return false;
    if (
      index !== null &&
      (!Number.isInteger(index) || index < 0 || index >= this.rows.length)
    )
      return false;
    let at = index ?? this.rows.findIndex((row) => !row.ref);
    if (at < 0) {
      this.rows = [...this.rows, { ref: "", operator: "eq", text: "" }];
      at = this.rows.length - 1;
    }
    this.setRef(at, ref);
    const kind = this.inputFor(this.rows[at]).kind;
    const tests =
      kind === "number"
        ? "number"
        : kind === "boolean"
          ? "yes/no"
          : kind === "choice"
            ? "choice"
            : "text";
    this.adapted.set({
      index: at,
      text: `Mapped ${option.label} (${option.typeLabel}). The comparisons now offer ${tests} tests.`,
    });
    clearTimeout(this.adaptedTimer);
    this.adaptedTimer = setTimeout(() => this.adapted.set(null), 2000);
    return true;
  }
  /** Typing, deleting or opening the list starts editing; the first key replaces the data. */
  cellKey(event: KeyboardEvent, cell: string) {
    if (this.editing() === cell || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp")
      return this.editing.set(cell);
    if (
      event.key.length === 1 ||
      event.key === "Backspace" ||
      event.key === "Delete"
    ) {
      (event.target as HTMLInputElement).select?.();
      this.editing.set(cell);
    }
  }
  /** A click in the text box opens the list; a pick in the list ends editing. */
  cellClick(event: MouseEvent, cell: string) {
    if (!(event.target as HTMLElement).closest('[role="listbox"]'))
      this.editing.set(cell);
  }
  cellLeft(event: FocusEvent, cell: string) {
    const next = event.relatedTarget as Node | null;
    if (
      this.editing() === cell &&
      !(event.currentTarget as HTMLElement).contains(next)
    )
      this.editing.set(null);
  }
  /** A typed row that isn't complete, once the rows were left: its controls are invalid. */
  rowInvalid(row: DraftRow): boolean {
    return (
      this.compact &&
      this.touched &&
      !!this.errorId &&
      !!(row.ref || row.text || row.compareRef !== undefined) &&
      !this.complete(row)
    );
  }
  /** Rows typed but not finished: they live here until they are complete. */
  hasDraft(): boolean {
    return this.rows.some(
      (row) =>
        (row.ref || row.text || row.compareRef !== undefined) &&
        !this.complete(row),
    );
  }
  /** The data of a row as a pill: its icon and breadcrumb ("Input › Amount"). */
  pillFor(ref: string): { icon: string; parts: string[] } | null {
    const option = this.references.find((reference) => reference.ref === ref);
    if (!option) return null;
    return {
      icon: option.source === "step" ? (option.stepKind ?? "action") : "source",
      parts: option.breadcrumb
        .replace(/^Workflow input(?= ›|$)/, "Input")
        .split(" › "),
    };
  }
  /** Arrow keys move between Fixed and Mapped and choose. */
  modeKey(event: KeyboardEvent, index: number) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const mapped = this.rows[index]?.compareRef !== undefined;
    this.useComparisonData(index, !mapped);
    const group = event.currentTarget as HTMLElement;
    setTimeout(() =>
      group
        .querySelector<HTMLElement>('[role="radio"][aria-checked="true"]')
        ?.focus(),
    );
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
  isMembership(operator: ConditionOperator) {
    return membership(operator);
  }
  comparisonSchema(row: DraftRow) {
    return this.schemaFor(row) ?? null;
  }
  useComparisonData(index: number, enabled: boolean) {
    if (
      this.readOnly ||
      (this.rows[index].compareRef !== undefined) === enabled
    )
      return;
    this.rows[index] = {
      ...this.rows[index],
      compareRef: enabled ? "" : undefined,
      source: undefined,
    };
    this.emit();
  }
  setComparisonRef(index: number, ref: string) {
    if (this.readOnly) return;
    this.rows[index] = {
      ...this.rows[index],
      compareRef: ref,
      source: undefined,
    };
    this.emit();
  }
  editAsFormula() {
    this.touched = true;
    if (
      this.rows.some(
        (row) =>
          (row.ref || row.text || row.compareRef !== undefined) &&
          !this.complete(row),
      )
    )
      return;
    this.emit();
    this.formula.emit();
  }
  private schemaFor(row: DraftRow) {
    return this.references.find((option) => option.ref === row.ref)?.schema;
  }
  operatorsFor(row: DraftRow) {
    const schema = this.schemaFor(row);
    const types = !schema
      ? []
      : Array.isArray(schema["type"])
        ? schema["type"]
        : schema["type"]
          ? [schema["type"]]
          : [];
    const choices = schema?.["enum"];
    if (!types.length && !Array.isArray(choices)) return conditionOperators;
    const allowed = new Set<ConditionOperator>([
      "eq",
      "ne",
      "exists",
      "missing",
      "in",
      "notIn",
    ]);
    if (types.includes("array") || types.includes("object")) {
      allowed.delete("in");
      allowed.delete("notIn");
      allowed.delete("eq");
      allowed.delete("ne");
      if (types.includes("array")) {
        allowed.add("contains");
        allowed.add("notContains");
      }
    }
    if (
      (types.includes("number") || types.includes("integer")) &&
      !Array.isArray(choices)
    ) {
      for (const operator of ordering) allowed.add(operator);
    }
    if (
      types.includes("string") ||
      (Array.isArray(choices) &&
        choices.every((value) => typeof value === "string"))
    ) {
      for (const operator of [
        "contains",
        "notContains",
        "startsWith",
        "endsWith",
      ] as const)
        allowed.add(operator);
    }
    if (row.source) allowed.add(row.operator);
    return conditionOperators.filter((operator) => allowed.has(operator.value));
  }
  inputFor(row: DraftRow): ValueInput {
    const schema = this.schemaFor(row);
    if (row.operator === "startsWith" || row.operator === "endsWith")
      return { kind: "text" };
    if (row.operator === "contains" || row.operator === "notContains") {
      return schema?.["type"] === "array" ||
        (Array.isArray(schema?.["type"]) && schema["type"].includes("array"))
        ? valueInput(schema["items"])
        : { kind: "text" };
    }
    return valueInput(schema);
  }
  joinOptions = [
    { value: "and", label: "all" },
    { value: "or", label: "any" },
  ];
  readonly compactJoinOptions = [
    { value: "and", label: "all of these are true" },
    { value: "or", label: "any of these are true" },
  ];
  booleanOptions = [
    { value: "", label: "Choose…" },
    { value: "true", label: "Yes" },
    { value: "false", label: "No" },
  ];
  choiceOptions(input: ValueInput) {
    return [
      { value: "", label: "Choose…" },
      ...this.choices(input).map((choice) => ({
        value: choice.text,
        label: choice.label,
      })),
    ];
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
    if (draft.compareRef !== undefined)
      return {
        ref: draft.ref.trim(),
        operator: draft.operator,
        compareRef: draft.compareRef.trim(),
      };
    if (draft.source)
      return {
        ...draft.source,
        ref: draft.ref.trim(),
        operator: draft.operator,
      };
    const items = (draft.items ?? []).map((text, i) =>
      draft.itemSources?.[i] !== undefined
        ? draft.itemSources[i]
        : typedValue(text, this.inputFor(draft), draft.operator),
    );
    return {
      ref: draft.ref.trim(),
      operator: draft.operator,
      ...(unary(draft.operator)
        ? {}
        : {
            value: membership(draft.operator)
              ? items.every(
                  (item): item is ConditionScalar => item !== undefined,
                )
                ? items
                : undefined
              : typedValue(draft.text, this.inputFor(draft), draft.operator),
          }),
    };
  }
  private complete(draft: DraftRow) {
    return rowComplete(this.row(draft));
  }
  setJoin(join: ConditionJoin) {
    if (this.readOnly || join === this.join) return;
    this.join = join;
    this.emit();
  }
  setRef(index: number, ref: string) {
    if (this.readOnly || ref === this.rows[index].ref) return;
    const row = { ...this.rows[index], ref, source: undefined };
    const operators = this.operatorsFor(row);
    if (!operators.some((option) => option.value === row.operator))
      row.operator = operators[0]?.value ?? "eq";
    this.rows[index] = row;
    this.emit();
  }
  setOperator(index: number, event: Event | string) {
    if (this.readOnly) return;
    const operator = (
      typeof event === "string"
        ? event
        : (event.target as HTMLSelectElement).value
    ) as ConditionOperator;
    const row = this.rows[index];
    const changesShape = membership(operator) !== membership(row.operator);
    this.rows[index] = {
      ...row,
      operator,
      source: changesShape ? undefined : row.source,
      items: membership(operator) ? (row.items ?? [""]) : row.items,
    };
    this.emit();
  }
  setText(index: number, event: Event | string) {
    if (this.readOnly) return;
    const text =
      typeof event === "string"
        ? event
        : (event.target as HTMLInputElement).value;
    this.rows[index] = { ...this.rows[index], text, source: undefined };
    this.emit();
  }
  setItem(index: number, item: number, event: Event | string) {
    if (this.readOnly) return;
    const row = this.rows[index];
    const items = [...(row.items ?? [])];
    const itemSources = [...(row.itemSources ?? [])];
    items[item] =
      typeof event === "string"
        ? event
        : (event.target as HTMLInputElement).value;
    itemSources[item] = undefined;
    this.rows[index] = { ...row, items, itemSources, source: undefined };
    this.emit();
  }
  addItem(index: number) {
    if (this.readOnly) return;
    const row = this.rows[index];
    const items = [...(row.items ?? []), ""];
    this.rows[index] = { ...row, items, source: undefined };
    this.pendingItem = `${index + 1}-${items.length - 1}`;
    this.emit();
  }
  removeItem(index: number, item: number) {
    if (this.readOnly) return;
    const row = this.rows[index];
    this.rows[index] = {
      ...row,
      items: row.items?.filter((_, i) => i !== item),
      itemSources: row.itemSources?.filter((_, i) => i !== item),
      source: undefined,
    };
    this.emit();
  }
  add() {
    if (this.readOnly) return;
    this.rows = [...this.rows, { ref: "", operator: "eq", text: "" }];
    this.pendingRow = true;
  }
  remove(index: number) {
    if (this.readOnly) return;
    this.editing.set(null);
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
