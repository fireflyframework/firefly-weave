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
  ChangeDetectorRef,
  Component,
  ElementRef,
  OnChanges,
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Icon } from "./icon";
import { ReferenceCombobox } from "./forms/ui/reference-combobox";
import { LazyComponent, type LazyOutputs } from "./forms/ui/lazy-component";
import type { FieldInfo } from "./forms/core/resolve";
import {
  formFields,
  formatProblem,
  localDateTime,
  memberField,
  missingBound,
  prepareData,
  scalarMember,
  storedDateTime,
  validReference,
  widgetFor,
  type Widget,
} from "./forms/core/form-model";
import { fieldsOf } from "./forms/core/resolve";
import {
  decode,
  encode,
  formula,
  get as boundAt,
  literalValue,
  reference,
  remove as unbind,
  set as bind,
  value as literalNode,
  type Bound,
} from "./forms/core/binding";
import { getAt, removeAt, setAt } from "./forms/core/json";
import {
  referencesAt,
  type ReferenceContext,
} from "./forms/core/reference-context";
import type { Schema } from "./task-schema";
// The schema helpers live apart so the shell can use them without loading the form.
export {
  type Schema,
  groupedObject,
  missingRequired,
  scalarList,
  withValue,
} from "./task-schema";

type Data = Record<string, unknown>;
type Path = (string | number)[];
/** Where a bound field's value comes from. */
type Mode = "value" | "data" | "formula";
const modes: { mode: Mode; label: string; hint: string }[] = [
  { mode: "value", label: "Value", hint: "Type the value" },
  {
    mode: "data",
    label: "Data",
    hint: "Use the workflow input or a step's output",
  },
  { mode: "formula", label: "Formula", hint: "Combine data with an operation" },
];
/**
 * What an unwritten formula shows: "Choose a formula…", not a null value.
 * One object, so its editor isn't reset.
 */
const emptyFormula = { op: { name: "", args: [] } };
/** Widgets that never take a value from data or a formula. */
const fixed = new Set<Widget>(["secret", "const", "never"]);
const isRecord = (v: unknown): v is Data =>
  !!v && typeof v === "object" && !Array.isArray(v);
let sequence = 0;

/**
 * Schema-driven form for human task decisions, workflow input, simulation
 * values and action inputs (WP-08, WP-11, WP-12). Fields are resolved by
 * forms/core/resolve.ts (`$ref`, `allOf`, nullable, `oneOf` choices, formats)
 * and each kind gets a real editor: groups, lists, tables of records, maps,
 * constants, date-times and typed values; only unions and schemas the form
 * can't read fall back to JSON. Secret (`x-secret`, `writeOnly`) fields are
 * locked and never receive a value. A required boolean starts at `false`; an
 * optional one is "Not set", "Yes" or "No".
 *
 * With `bindings` on, the form edits an expression (an action's `with`):
 * each field is a value, data from the workflow input or an earlier step, or
 * a formula. Untouched fields keep their original expression, so an unedited
 * input is emitted unchanged. Partial input such as "-" in a number field or
 * half-typed JSON stays on screen and blocks submission instead of being
 * coerced or cleared.
 */
@Component({
  selector: "weave-task-form",
  standalone: true,
  imports: [NgTemplateOutlet, Icon, ReferenceCombobox, LazyComponent],
  template: `<div class="task-fields">
      <ng-container
        *ngTemplateOutlet="
          group;
          context: {
            $implicit: rootFields(),
            path: [],
            active: true,
            depth: 0,
          }
        "
      />
    </div>
    <p class="sr-only" aria-live="polite">{{ status }}</p>
    <!-- "active" is false inside an optional group that holds no value yet: its
         required fields are marked, but only enforced once the group is used. -->
    <ng-template
      #group
      let-fields
      let-path="path"
      let-active="active"
      let-depth="depth"
    >
      @for (field of fields; track field.key) {
        @let fieldPath = path.concat(field.key);
        @let k = key(fieldPath);
        @let id = idFor(fieldPath);
        @let widget = widgetOf(field, depth);
        @let mode = modeAt(fieldPath, widget);
        @let enforced = active && field.required;
        @let described = describedBy(id, field, k);
        @let boxed =
          mode === "value" &&
          (widget === "group" ||
            widget === "list" ||
            widget === "map" ||
            widget === "table");
        @if (boxed) {
          <fieldset
            class="schema-group"
            [class.schema-list]="widget !== 'group'"
            [attr.aria-describedby]="described"
            [attr.data-path]="k"
          >
            <legend [id]="id + '-label'">
              {{ field.label }}
              @if (!field.required) {
                <span class="optional"> (optional)</span>
              }
            </legend>
            @if (bindings()) {
              <ng-container
                *ngTemplateOutlet="
                  sources;
                  context: {
                    $implicit: field,
                    path: fieldPath,
                    mode,
                    widget,
                    labelId: id + '-label',
                  }
                "
              />
            }
            @if (field.description) {
              <p class="hint" [id]="id + '-hint'">{{ field.description }}</p>
            }
            @switch (widget) {
              @case ("group") {
                <ng-container
                  *ngTemplateOutlet="
                    group;
                    context: {
                      $implicit: fieldsIn(field),
                      path: fieldPath,
                      active: enforced || read(fieldPath) !== undefined,
                      depth: depth + 1,
                    }
                  "
                />
              }
              @case ("table") {
                @for (row of items(fieldPath); track $index) {
                  <fieldset
                    class="schema-group schema-row"
                    tabindex="-1"
                    [id]="idFor(fieldPath.concat($index))"
                  >
                    <legend>{{ field.label }} item {{ $index + 1 }}</legend>
                    <ng-container
                      *ngTemplateOutlet="
                        group;
                        context: {
                          $implicit: fieldsIn(member(field)),
                          path: fieldPath.concat($index),
                          active: true,
                          depth: depth + 1,
                        }
                      "
                    />
                    <button
                      type="button"
                      class="schema-remove"
                      (click)="removeItem(fieldPath, $index, field.label)"
                    >
                      Remove {{ field.label }} item {{ $index + 1 }}
                    </button>
                  </fieldset>
                }
                <button
                  type="button"
                  class="schema-add"
                  [id]="id + '-add'"
                  (click)="addRow(fieldPath, field)"
                >
                  Add {{ field.label }} item
                </button>
              }
              @case ("list") {
                @let item = member(field);
                @for (value of items(fieldPath); track $index) {
                  @let itemPath = fieldPath.concat($index);
                  @let itemId = idFor(itemPath);
                  @let itemKey = key(itemPath);
                  <div class="schema-list-row">
                    <ng-container
                      *ngTemplateOutlet="
                        scalar;
                        context: {
                          $implicit: item,
                          path: itemPath,
                          id: itemId,
                          label: field.label + ' item ' + ($index + 1),
                          value,
                          member: true,
                        }
                      "
                    />
                    <button
                      type="button"
                      [attr.aria-label]="
                        'Remove ' + field.label + ' item ' + ($index + 1)
                      "
                      (click)="removeItem(fieldPath, $index, field.label)"
                    >
                      Remove
                    </button>
                  </div>
                  @if (errors[itemKey]) {
                    <p class="error" [id]="itemId + '-error'">
                      {{ errors[itemKey] }}
                    </p>
                  }
                }
                <button
                  type="button"
                  class="schema-add"
                  [id]="id + '-add'"
                  (click)="addItem(fieldPath, item, field.label)"
                >
                  Add {{ field.label }} item
                </button>
              }
              @case ("map") {
                @let entry = member(field);
                @for (pair of entries(fieldPath); track $index) {
                  @let entryPath = fieldPath.concat(pair.key);
                  @let entryId = idFor(entryPath);
                  @let keyId = entryId + "-key";
                  <div class="schema-map-row">
                    <input
                      [id]="keyId"
                      [attr.aria-label]="field.label + ' name ' + ($index + 1)"
                      [value]="pair.key"
                      [attr.aria-invalid]="!!errors[k + '#' + $index] || null"
                      [attr.aria-describedby]="
                        errors[k + '#' + $index] ? keyId + '-error' : null
                      "
                      (change)="
                        renameEntry(fieldPath, pair.key, $index, $event)
                      "
                    />
                    <ng-container
                      *ngTemplateOutlet="
                        scalar;
                        context: {
                          $implicit: entry,
                          path: entryPath,
                          id: entryId,
                          label: field.label + ' value ' + ($index + 1),
                          value: pair.value,
                          member: true,
                        }
                      "
                    />
                    <button
                      type="button"
                      [attr.aria-label]="
                        'Remove ' + field.label + ' entry ' + ($index + 1)
                      "
                      (click)="removeEntry(fieldPath, pair.key, field.label)"
                    >
                      Remove
                    </button>
                  </div>
                  @if (errors[k + "#" + $index]) {
                    <p class="error" [id]="keyId + '-error'">
                      {{ errors[k + "#" + $index] }}
                    </p>
                  }
                  @if (errors[key(entryPath)]) {
                    <p class="error" [id]="entryId + '-error'">
                      {{ errors[key(entryPath)] }}
                    </p>
                  }
                }
                <button
                  type="button"
                  class="schema-add"
                  [id]="id + '-add'"
                  (click)="addEntry(fieldPath, entry, field.label)"
                >
                  Add {{ field.label }} entry
                </button>
              }
            }
          </fieldset>
        } @else {
          <div
            class="schema-field"
            [attr.data-path]="k"
            [class.bound]="mode !== 'value'"
          >
            <div class="schema-field-head">
              @if (widget === "checkbox" && mode === "value") {
                <label class="checkbox-field" [for]="id" [id]="id + '-label'"
                  ><input
                    [id]="id"
                    type="checkbox"
                    [checked]="read(fieldPath) === true"
                    [attr.aria-describedby]="described"
                    (change)="setBoolean(fieldPath, $event)"
                  />{{ field.label }}
                  @if (!field.required) {
                    <span class="optional"> (optional)</span>
                  }
                </label>
              } @else if (
                mode === "formula" || fixed(widget) || widget === "any"
              ) {
                <span class="schema-label" [id]="id + '-label'"
                  >{{ field.label }}
                  @if (!field.required && widget !== "secret") {
                    <span class="optional"> (optional)</span>
                  }
                </span>
              } @else {
                <label [for]="id" [id]="id + '-label'"
                  >{{ field.label }}
                  @if (!field.required) {
                    <span class="optional"> (optional)</span>
                  }
                </label>
              }
              @if (bindings() && !fixed(widget)) {
                <ng-container
                  *ngTemplateOutlet="
                    sources;
                    context: {
                      $implicit: field,
                      path: fieldPath,
                      mode,
                      widget,
                      labelId: id + '-label',
                    }
                  "
                />
              }
            </div>
            @if (mode === "data") {
              @defer (on immediate) {
                <weave-reference-combobox
                  [inputId]="id"
                  ariaLabel=""
                  [value]="pointerAt(fieldPath)"
                  [options]="references"
                  [target]="field.schema"
                  [describedByIds]="described ?? ''"
                  (valueChange)="setReference(fieldPath, $event)"
                />
              } @placeholder {
                <input [id]="id" disabled placeholder="Loading data…" />
              }
            } @else if (mode === "formula") {
              <ng-container
                [weaveLazy]="loadFormula"
                [lazyInputs]="{
                  value: formulaAt(fieldPath),
                  label: field.label,
                  references,
                }"
                [lazyOutputs]="handlers('formula', fieldPath)"
              />
            } @else {
              @switch (widget) {
                @case ("checkbox") {}
                @case ("secret") {
                  <p
                    class="schema-locked"
                    [attr.aria-labelledby]="id + '-label'"
                  >
                    <weave-icon name="lock" />{{
                      bindings()
                        ? "Supplied by the connection's credentials; workflows can't set it."
                        : "Secret: Studio never enters, sends or stores this value."
                    }}
                  </p>
                }
                @case ("const") {
                  <p class="schema-locked">
                    <span class="schema-chip">{{
                      show(field.constValue)
                    }}</span>
                    Always this value.
                  </p>
                }
                @case ("never") {
                  <p class="hint">This field can't have a value.</p>
                }
                @case ("any") {
                  <ng-container
                    [weaveLazy]="loadValue"
                    [lazyInputs]="{ value: read(fieldPath) ?? null }"
                    [lazyOutputs]="handlers('any', fieldPath)"
                  />
                }
                @case ("json") {
                  <textarea
                    [id]="id"
                    class="json-field"
                    spellcheck="false"
                    [placeholder]="
                      field.schema['type'] === 'array' ? '[]' : '{}'
                    "
                    [value]="jsonText(fieldPath)"
                    [required]="enforced"
                    [attr.aria-invalid]="!!errors[k] || null"
                    [attr.aria-describedby]="described"
                    (input)="setJson(fieldPath, $event, field)"
                  ></textarea>
                }
                @default {
                  <ng-container
                    *ngTemplateOutlet="
                      scalar;
                      context: {
                        $implicit: field,
                        path: fieldPath,
                        id,
                        required: enforced,
                        described,
                        value: read(fieldPath),
                      }
                    "
                  />
                }
              }
              @if (
                field.nullable && widget !== "const" && widget !== "secret"
              ) {
                <label class="checkbox-field schema-null"
                  ><input
                    type="checkbox"
                    [attr.aria-label]="field.label + ': no value (null)'"
                    [checked]="read(fieldPath) === null"
                    (change)="setNull(fieldPath, $event)"
                  />No value (null)</label
                >
              }
            }
            @if (field.description) {
              <p class="hint" [id]="id + '-hint'">{{ field.description }}</p>
            }
            @if (errors[k]) {
              <p class="error" [id]="id + '-error'">{{ errors[k] }}</p>
            }
          </div>
        }
      }
    </ng-template>

    <!-- Value | Data | Formula for one field of a bound expression. -->
    <ng-template
      #sources
      let-field
      let-path="path"
      let-mode="mode"
      let-widget="widget"
      let-labelId="labelId"
    >
      <!-- The group name leaves the field label out, so the field's own
           label names only its control; each button is described by it. -->
      <span
        class="binding-modes"
        role="group"
        aria-label="Where the value comes from"
      >
        @for (option of modes; track option.mode) {
          <button
            type="button"
            class="binding-mode"
            [attr.data-mode]="option.mode"
            [attr.aria-pressed]="mode === option.mode"
            [attr.aria-describedby]="labelId"
            [title]="option.hint"
            (click)="switchMode(path, widget, option.mode)"
          >
            {{ option.label }}
          </button>
        }
      </span>
    </ng-template>

    <!-- One plain value: a scalar field, a list item or a map value. -->
    <ng-template
      #scalar
      let-field
      let-path="path"
      let-id="id"
      let-label="label"
      let-required="required"
      let-described="described"
      let-value="value"
      let-member="member"
    >
      @let widget = scalarWidget(field);
      @let k = key(path);
      @let isNull = value === null;
      @let invalid = !!errors[k] || null;
      @let describe = described ?? (errors[k] ? id + "-error" : null);
      @switch (widget) {
        @case ("choice") {
          <select
            [id]="id"
            [attr.aria-label]="label || null"
            [required]="required"
            [disabled]="isNull"
            [attr.aria-describedby]="describe"
            (change)="setChoice(path, $event, field)"
          >
            <option value="" [selected]="choiceIndex(field, value) < 0">
              Choose a value
            </option>
            @for (option of field.options; track $index) {
              <option
                [value]="$index"
                [selected]="choiceIndex(field, value) === $index"
              >
                {{ option.label }}
              </option>
            }
          </select>
        }
        @case ("tristate") {
          <select
            [id]="id"
            [attr.aria-label]="label || null"
            [disabled]="isNull"
            [attr.aria-describedby]="describe"
            (change)="setTristate(path, $event)"
          >
            @if (!member) {
              <option value="" [selected]="value !== true && value !== false">
                Not set
              </option>
            }
            <option value="true" [selected]="value === true">Yes</option>
            <option value="false" [selected]="value === false">No</option>
          </select>
        }
        @case ("multiline") {
          <textarea
            [id]="id"
            [attr.aria-label]="label || null"
            [value]="isNull ? '' : (value ?? '')"
            [required]="required"
            [disabled]="isNull"
            [attr.minlength]="field.schema['minLength']"
            [attr.maxlength]="field.schema['maxLength']"
            [attr.aria-invalid]="invalid"
            [attr.aria-describedby]="describe"
            (input)="setScalar(path, $event, field, widget, label)"
          ></textarea>
        }
        @case ("json") {
          <textarea
            [id]="id"
            class="json-field"
            spellcheck="false"
            [attr.aria-label]="label || null"
            [value]="jsonText(path)"
            [attr.aria-invalid]="invalid"
            [attr.aria-describedby]="describe"
            (input)="setJson(path, $event, field, label)"
          ></textarea>
        }
        @default {
          <input
            [id]="id"
            [type]="inputType(widget)"
            [attr.aria-label]="label || null"
            [value]="
              isNull
                ? ''
                : widget === 'datetime'
                  ? dateText(path, value)
                  : (value ?? '')
            "
            [attr.step]="
              widget === 'integer' ? 1 : widget === 'number' ? 'any' : null
            "
            [attr.inputmode]="widget === 'integer' ? 'numeric' : null"
            [attr.min]="field.schema['minimum']"
            [attr.max]="field.schema['maximum']"
            [attr.minlength]="field.schema['minLength']"
            [attr.maxlength]="field.schema['maxLength']"
            [attr.placeholder]="placeholder(field, widget)"
            [required]="required"
            [disabled]="isNull"
            [attr.aria-invalid]="invalid"
            [attr.aria-describedby]="describe"
            (input)="setScalar(path, $event, field, widget, label)"
          />
        }
      }
    </ng-template>`,
  styles: [
    `
      .schema-field {
        display: grid;
        gap: 4px;
        min-width: 0;
      }
      .schema-field-head {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 4px 8px;
      }
      .schema-label {
        display: block;
        margin: 6px 0 0;
        font-size: 12px;
        font-weight: 600;
      }
      .binding-modes {
        display: inline-flex;
        justify-self: start;
        border: 1px solid var(--line);
        border-radius: 6px;
        overflow: hidden;
      }
      .binding-mode {
        min-height: 24px;
        padding: 0 8px;
        border: 0;
        border-radius: 0;
        font: var(--type-caption);
      }
      .binding-mode + .binding-mode {
        border-left: 1px solid var(--line);
      }
      .binding-mode[aria-pressed="true"] {
        background: var(--forest);
        color: var(--on-dark);
        font-weight: 600;
      }
      .schema-map-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
        gap: 6px;
      }
      .schema-map-row button,
      .schema-remove {
        font-size: 11px;
        min-height: 32px;
        padding: 4px 8px;
      }
      .schema-locked {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 0;
        color: var(--muted);
        font-size: 12px;
      }
      .schema-locked weave-icon {
        width: 16px;
        height: 16px;
        --icon-stroke: 1.5;
        flex: none;
      }
      .schema-chip {
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 1px 8px;
        background: var(--mist);
        color: var(--text);
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      }
      .schema-null {
        font-size: 11px;
        color: var(--muted);
      }
      @media (max-width: 420px) {
        .schema-map-row {
          grid-template-columns: minmax(0, 1fr) auto;
        }
        .schema-map-row > :nth-child(2) {
          grid-column: 1 / -1;
          grid-row: 2;
        }
      }
    `,
  ],
})
export class TaskForm implements OnChanges {
  schema = input<Schema>({});
  initialData = input<Record<string, unknown>>({});
  /** Edit an expression whose fields each take a value, data or a formula. */
  bindings = input(false);
  /** The expression edited when `bindings` is on, such as an action's `with`. */
  expression = input<unknown>(undefined);
  /** Where the expression sits, for data suggestions (bindings only). */
  scope = input<ReferenceContext | null>(null);
  /** The expression's pointer relative to its step. */
  fieldPath = input("/with");
  dataChange = output<Record<string, unknown>>();
  /** The edited expression (bindings only); never emitted before an edit. */
  expressionChange = output<unknown>();
  /** Labels of required fields the expression leaves unset (bindings only). */
  missingChange = output<string[]>();
  validityChange = output<boolean>();
  values: Data = {};
  bound: Bound = { kind: "object", entries: {} };
  errors: Record<string, string> = {};
  /** A polite announcement for added and removed rows. */
  status = "";
  modes = modes;
  /** Text the person typed that isn't a value yet (JSON, dates, references). */
  private drafts = new Map<string, string>();
  /** A source chosen for a field that has no value from it yet. */
  private chosen = new Map<string, Mode>();
  /** What each source held, so switching back restores it. */
  private memory = new Map<string, Partial<Record<Mode, Bound>>>();
  private fieldCache = new WeakMap<object, FieldInfo[]>();
  private memberCache = new WeakMap<object, FieldInfo | null>();
  private ids = new Map<string, string>();
  private generation = 0;
  private prefix = `schema-field-${++sequence}`;
  private host = inject(ElementRef<HTMLElement>);
  private cdr = inject(ChangeDetectorRef);
  private handlerCache = new Map<string, LazyOutputs>();
  private literals = new WeakMap<Bound, unknown>();
  private expressions = new WeakMap<Bound, unknown>();
  /** The formula and typed-value editors live in the property grid's chunk. */
  loadFormula = () => import("./property-grid").then((m) => m.ExpressionEditor);
  loadValue = () => import("./property-grid").then((m) => m.PropertyValue);
  /** Output handlers of a lazily loaded editor, one stable set per field. */
  handlers(kind: "formula" | "any", path: Path): LazyOutputs {
    const key = `${kind}\u001e${this.key(path)}`;
    let handlers = this.handlerCache.get(key);
    if (!handlers)
      this.handlerCache.set(
        key,
        (handlers =
          kind === "formula"
            ? {
                valueChange: (value: unknown) => this.setFormula(path, value),
                validityChange: (valid: boolean) =>
                  this.formulaValidity(path, valid),
              }
            : {
                valueChange: (value: unknown) => this.write(path, value),
                validityChange: (valid: boolean) =>
                  this.nestedValidity(path, valid),
              }),
      );
    return handlers;
  }

  ngOnChanges(changes: SimpleChanges) {
    if (
      !changes["schema"] &&
      !changes["initialData"] &&
      !changes["expression"] &&
      !changes["bindings"]
    )
      return;
    const generation = ++this.generation;
    this.errors = {};
    this.drafts.clear();
    this.chosen.clear();
    this.memory.clear();
    if (this.bindings()) {
      const bound = decode(this.expression() ?? { literal: {} }, this.schema());
      // The host offers fields only for an object input; anything else
      // starts as an empty object instead of being misread.
      this.bound =
        bound.kind === "object" ? bound : { kind: "object", entries: {} };
      queueMicrotask(() => {
        if (generation === this.generation)
          this.missingChange.emit(this.missing());
      });
      return;
    }
    const prepared = prepareData(this.schema(), this.initialData());
    this.values = prepared.data;
    // Filled defaults (a required `false`) and dropped secrets are what the
    // form submits, so the host hears about them before any edit.
    if (prepared.changed)
      queueMicrotask(() => {
        if (generation === this.generation) this.publish();
      });
  }

  get valid() {
    return Object.keys(this.errors).length === 0;
  }
  get references() {
    return referencesAt(this.scope(), this.fieldPath());
  }
  rootFields() {
    return this.fieldsIn(this.schema());
  }
  /** Fields of an object schema or field, resolved once. */
  fieldsIn(source: FieldInfo | Schema | null | undefined): FieldInfo[] {
    if (!source || typeof source !== "object") return [];
    let fields = this.fieldCache.get(source);
    if (!fields) {
      fields =
        source === this.schema()
          ? formFields(source)
          : "kind" in source && "context" in source
            ? fieldsOf(
                (source as FieldInfo).schema,
                (source as FieldInfo).context,
              )
            : formFields(source);
      this.fieldCache.set(source, fields);
    }
    return fields;
  }
  /** A list's items or a map's values as one field. */
  member(field: FieldInfo): FieldInfo | null {
    if (!this.memberCache.has(field))
      this.memberCache.set(field, memberField(field));
    return this.memberCache.get(field) ?? null;
  }
  widgetOf(field: FieldInfo, depth: number): Widget {
    return widgetFor(field, depth);
  }
  /** The widget for one value inside a list or map, or a scalar field. */
  scalarWidget(field: FieldInfo | null): Widget {
    if (!field || !scalarMember(field)) return "json";
    return field.kind === "boolean" ? "tristate" : widgetFor(field);
  }
  fixed(widget: Widget) {
    return fixed.has(widget);
  }
  key(path: Path) {
    return path.join("\u001f");
  }
  /** A unique, stable element ID per field path. */
  idFor(path: Path) {
    const key = this.key(path);
    let id = this.ids.get(key);
    if (!id) this.ids.set(key, (id = `${this.prefix}-${this.ids.size + 1}`));
    return id;
  }
  describedBy(id: string, field: FieldInfo, key: string) {
    return (
      [
        field.description ? id + "-hint" : "",
        this.errors[key] ? id + "-error" : "",
      ]
        .filter(Boolean)
        .join(" ") || null
    );
  }
  inputType(widget: Widget) {
    return (
      (
        {
          number: "number",
          integer: "number",
          email: "email",
          uri: "url",
          datetime: "datetime-local",
        } as Record<string, string>
      )[widget] ?? "text"
    );
  }
  placeholder(field: FieldInfo, widget: Widget) {
    if (widget === "uuid") return "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e";
    const example = field.examples?.[0] ?? field.default;
    return example === undefined || typeof example === "object"
      ? null
      : String(example);
  }
  show(value: unknown) {
    return value === null ? "None" : JSON.stringify(value);
  }

  // --- reading ---------------------------------------------------------------
  read(path: Path): unknown {
    if (!this.bindings()) return getAt(this.values, path);
    const node = boundAt(this.bound, path);
    if (!node) return undefined;
    // One value per node, so editors bound to it see a stable object.
    if (!this.literals.has(node)) this.literals.set(node, literalValue(node));
    return this.literals.get(node);
  }
  items(path: Path): unknown[] {
    const value = this.read(path);
    return Array.isArray(value) ? value : [];
  }
  entries(path: Path): { key: string; value: unknown }[] {
    const value = this.read(path);
    return isRecord(value)
      ? Object.entries(value).map(([key, item]) => ({ key, value: item }))
      : [];
  }
  choiceIndex(field: FieldInfo | null, value: unknown) {
    if (value === undefined || !field?.options) return -1;
    const text = JSON.stringify(value);
    return field.options.findIndex(
      (option) => JSON.stringify(option.value) === text,
    );
  }
  jsonText(path: Path) {
    const draft = this.drafts.get(this.key(path));
    if (draft !== undefined) return draft;
    const value = this.read(path);
    return value === undefined ? "" : JSON.stringify(value, null, 2);
  }
  dateText(path: Path, value: unknown) {
    return this.drafts.get(this.key(path)) ?? localDateTime(value);
  }

  // --- sources of a bound field ------------------------------------------------
  modeAt(path: Path, widget: Widget): Mode {
    if (!this.bindings() || fixed.has(widget)) return "value";
    const chosen = this.chosen.get(this.key(path));
    if (chosen) return chosen;
    const node = boundAt(this.bound, path);
    if (!node) return "value";
    if (node.kind === "ref") return "data";
    if (node.kind === "formula") return "formula";
    if (node.kind === "object" && widget === "group") return "value";
    return literalValue(node) === undefined ? "formula" : "value";
  }
  pointerAt(path: Path) {
    const draft = this.drafts.get(this.key(path));
    if (draft !== undefined) return draft;
    const node = boundAt(this.bound, path);
    return node?.kind === "ref" ? node.pointer : "";
  }
  formulaAt(path: Path): unknown {
    const node = boundAt(this.bound, path);
    if (!node) return emptyFormula;
    if (!this.expressions.has(node)) this.expressions.set(node, encode(node));
    return this.expressions.get(node);
  }
  /** Switches a field's source; what the previous source held is kept. */
  switchMode(path: Path, widget: Widget, mode: Mode) {
    const key = this.key(path);
    const current = this.modeAt(path, widget);
    if (mode === current) return;
    const memory = this.memory.get(key) ?? {};
    const node = boundAt(this.bound, path);
    if (node) memory[current] = node;
    this.memory.set(key, memory);
    let next = memory[mode];
    // A formula starts from the current value; with none, nothing is
    // written until the formula is edited.
    if (!next && mode === "formula" && node) next = formula(encode(node));
    this.bound = this.place(path, next);
    this.chosen.set(key, mode);
    this.drafts.delete(key);
    this.clearErrors(key);
    this.publish();
    this.cdr.detectChanges();
    // A group, list or map is drawn differently per source, so the button
    // that was pressed may be gone: keep focus on the new one.
    const host = this.host.nativeElement as HTMLElement;
    if (!host.contains(document.activeElement)) {
      const field = host.querySelector(`[data-path="${CSS.escape(key)}"]`);
      const button = [
        ...(field?.querySelectorAll<HTMLElement>(
          `.binding-mode[data-mode="${mode}"]`,
        ) ?? []),
      ].find((element) => element.closest("[data-path]") === field);
      button?.focus();
    }
  }
  setReference(path: Path, text: string) {
    const key = this.key(path);
    const pointer = text.trim();
    this.drafts.set(key, text);
    this.chosen.set(key, "data");
    if (pointer && !validReference(pointer)) {
      this.fail(
        path,
        "Enter a data reference that starts with /, such as /input/customerId.",
      );
      return;
    }
    this.clearErrors(key);
    this.bound = this.place(path, pointer ? reference(pointer) : undefined);
    this.publish();
  }
  setFormula(path: Path, expression: unknown) {
    this.chosen.set(this.key(path), "formula");
    this.bound = this.place(path, formula(expression));
    this.clearErrors(this.key(path));
    this.publish();
  }
  formulaValidity(path: Path, valid: boolean) {
    this.nestedValidity(path, valid, "Finish the formula.");
  }
  nestedValidity(path: Path, valid: boolean, message = "Fix this value.") {
    if (valid) {
      if (!(this.key(path) in this.errors)) return;
      this.clearErrors(this.key(path));
      this.validityChange.emit(this.valid);
    } else this.fail(path, message);
  }

  // --- writing -----------------------------------------------------------------
  write(path: Path, value: unknown) {
    if (this.bindings()) {
      this.bound = this.place(
        path,
        value === undefined ? undefined : literalNode(value),
      );
      this.chosen.delete(this.key(path));
    } else
      this.values = (
        value === undefined
          ? removeAt(this.values, path)
          : setAt(this.values, path, value)
      ) as Data;
    this.publish();
  }
  /**
   * The bound tree with `node` at `path`, or without the field. A single
   * value (or a formula) standing where a group is needed is replaced by an
   * empty group first.
   */
  private place(path: Path, node: Bound | undefined): Bound {
    if (!node) {
      // Like plain data, a group the removal leaves empty goes too, so an
      // optional group the person cleared is unset again.
      let tree = unbind(this.bound, path);
      for (let depth = path.length - 1; depth > 0; depth--) {
        const parent = boundAt(tree, path.slice(0, depth));
        if (parent?.kind !== "object" || Object.keys(parent.entries).length)
          break;
        tree = unbind(tree, path.slice(0, depth));
      }
      return tree;
    }
    let tree = this.bound;
    for (let depth = 1; depth < path.length; depth++) {
      const parent = boundAt(tree, path.slice(0, depth));
      const list = typeof path[depth] === "number";
      if (parent && (list ? parent.kind !== "array" : parent.kind !== "object"))
        tree = bind(
          tree,
          path.slice(0, depth),
          list ? { kind: "array", items: [] } : { kind: "object", entries: {} },
        );
    }
    return bind(tree, path, node);
  }
  private missing() {
    return missingBound(this.schema(), encode(this.bound));
  }
  private publish() {
    this.validityChange.emit(this.valid);
    if (this.bindings()) {
      const expression = encode(this.bound);
      this.expressionChange.emit(expression);
      this.missingChange.emit(missingBound(this.schema(), expression));
    } else this.dataChange.emit(structuredClone(this.values));
  }
  private fail(path: Path, message: string, element?: HTMLInputElement) {
    this.errors = { ...this.errors, [this.key(path)]: message };
    element?.setCustomValidity(message);
    this.validityChange.emit(false);
  }
  private clearErrors(key: string, element?: HTMLInputElement) {
    element?.setCustomValidity("");
    if (!(key in this.errors)) return;
    const { [key]: _, ...rest } = this.errors;
    this.errors = rest;
  }
  setChoice(path: Path, event: Event, field: FieldInfo) {
    const index = (event.target as HTMLSelectElement).value;
    this.write(
      path,
      index === "" ? undefined : field.options?.[Number(index)]?.value,
    );
  }
  setTristate(path: Path, event: Event) {
    const text = (event.target as HTMLSelectElement).value;
    this.write(path, text === "" ? undefined : text === "true");
  }
  setBoolean(path: Path, event: Event) {
    this.write(path, (event.target as HTMLInputElement).checked);
  }
  setNull(path: Path, event: Event) {
    this.drafts.delete(this.key(path));
    this.clearErrors(this.key(path));
    this.write(
      path,
      (event.target as HTMLInputElement).checked ? null : undefined,
    );
  }
  setScalar(
    path: Path,
    event: Event,
    field: FieldInfo,
    widget: Widget,
    name?: string,
  ) {
    const element = event.target as HTMLInputElement;
    const text = element.value;
    const key = this.key(path);
    const label = name || field.label;
    if (widget === "datetime") {
      this.drafts.set(key, text);
      const stored = storedDateTime(text);
      if (text && !stored) {
        this.fail(path, `${label} must be a date and time.`, element);
        return;
      }
      this.clearErrors(key, element);
      this.write(path, stored);
      return;
    }
    if (widget !== "number" && widget !== "integer") {
      const problem = formatProblem(widget, text, label);
      if (problem) {
        this.fail(path, problem, element);
        return;
      }
      this.clearErrors(key, element);
      this.write(path, text === "" ? undefined : text);
      return;
    }
    // A number input reports "" for partial text such as "-"; keep it on screen.
    if (element.validity.badInput) {
      this.fail(path, `${label} must be a number.`, element);
      return;
    }
    const value = text.trim() === "" ? undefined : Number(text);
    if (
      value !== undefined &&
      widget === "integer" &&
      !Number.isInteger(value)
    ) {
      this.fail(path, `${label} must be a whole number.`, element);
      return;
    }
    if (value === undefined && typeof path.at(-1) === "number") {
      this.fail(path, "Enter a number.", element);
      return;
    }
    this.clearErrors(key, element);
    this.write(path, value);
  }
  setJson(path: Path, event: Event, field: FieldInfo | null, name?: string) {
    const element = event.target as HTMLInputElement;
    const text = element.value;
    const key = this.key(path);
    const label = name || field?.label || "This value";
    this.drafts.set(key, text);
    if (!text.trim()) {
      this.clearErrors(key, element);
      this.write(path, undefined);
      return;
    }
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      this.fail(path, `${label} must contain valid JSON.`, element);
      return;
    }
    const type = field?.schema["type"];
    if (type === "object" && !isRecord(value)) {
      this.fail(path, `${label} must be a JSON object.`, element);
      return;
    }
    if (type === "array" && !Array.isArray(value)) {
      this.fail(path, `${label} must be a JSON list.`, element);
      return;
    }
    this.clearErrors(key, element);
    this.write(path, value);
  }

  // --- lists, tables and maps ---------------------------------------------------
  private initial(field: FieldInfo | null): unknown {
    if (!field) return "";
    if (field.kind === "choice") return field.options?.[0]?.value ?? "";
    if (field.kind === "boolean") return false;
    if (field.kind === "number" || field.kind === "integer")
      return typeof field.schema["minimum"] === "number"
        ? field.schema["minimum"]
        : 0;
    if (field.kind === "object") return prepareData(field.schema, {}).data;
    return scalarMember(field) ? "" : {};
  }
  addItem(path: Path, item: FieldInfo | null, label: string) {
    const list = this.items(path);
    this.write(path, [...list, this.initial(item)]);
    this.announce(`Added ${label} item ${list.length + 1}.`);
    this.focusSoon(this.idFor(path.concat(list.length)));
  }
  addRow(path: Path, field: FieldInfo) {
    this.addItem(path, this.member(field), field.label);
  }
  removeItem(path: Path, index: number, label: string) {
    const items = this.items(path);
    const list = items.filter((_, i) => i !== index);
    this.forgetRows(path, items.map((_, i) => i).slice(index), index);
    this.write(path, list);
    this.announce(`Removed ${label} item ${index + 1}.`);
    this.focusSoon(this.idFor(path) + "-add");
  }
  addEntry(path: Path, entry: FieldInfo | null, label: string) {
    const current = isRecord(this.read(path)) ? (this.read(path) as Data) : {};
    let n = Object.keys(current).length + 1;
    while (`entry${n}` in current) n++;
    this.write(path, { ...current, [`entry${n}`]: this.initial(entry) });
    this.announce(`Added ${label} entry ${Object.keys(current).length + 1}.`);
    this.focusSoon(this.idFor(path.concat(`entry${n}`)) + "-key");
  }
  removeEntry(path: Path, name: string, label: string) {
    const current = this.read(path);
    if (!isRecord(current)) return;
    const { [name]: _, ...rest } = current;
    const names = Object.keys(current);
    const index = names.indexOf(name);
    this.forgetRows(path, names.slice(index), index);
    this.write(path, rest);
    this.announce(`Removed ${label} entry ${name}.`);
    this.focusSoon(this.idFor(path) + "-add");
  }
  renameEntry(path: Path, name: string, index: number, event: Event) {
    const element = event.target as HTMLInputElement;
    const next = element.value.trim();
    const current = this.read(path);
    const errorKey = `${this.key(path)}#${index}`;
    if (!isRecord(current) || next === name) {
      this.clearErrors(errorKey, element);
      this.validityChange.emit(this.valid);
      return;
    }
    if (!next || Object.hasOwn(current, next)) {
      this.errors = {
        ...this.errors,
        [errorKey]: next
          ? `There is already an entry named ${next}.`
          : "Enter a name.",
      };
      element.setCustomValidity(this.errors[errorKey]);
      this.validityChange.emit(false);
      return;
    }
    this.clearErrors(errorKey, element);
    // The row keeps what it shows, so its typed text and errors move along.
    this.moveRow(path.concat(name), path.concat(next));
    // Same order, new name.
    this.write(
      path,
      Object.fromEntries(
        Object.entries(current).map(([key, value]) => [
          key === name ? next : key,
          value,
        ]),
      ),
    );
    this.cdr.detectChanges();
  }
  /**
   * After a row is removed, the rows from `index` on show other values (rows
   * are rendered by position), so their typed text, errors and entry-name
   * errors are dropped; the rows before it keep theirs.
   */
  private forgetRows(
    path: Path,
    members: readonly (string | number)[],
    index: number,
  ) {
    const base = this.key(path);
    const prefixes = members.map((member) => this.key(path.concat(member)));
    const stale = (key: string) => {
      const name = key.startsWith(base + "#") ? key.slice(base.length + 1) : "";
      return (
        (/^\d+$/.test(name) && Number(name) >= index) ||
        prefixes.some(
          (prefix) => key === prefix || key.startsWith(prefix + "\u001f"),
        )
      );
    };
    this.errors = Object.fromEntries(
      Object.entries(this.errors).filter(([key]) => !stale(key)),
    );
    for (const key of [...this.drafts.keys()])
      if (stale(key)) this.drafts.delete(key);
  }
  /** Moves the typed text and errors of a renamed map entry to its new name. */
  private moveRow(from: Path, to: Path) {
    const old = this.key(from),
      next = this.key(to);
    const moved = (key: string) =>
      key === old || key.startsWith(old + "\u001f")
        ? next + key.slice(old.length)
        : key;
    this.errors = Object.fromEntries(
      Object.entries(this.errors).map(([key, message]) => [
        moved(key),
        message,
      ]),
    );
    for (const [key, text] of [...this.drafts])
      if (moved(key) !== key) {
        this.drafts.delete(key);
        this.drafts.set(moved(key), text);
      }
  }
  /**
   * Rows were added, removed or renamed: render now, so the next key press
   * or click reaches the row's new path (a scheduled render may come later).
   */
  private announce(text: string) {
    this.status = text;
    this.cdr.detectChanges();
  }
  /** Focuses an element of this form after the next render. */
  private focusSoon(id: string) {
    setTimeout(() => {
      const element = (
        this.host.nativeElement as HTMLElement
      ).querySelector<HTMLElement>(`#${CSS.escape(id)}`);
      const control = element?.matches("input, select, textarea, button")
        ? element
        : element?.querySelector<HTMLElement>("input, select, textarea");
      (control ?? element)?.focus();
    });
  }
}
