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
  EventEmitter,
  Input,
  Output,
  OnChanges,
  SimpleChanges,
  forwardRef,
} from "@angular/core";
import { Step, Workflow } from "./model";
import { ConditionEditor } from "./designer/condition-editor";
import { parseCondition } from "./designer/conditions";
import { ReferenceCombobox } from "./forms/ui/reference-combobox";
import type { ReferenceOption } from "./forms/ui/reference-combobox";
import { LazyComponent, type LazyOutputs } from "./forms/ui/lazy-component";
import type { DesignerPurpose, InferSchema } from "./forms/ui/schema-designer";
import {
  referencesAt,
  type ReferenceContext,
} from "./forms/core/reference-context";
import {
  DurationUnit,
  durationSeconds,
  durationUnits,
  splitDuration,
} from "./designer/canvas-summary";
type EditableDocument = Step | Workflow;
export const supportedOperators = [
  "eq",
  "ne",
  "lt",
  "lte",
  "gt",
  "gte",
  "and",
  "or",
  "not",
  "exists",
  "coalesce",
];
/** Operators in words; the stored name stays the language's own. */
export const operatorLabels: Record<string, string> = {
  eq: "Equals",
  ne: "Does not equal",
  lt: "Less than",
  lte: "At most",
  gt: "Greater than",
  gte: "At least",
  and: "All of",
  or: "Any of",
  not: "Not",
  exists: "Is present",
  coalesce: "First available",
};
/**
 * Where an expression's value comes from, in the words the action input
 * form uses too: Value, Data, Formula, Fields, List.
 */
export const expressionModes = [
  { value: "literal", label: "Value", hint: "Type the value" },
  {
    value: "ref",
    label: "Data",
    hint: "Use the workflow input or a step's output",
  },
  { value: "op", label: "Formula", hint: "Combine data with an operation" },
  { value: "object", label: "Fields", hint: "Build an object field by field" },
  { value: "array", label: "List", hint: "Build a list item by item" },
];
/** A formula whose operation isn't chosen yet ("Choose a formula…"). */
export const unchosenFormula = { op: { name: "", args: [] } };
/** True for a decision case's condition: `cases/<n>/when`. */
const conditionPath = (path: Path) =>
  path.length === 3 && path[0] === "cases" && path[2] === "when";
type Path = (string | number)[];
type RecordValue = Record<string, unknown>;
const object = (v: unknown): v is RecordValue =>
  !!v && typeof v === "object" && !Array.isArray(v);
const semverPattern = String.raw`(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?`;
const semver = new RegExp("^" + semverPattern + "$");
const versionedReference = new RegExp(
  "^[A-Za-z0-9][A-Za-z0-9_.-]*@" + semverPattern + "$",
);
const name = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const pointer = /^(?:\/(?:[^~/]|~[01])*)*$/;
interface Field {
  label: string;
  path: Path;
  kind:
    | "text"
    | "name"
    | "positive"
    | "duration"
    | "expression"
    | "value"
    | "schema"
    | "decisions";
  optional?: boolean;
}
export function propertyFields(step: EditableDocument): Field[] {
  const f = (
    label: string,
    key: string,
    kind: Field["kind"],
    optional = false,
  ): Field => ({ label, path: [key], kind, optional });
  if (step.kind === "Workflow")
    return [
      { label: "Name", path: ["metadata", "name"], kind: "name" },
      { label: "Version", path: ["metadata", "version"], kind: "text" },
      { label: "Input schema", path: ["spec", "inputSchema"], kind: "schema" },
      {
        label: "Output schema",
        path: ["spec", "outputSchema"],
        kind: "schema",
      },
      {
        label: "Connection slots",
        path: ["spec", "connections"],
        kind: "value",
        optional: true,
      },
      {
        label: "Workflow timeout",
        path: ["spec", "timeoutSeconds"],
        kind: "duration",
        optional: true,
      },
      {
        label: "Workflow output",
        path: ["spec", "output"],
        kind: "expression",
      },
    ];
  const result: Record<string, Field[]> = {
    action: [
      f("Action version", "uses", "text"),
      f("Connection slot", "connection", "name", true),
      f("Input", "with", "expression"),
    ],
    transform: [f("Value", "value", "expression")],
    wait: [f("Duration", "durationSeconds", "duration")],
    signal: [
      f("Signal name", "name", "name"),
      f("Timeout", "timeoutSeconds", "duration"),
      f("Payload schema", "payloadSchema", "schema"),
    ],
    fail: [f("Error code", "code", "name"), f("Message", "message", "text")],
    humanTask: [
      f("Assignment binding", "assignment", "name"),
      f("Title", "title", "expression"),
      f("Context", "context", "expression"),
      f("Decisions", "decisions", "decisions"),
      f("Due after", "dueSeconds", "duration", true),
      f("Expires after", "expirySeconds", "duration", true),
      f("Form schema", "formSchema", "schema"),
    ],
    parallel: [f("Concurrency", "concurrency", "positive")],
    switch: [],
  };
  const fields = result[step.kind] ?? [];
  if (step.kind === "switch") {
    const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
    cases.forEach((_, i) => {
      fields.push({
        label: `Case ${i + 1} condition`,
        path: ["cases", i, "when"],
        kind: "expression",
      });
      fields.push({
        label: `Case ${i + 1} output`,
        path: ["cases", i, "output"],
        kind: "expression",
      });
    });
    fields.push({
      label: "Otherwise output",
      path: ["default", "output"],
      kind: "expression",
    });
  }
  if (step.kind === "parallel" && object(step["branches"]))
    Object.keys(step["branches"]).forEach((key) =>
      fields.push({
        label: `${key} output`,
        path: ["branches", key, "output"],
        kind: "expression",
      }),
    );
  return fields;
}
function expressionValid(v: unknown): boolean {
  if (!object(v) || Object.keys(v).length !== 1) return false;
  if ("literal" in v) return true;
  if ("ref" in v) return typeof v["ref"] === "string" && pointer.test(v["ref"]);
  if ("object" in v)
    return (
      object(v["object"]) && Object.values(v["object"]).every(expressionValid)
    );
  if ("array" in v)
    return Array.isArray(v["array"]) && v["array"].every(expressionValid);
  if (!("op" in v) || !object(v["op"])) return false;
  const op = v["op"];
  if (
    !supportedOperators.includes(String(op["name"])) ||
    !Array.isArray(op["args"]) ||
    !op["args"].every(expressionValid)
  )
    return false;
  if (
    op["name"] === "exists" &&
    !op["args"].every((arg) => object(arg) && "ref" in arg)
  )
    return false;
  const count = op["args"].length;
  return ["not", "exists"].includes(String(op["name"]))
    ? count === 1
    : ["and", "or", "coalesce"].includes(String(op["name"]))
      ? count >= 1
      : count === 2;
}
export class PropertyDraft {
  value: EditableDocument;
  errors = new Map<string, string>();
  constructor(step: EditableDocument) {
    this.value = structuredClone(step);
    this.validate();
  }
  reset(step: EditableDocument) {
    this.value = structuredClone(step);
    this.errors.clear();
    this.validate();
  }
  get valid() {
    return this.errors.size === 0;
  }
  get(path: Path): unknown {
    return path.reduce<unknown>(
      (v, key) =>
        object(v) || Array.isArray(v)
          ? (v as Record<string, unknown>)[key]
          : undefined,
      this.value,
    );
  }
  snapshot(): EditableDocument {
    return structuredClone(this.value);
  }
  set(path: Path, value: unknown): boolean {
    let target: unknown = this.value;
    for (const key of path.slice(0, -1))
      target = (target as Record<string, unknown>)[key];
    if (!target || typeof target !== "object") return false;
    const key = path.at(-1)!;
    if (value === undefined) delete (target as Record<string, unknown>)[key];
    else (target as Record<string, unknown>)[key] = structuredClone(value);
    this.validate();
    return this.valid;
  }
  validate() {
    this.errors.clear();
    for (const field of propertyFields(this.value)) {
      const v = this.get(field.path);
      let error = "";
      if (field.optional && v === undefined) continue;
      // An unset case condition is reported by the validator ("Choose when
      // this path applies.") and doesn't block applying other edits.
      if (conditionPath(field.path) && v === undefined) continue;
      if (
        field.kind === "positive" &&
        !(typeof v === "number" && Number.isSafeInteger(v) && v > 0)
      )
        error = "Enter a positive whole number.";
      if (
        field.kind === "duration" &&
        !(typeof v === "number" && Number.isSafeInteger(v) && v > 0)
      )
        error = "Enter a positive duration in whole seconds.";
      if (field.kind === "name" && !(typeof v === "string" && name.test(v)))
        error = "Use letters, numbers, dots, underscores or hyphens.";
      if (field.kind === "text" && !(typeof v === "string" && v.length > 0))
        error = "Enter a value.";
      if (
        field.path[0] === "uses" &&
        !(typeof v === "string" && versionedReference.test(v))
      )
        error = "Use action-name@1.0.0.";
      if (
        field.path.join("/") === "metadata/version" &&
        !(typeof v === "string" && semver.test(v))
      )
        error = "Use a semantic version such as 1.0.0.";
      if (
        ["inputSchema", "outputSchema"].includes(String(field.path[1])) &&
        !object(v)
      )
        error = "Schema must be an object.";
      if (
        field.path.join("/") === "spec/connections" &&
        v !== undefined &&
        !(
          object(v) &&
          Object.entries(v).every(
            ([key, value]) =>
              name.test(key) &&
              object(value) &&
              typeof value["connector"] === "string" &&
              versionedReference.test(value["connector"]) &&
              (value["required"] === undefined ||
                typeof value["required"] === "boolean"),
          )
        )
      )
        error =
          "Each slot requires connector name@version and optional required boolean.";
      if (field.kind === "expression" && !expressionValid(v))
        error = "Choose a literal or a valid JSON pointer reference.";
      if (
        field.kind === "decisions" &&
        !(
          Array.isArray(v) &&
          v.length > 0 &&
          v.length <= 32 &&
          v.every((x) => typeof x === "string" && name.test(x)) &&
          new Set(v).size === v.length
        )
      )
        error = "Enter 1–32 unique decision identifiers.";
      if (
        (field.path[0] === "formSchema" || field.path[0] === "payloadSchema") &&
        !object(v)
      )
        error = "Schema must be an object.";
      if (this.value.kind === "humanTask" && object(v) && "literal" in v) {
        if (
          field.path[0] === "title" &&
          !(
            typeof v["literal"] === "string" &&
            v["literal"].length > 0 &&
            v["literal"].length <= 512
          )
        )
          error = "Title must contain 1–512 characters.";
        if (field.path[0] === "context" && !object(v["literal"]))
          error = "Context literal must be an object.";
      }
      if (error) this.errors.set(field.path.join("/"), error);
    }
  }
}

@Component({
  selector: "weave-property-value",
  standalone: true,
  imports: [forwardRef(() => PropertyValue)],
  template: `<div class="value-editor">
    @if (depth > 6) {
      <details>
        <summary>Advanced value (preserved)</summary>
        <textarea
          [disabled]="readOnly"
          [value]="advanced"
          (input)="parseAdvanced($event)"
          aria-label="Advanced JSON value"
        ></textarea>
      </details>
    } @else {
      <select
        [disabled]="readOnly"
        (change)="changeType($event)"
        aria-label="Value type"
      >
        @for (t of types; track t) {
          <option [value]="t" [selected]="t === type">{{ t }}</option>
        }
      </select>
      @switch (type) {
        @case ("object") {
          <div class="nested-properties">
            @for (entry of entries; track entry.key + ":" + generation) {
              <div class="nested-row">
                <span>{{ entry.key }}</span
                ><weave-property-value
                  [value]="entry.value"
                  [depth]="depth + 1"
                  [readOnly]="readOnly"
                  (valueChange)="child(entry.key, $event)"
                  (validityChange)="childValidity(entry.key, $event)"
                /><button
                  type="button"
                  [disabled]="readOnly"
                  (click)="remove(entry.key)"
                  [attr.aria-label]="'Remove ' + entry.key"
                >
                  ×
                </button>
              </div>
            }
            <div class="add-property">
              <input
                #key
                placeholder="Property name"
                [disabled]="readOnly"
                aria-label="New property name"
              /><button
                type="button"
                [disabled]="readOnly"
                (click)="add(key.value); key.value = ''"
              >
                Add property
              </button>
            </div>
          </div>
        }
        @case ("array") {
          @for (entry of entries; track entry.key + ":" + generation) {
            <div class="nested-row">
              <span>{{ entry.key }}</span
              ><weave-property-value
                [value]="entry.value"
                [depth]="depth + 1"
                [readOnly]="readOnly"
                (valueChange)="child(entry.key, $event)"
                (validityChange)="childValidity(entry.key, $event)"
              /><button
                type="button"
                [disabled]="readOnly"
                (click)="remove(entry.key)"
                aria-label="Remove item"
              >
                ×
              </button>
            </div>
          }
          <button type="button" [disabled]="readOnly" (click)="add('')">
            Add item
          </button>
        }
        @case ("boolean") {
          <select
            [disabled]="readOnly"
            (change)="scalar($event)"
            aria-label="Boolean value"
          >
            <option value="true" [selected]="value === true">True</option>
            <option value="false" [selected]="value !== true">False</option>
          </select>
        }
        @case ("null") {
          <span>Null</span>
        }
        @default {
          <input
            [disabled]="readOnly"
            [type]="type === 'number' ? 'number' : 'text'"
            [value]="buffer"
            (input)="scalar($event)"
            aria-label="Property value"
          />
        }
      }
    }
    @if (error) {
      <small class="property-error" role="alert">{{ error }}</small>
    }
  </div>`,
  styleUrl: "./property-grid.css",
})
export class PropertyValue implements OnChanges {
  @Input() value: unknown = null;
  @Input() depth = 0;
  @Input() readOnly = false;
  @Output() valueChange = new EventEmitter<unknown>();
  @Output() validityChange = new EventEmitter<boolean>();
  types = ["string", "number", "boolean", "object", "array", "null"];
  buffer = "";
  advanced = "";
  error = "";
  invalid = new Set<string>();
  // Children are re-created only when the value changes from outside.
  generation = 0;
  private known: string | undefined;
  get type() {
    return this.value === null
      ? "null"
      : Array.isArray(this.value)
        ? "array"
        : typeof this.value === "object"
          ? "object"
          : typeof this.value;
  }
  get entries() {
    return Array.isArray(this.value)
      ? this.value.map((value, i) => ({ key: String(i), value }))
      : object(this.value)
        ? Object.entries(this.value).map(([key, value]) => ({ key, value }))
        : [];
  }
  ngOnChanges() {
    // Parents hand back a clone of what this editor emitted; keep local
    // validity unless the content really changed (undo, apply, another step).
    const incoming = JSON.stringify(this.value);
    if (incoming === this.known) return;
    const wasInvalid = !!this.error || this.invalid.size > 0;
    this.known = incoming;
    this.generation++;
    this.buffer = String(this.value ?? "");
    this.advanced = JSON.stringify(this.value, null, 2);
    this.error = "";
    this.invalid.clear();
    if (wasInvalid) this.validityChange.emit(true);
  }
  /** What each value type held, so switching back restores it (F8). */
  private memory = new Map<string, unknown>();
  changeType(e: Event) {
    if (this.readOnly) return;
    const type = (e.target as HTMLSelectElement).value;
    const defaults: RecordValue = {
      string: "",
      number: 0,
      boolean: false,
      object: {},
      array: [],
      null: null,
    };
    this.memory.set(this.type, structuredClone(this.value));
    this.emit(
      this.memory.has(type)
        ? structuredClone(this.memory.get(type))
        : defaults[type],
    );
  }
  emit(value: unknown) {
    if (this.readOnly) return;
    this.error = "";
    this.value = value;
    this.known = JSON.stringify(value);
    this.buffer = String(value ?? "");
    this.validityChange.emit(this.invalid.size === 0);
    if (!this.invalid.size) this.valueChange.emit(structuredClone(value));
  }
  scalar(e: Event) {
    const text = (e.target as HTMLInputElement).value;
    this.buffer = text;
    if (this.type === "number") {
      const value = Number(text);
      if (!text.trim() || !Number.isFinite(value)) {
        this.error = "Enter a finite number.";
        this.validityChange.emit(false);
        return;
      }
      this.emit(value);
    } else this.emit(this.type === "boolean" ? text === "true" : text);
  }
  child(key: string, value: unknown) {
    const next = structuredClone(this.value) as Record<string, unknown>;
    next[key] = value;
    this.emit(next);
  }
  childValidity(key: string, valid: boolean) {
    if (valid) this.invalid.delete(key);
    else this.invalid.add(key);
    this.validityChange.emit(this.invalid.size === 0);
  }
  add(key: string) {
    if (this.readOnly) return;
    if (Array.isArray(this.value)) {
      this.emit([...this.value, ""]);
      return;
    }
    if (!key.trim() || !object(this.value) || Object.hasOwn(this.value, key)) {
      this.error = "Enter a new, nonempty property name.";
      this.validityChange.emit(false);
      return;
    }
    this.emit({ ...this.value, [key]: "" });
  }
  remove(key: string) {
    if (this.readOnly) return;
    this.invalid.delete(key);
    if (Array.isArray(this.value))
      this.emit(this.value.filter((_, i) => i !== Number(key)));
    else if (object(this.value)) {
      const next = { ...this.value };
      delete next[key];
      this.emit(next);
    }
  }
  parseAdvanced(e: Event) {
    if (this.readOnly) return;
    this.advanced = (e.target as HTMLTextAreaElement).value;
    try {
      this.emit(JSON.parse(this.advanced));
    } catch {
      this.error = "Invalid JSON; the committed value is preserved.";
      this.validityChange.emit(false);
    }
  }
}

let expressionSequence = 0;

@Component({
  selector: "weave-expression-editor",
  standalone: true,
  imports: [
    PropertyValue,
    ReferenceCombobox,
    forwardRef(() => ExpressionEditor),
  ],
  styleUrl: "./property-grid.css",
  template: `<div class="expression-editor">
    <div class="expression-head">
      @if (heading) {
        <span class="expression-label" [id]="prefix + '-label'">{{
          heading
        }}</span>
      }
      <span
        class="mode-switch"
        role="group"
        [attr.aria-label]="label + ' expression mode'"
      >
        @for (option of modes; track option.value) {
          <button
            type="button"
            [attr.aria-pressed]="option.value === mode"
            [attr.data-mode]="option.value"
            [title]="option.hint"
            [disabled]="readOnly"
            (click)="setMode(option.value)"
          >
            {{ option.label }}
          </button>
        }
      </span>
    </div>
    @if (mode === "literal") {
      <weave-property-value
        [value]="body"
        [readOnly]="readOnly"
        (valueChange)="replaceBody($event)"
        (validityChange)="validityChange.emit($event)"
      />
    } @else if (mode === "ref") {
      <weave-reference-combobox
        [value]="refText"
        [options]="references"
        [ariaLabel]="label + ' reference'"
        placeholder="/input/customer"
        [disabled]="readOnly"
        (valueChange)="replaceBody($event)"
      />
    } @else {
      @if (mode === "op") {
        <select
          [disabled]="readOnly"
          (change)="changeOperator($event)"
          [attr.aria-label]="label + ' operator'"
          [attr.aria-invalid]="!operator || null"
        >
          <option value="" [selected]="!operator" disabled>
            Choose a formula…
          </option>
          @for (name of operators; track name) {
            <option [value]="name" [selected]="name === operator">
              {{ operatorLabel(name) }}
            </option>
          }
        </select>
      }
      @for (entry of entries; track entry.key + ":" + generation) {
        <div class="expression-argument">
          @if (mode === "object") {
            <label
              >Key<input
                [disabled]="readOnly"
                [value]="entry.key"
                (change)="rename(entry.key, text($event))"
            /></label>
          }
          <weave-expression-editor
            [value]="entry.value"
            [readOnly]="readOnly"
            [references]="references"
            [heading]="
              mode === 'object' ? '' : 'Argument ' + (Number(entry.key) + 1)
            "
            [label]="label + ' ' + entry.key"
            (valueChange)="update(entry.key, $event)"
            (validityChange)="childValidity(entry.key, $event)"
          />
          <button
            type="button"
            [disabled]="readOnly"
            (click)="remove(entry.key)"
          >
            Remove {{ mode === "object" ? "field" : "argument" }}
          </button>
        </div>
      }
      @if (mode !== "op" || operator) {
        <button type="button" [disabled]="readOnly" (click)="add()">
          Add {{ mode === "object" ? "field" : "argument" }}
        </button>
      }
    }
    @if (error) {
      <p role="alert" class="property-error">{{ error }}</p>
    }
  </div>`,
})
export class ExpressionEditor implements OnChanges {
  @Input() value: unknown = { literal: null };
  @Input() readOnly = false;
  @Input() label = "Expression";
  /** Visible label beside the mode switch; empty when the host labels it. */
  @Input() heading = "";
  /** Data the expression may read here (forms/core/scope.ts). */
  @Input() references: readonly ReferenceOption[] = [];
  @Output() valueChange = new EventEmitter<unknown>();
  @Output() validityChange = new EventEmitter<boolean>();
  Number = Number;
  operators = supportedOperators;
  modes = expressionModes;
  readonly prefix = `expression-${++expressionSequence}`;
  operatorLabel(name: string) {
    return operatorLabels[name] ?? name;
  }
  current: RecordValue = { literal: null };
  error = "";
  invalid = new Set<string>();
  // Key renames that collide stay invalid until fixed, even when a sibling changes.
  renameErrors = new Map<string, string>();
  generation = 0;
  private known: string | undefined;
  ngOnChanges() {
    const incoming = JSON.stringify(this.value);
    if (incoming === this.known) return;
    const wasInvalid =
      !!this.error || this.invalid.size > 0 || this.renameErrors.size > 0;
    this.known = incoming;
    this.generation++;
    this.current = object(this.value)
      ? structuredClone(this.value)
      : { literal: null };
    this.invalid.clear();
    this.renameErrors.clear();
    this.error = "";
    if (wasInvalid) this.validityChange.emit(true);
  }
  get mode() {
    return Object.keys(this.current)[0] ?? "literal";
  }
  get body() {
    return this.current[this.mode];
  }
  get refText() {
    return typeof this.body === "string" ? this.body : "";
  }
  get operator() {
    return object(this.body) ? String(this.body["name"] ?? "") : "";
  }
  get entries() {
    const values =
      this.mode === "op" && object(this.body) ? this.body["args"] : this.body;
    return object(values) || Array.isArray(values)
      ? Object.entries(values).map(([key, value]) => ({ key, value }))
      : [];
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  emit() {
    const structural = expressionValid(this.current);
    const valid = !this.invalid.size && !this.renameErrors.size && structural;
    this.error =
      [...this.renameErrors.values()][0] ??
      (this.mode === "op" && !this.operator
        ? "Choose a formula."
        : !this.invalid.size && !structural
          ? "Fix references and operation arguments before applying."
          : "");
    this.validityChange.emit(valid);
    if (valid && !this.readOnly) {
      this.known = JSON.stringify(this.current);
      this.valueChange.emit(structuredClone(this.current));
    }
  }
  replaceBody(value: unknown) {
    if (this.readOnly) return;
    this.current = { [this.mode]: value };
    this.emit();
  }
  /** What each mode held, so switching back restores it (F8). */
  private memory = new Map<string, unknown>();
  changeMode(event: Event) {
    this.setMode(this.text(event));
  }
  setMode(mode: string) {
    if (this.readOnly || mode === this.mode) return;
    this.memory.set(this.mode, structuredClone(this.body));
    this.invalid.clear();
    this.renameErrors.clear();
    this.current = {
      [mode]: this.memory.has(mode)
        ? structuredClone(this.memory.get(mode))
        : mode === "ref"
          ? "/input"
          : mode === "op"
            ? structuredClone(unchosenFormula.op)
            : mode === "object"
              ? {}
              : mode === "array"
                ? []
                : null,
    };
    this.emit();
  }
  changeOperator(event: Event) {
    if (this.readOnly) return;
    const name = this.text(event);
    let args = this.entries.map((e) => e.value);
    if (["not", "exists"].includes(name))
      args = [
        name === "exists" ? { ref: "/input" } : (args[0] ?? { literal: true }),
      ];
    else if (!["and", "or", "coalesce"].includes(name))
      args = [args[0] ?? { literal: null }, args[1] ?? { literal: null }];
    else if (!args.length) args = [{ literal: null }, { literal: null }];
    this.current = { op: { name, args } };
    this.emit();
  }
  childValidity(key: string, valid: boolean) {
    valid ? this.invalid.delete(key) : this.invalid.add(key);
    this.validityChange.emit(
      !this.invalid.size &&
        !this.renameErrors.size &&
        expressionValid(this.current),
    );
  }
  update(key: string, value: unknown) {
    if (this.readOnly) return;
    this.invalid.delete(key);
    const next = structuredClone(this.body);
    if (this.mode === "op" && object(next))
      (next["args"] as unknown[])[Number(key)] = value;
    else (next as RecordValue)[key] = value;
    this.replaceBody(next);
  }
  add() {
    if (this.readOnly) return;
    const next = structuredClone(this.body);
    if (this.mode === "object" && object(next)) {
      let n = 1;
      while (`field${n}` in next) n++;
      next[`field${n}`] = { literal: null };
    } else if (this.mode === "op" && object(next))
      (next["args"] as unknown[]).push({ literal: null });
    else (next as unknown[]).push({ literal: null });
    this.replaceBody(next);
  }
  remove(key: string) {
    if (this.readOnly) return;
    const next = structuredClone(this.body);
    this.invalid.delete(key);
    this.renameErrors.delete(key);
    if (this.mode === "object" && object(next)) delete next[key];
    else
      (this.mode === "op" && object(next)
        ? (next["args"] as unknown[])
        : (next as unknown[])
      ).splice(Number(key), 1);
    this.replaceBody(next);
  }
  rename(key: string, name: string) {
    if (this.readOnly || !object(this.body)) return;
    if (name === key) {
      if (this.renameErrors.delete(key)) this.emit();
      return;
    }
    if (!name || Object.hasOwn(this.body, name)) {
      this.renameErrors.set(key, "Use a unique nonempty key.");
      this.error = "Use a unique nonempty key.";
      this.validityChange.emit(false);
      return;
    }
    this.renameErrors.delete(key);
    const next = Object.fromEntries(
      Object.entries(this.body).map(([entry, value]) => [
        entry === key ? name : entry,
        value,
      ]),
    );
    this.replaceBody(next);
  }
}

let gridSequence = 0;

@Component({
  selector: "weave-step-property-grid",
  standalone: true,
  imports: [PropertyValue, ExpressionEditor, ConditionEditor, LazyComponent],
  styleUrl: "./property-grid.css",
  template: `@if (draft) {
    <div class="property-grid">
      @for (field of fields; track key(field)) {
        @if (field.kind === "schema") {
          <!-- Schemas get the full width: a field list with a preview (WP-15). -->
          <div
            class="property-field wide schema-property"
            [attr.data-field]="key(field)"
          >
            @if (raw.has(key(field))) {
              <div class="schema-property-head">
                <strong>{{ field.label }}</strong>
                <button
                  type="button"
                  class="text-link"
                  (click)="toggleRaw(field)"
                >
                  Use the field editor
                </button>
              </div>
              <weave-property-value
                [value]="draft.get(field.path)"
                [readOnly]="readOnly"
                (valueChange)="change(field, $event)"
                (validityChange)="validity(field, $event)"
              />
            } @else {
              <ng-container
                [weaveLazy]="loadDesigner"
                [lazyInputs]="{
                  heading: field.label,
                  purpose: purposeOf(field),
                  schema: draft.get(field.path),
                  revision,
                  readonly: readOnly,
                  inferSchema,
                }"
                [lazyOutputs]="designerOutputs(field)"
              />
              <button
                type="button"
                class="text-link"
                (click)="toggleRaw(field)"
              >
                Edit {{ field.label.toLowerCase() }} as JSON
              </button>
            }
            @if (error(field)) {
              <small class="property-error" role="alert">{{
                error(field)
              }}</small>
            }
          </div>
        } @else if (field.kind === "expression") {
          <div class="property-field wide" [attr.data-field]="key(field)">
            @if (isCondition(field) && !formulaFields.has(key(field))) {
              <span class="property-label" [id]="labelId(field)">{{
                field.label
              }}</span>
              <weave-condition-editor
                [value]="draft.get(field.path)"
                [label]="field.label"
                [labelId]="labelId(field)"
                [references]="referencesFor(field)"
                [readOnly]="readOnly"
                (valueChange)="change(field, $event)"
                (validityChange)="validity(field, $event)"
                (formula)="useFormula(field, true)"
              />
            } @else {
              <weave-expression-editor
                [value]="draft.get(field.path)"
                [readOnly]="readOnly"
                [references]="referencesFor(field)"
                [label]="field.label"
                [heading]="field.label"
                (valueChange)="change(field, $event)"
                (validityChange)="validity(field, $event)"
              />
              @if (isCondition(field) && rowsFit(field)) {
                <button
                  type="button"
                  class="text-link"
                  (click)="useFormula(field, false)"
                >
                  Use condition rows
                </button>
              }
            }
            @if (error(field)) {
              <small class="property-error" role="alert">{{
                error(field)
              }}</small>
            }
          </div>
        } @else {
          <div class="property-field" [attr.data-field]="key(field)">
            <span class="property-label" [id]="labelId(field)">{{
              field.label
            }}</span>
            <div class="property-control">
              @if (field.kind === "duration") {
                <span class="duration-field">
                  <input
                    type="number"
                    min="0"
                    step="any"
                    inputmode="decimal"
                    [disabled]="readOnly"
                    [value]="durationAmount(field)"
                    [attr.aria-invalid]="!!error(field)"
                    (input)="durationInput(field, $event)"
                    [attr.aria-label]="field.label"
                  /><select
                    [disabled]="readOnly"
                    [attr.aria-label]="field.label + ' unit'"
                    (change)="durationUnit(field, $event)"
                  >
                    @for (option of units; track option.unit) {
                      <option
                        [value]="option.unit"
                        [selected]="option.unit === unitOf(field)"
                      >
                        {{ option.label }}
                      </option>
                    }
                  </select>
                </span>
              } @else if (
                field.kind === "value" || field.kind === "decisions"
              ) {
                <weave-property-value
                  [value]="draft.get(field.path)"
                  [readOnly]="readOnly"
                  (valueChange)="change(field, $event)"
                  (validityChange)="validity(field, $event)"
                />
              } @else {
                <input
                  [disabled]="readOnly"
                  [type]="field.kind === 'positive' ? 'number' : 'text'"
                  [attr.min]="field.kind === 'positive' ? 1 : null"
                  [value]="text(field)"
                  (input)="scalar(field, $event)"
                  [attr.aria-label]="field.label"
                  [attr.aria-invalid]="!!error(field) || null"
                />
              }
              @if (error(field)) {
                <small class="property-error" role="alert">{{
                  error(field)
                }}</small>
              }
            </div>
          </div>
        }
      }
    </div>
    @if (step.kind === "switch" || step.kind === "parallel") {
      <p class="property-hint">
        Nested steps are edited on the canvas. Editing a branch output preserves
        its steps.
      </p>
    }
    @if (unknownKeys.length) {
      <details>
        <summary>Additional properties preserved</summary>
        <p class="property-hint">
          {{ unknownKeys.join(", ") }}. Edit unsupported properties in the
          source editor.
        </p>
      </details>
    }
  }`,
})
export class StepPropertyGrid implements OnChanges {
  @Input({ required: true }) step!: EditableDocument;
  @Input() readOnly = false;
  @Input() hiddenFields: string[] = [];
  /** Where these fields sit, for data suggestions (WP-10). */
  @Input() scope: ReferenceContext | null = null;
  /** Sample-to-schema inference for the schema editors; null hides it. */
  @Input() inferSchema: InferSchema | null = null;
  @Output() stepChange = new EventEmitter<EditableDocument>();
  @Output() validityChange = new EventEmitter<boolean>();
  draft!: PropertyDraft;
  fields: Field[] = [];
  buffers = new Map<string, string>();
  invalid = new Set<string>();
  /** The unit chosen for each duration field; seconds are what is stored. */
  durationUnits = new Map<string, DurationUnit>();
  units = durationUnits;
  private reported: boolean | undefined;
  /** Schema fields edited as JSON instead of with the field editor. */
  raw = new Set<string>();
  /** The schema designer loads with the first schema field shown (WP-15). */
  loadDesigner = () =>
    import("./forms/ui/schema-designer").then((m) => m.SchemaDesigner);
  private outputs = new Map<string, LazyOutputs>();
  designerOutputs(field: Field): LazyOutputs {
    let outputs = this.outputs.get(this.key(field));
    if (!outputs)
      this.outputs.set(
        this.key(field),
        (outputs = {
          schemaChange: (schema: unknown) => this.change(field, schema),
          validityChange: (valid: boolean) => this.validity(field, valid),
        }),
      );
    return outputs;
  }
  /** Bumped for each step loaded, so schema editors reload. */
  revision = 0;
  /** Case conditions the person chose to edit as a formula. */
  formulaFields = new Set<string>();
  private readonly prefix = `property-grid-${++gridSequence}`;
  labelId(field: Field) {
    return `${this.prefix}-${this.key(field).replace(/[^A-Za-z0-9_-]/g, "-")}`;
  }
  isCondition(field: Field) {
    return this.draft.value.kind === "switch" && conditionPath(field.path);
  }
  /** Whether condition rows can show this condition (unset counts). */
  rowsFit(field: Field) {
    return parseCondition(this.draft.get(field.path)) !== null;
  }
  /** Rows can't show a condition: it opens in the formula editor. */
  private formulaOnly(field: Field) {
    return this.isCondition(field) && !this.rowsFit(field);
  }
  useFormula(field: Field, formula: boolean) {
    const key = this.key(field);
    if (formula) this.formulaFields.add(key);
    else this.formulaFields.delete(key);
    this.invalid.delete(key);
    this.report(this.draft.valid && this.invalid.size === 0);
  }
  ngOnChanges(changes?: SimpleChanges) {
    if (!changes || changes["step"] || !this.draft) {
      this.revision++;
      this.draft = new PropertyDraft(this.step);
      this.buffers.clear();
      this.durationUnits.clear();
      this.invalid.clear();
      this.formulaFields.clear();
    }
    this.fields = propertyFields(this.draft.value).filter(
      (field) => !this.hiddenFields.includes(String(field.path[0])),
    );
    for (const field of this.fields)
      if (this.formulaOnly(field)) this.formulaFields.add(this.key(field));
    this.report(this.draft.valid && this.invalid.size === 0);
  }
  private report(valid: boolean) {
    if (valid === this.reported) return;
    this.reported = valid;
    this.validityChange.emit(valid);
  }
  key(field: Field) {
    return field.path.join("/");
  }
  /** Data an expression field may read, by its pointer in the step (or workflow). */
  referencesFor(field: Field): readonly ReferenceOption[] {
    return referencesAt(
      this.scope,
      field.path
        .map(
          (part) => "/" + String(part).replace(/~/g, "~0").replace(/\//g, "~1"),
        )
        .join(""),
    );
  }
  purposeOf(field: Field): DesignerPurpose {
    return (
      (
        {
          inputSchema: "input",
          outputSchema: "output",
          payloadSchema: "payload",
          formSchema: "form",
        } as Record<string, DesignerPurpose>
      )[String(field.path.at(-1))] ?? "other"
    );
  }
  toggleRaw(field: Field) {
    const key = this.key(field);
    if (!this.raw.delete(key)) this.raw.add(key);
    // The other editor starts from the stored schema and reports afresh.
    this.invalid.delete(key);
    this.report(this.draft.valid && this.invalid.size === 0);
  }
  get unknownKeys() {
    const known = new Set([
      "id",
      "kind",
      ...this.hiddenFields,
      ...(this.step.kind === "Workflow"
        ? ["apiVersion", "metadata", "spec"]
        : []),
      ...this.fields.map((f) => String(f.path[0])),
    ]);
    if (this.step.kind === "switch") known.add("cases");
    if (this.step.kind === "parallel") known.add("branches");
    return Object.keys(this.step).filter((k) => !known.has(k));
  }
  text(field: Field) {
    return (
      this.buffers.get(this.key(field)) ??
      String(this.draft.get(field.path) ?? "")
    );
  }
  error(field: Field) {
    return (
      this.draft.errors.get(this.key(field)) ||
      (this.invalid.has(this.key(field))
        ? "Fix this value before applying changes."
        : "")
    );
  }
  emit() {
    const valid = this.draft.valid && this.invalid.size === 0;
    this.report(valid);
    if (valid && !this.readOnly) this.stepChange.emit(this.draft.snapshot());
  }
  change(field: Field, value: unknown) {
    if (this.readOnly) return;
    this.invalid.delete(this.key(field));
    this.draft.set(field.path, value);
    this.emit();
  }
  validity(field: Field, valid: boolean) {
    if (valid) this.invalid.delete(this.key(field));
    else this.invalid.add(this.key(field));
    this.report(this.draft.valid && this.invalid.size === 0);
  }
  unitOf(field: Field): DurationUnit {
    return (
      this.durationUnits.get(this.key(field)) ??
      splitDuration(this.draft.get(field.path)).unit
    );
  }
  /** The amount shown in the unit field; empty for an unset optional duration. */
  durationAmount(field: Field) {
    const typed = this.buffers.get(this.key(field));
    if (typed !== undefined) return typed;
    const seconds = this.draft.get(field.path);
    if (typeof seconds !== "number") return "";
    const size =
      durationUnits.find((u) => u.unit === this.unitOf(field))?.seconds ?? 1;
    return String(seconds / size);
  }
  durationInput(field: Field, e: Event) {
    this.durationUnits.set(this.key(field), this.unitOf(field));
    this.buffers.set(this.key(field), (e.target as HTMLInputElement).value);
    this.applyDuration(field);
  }
  /** A new unit keeps the typed amount: "2" with hours becomes 7200 seconds. */
  durationUnit(field: Field, e: Event) {
    const amount = this.durationAmount(field);
    this.buffers.set(this.key(field), amount);
    this.durationUnits.set(
      this.key(field),
      (e.target as HTMLSelectElement).value as DurationUnit,
    );
    this.applyDuration(field);
  }
  private applyDuration(field: Field) {
    if (this.readOnly) return;
    const seconds = durationSeconds(
      this.buffers.get(this.key(field)) ?? "",
      this.unitOf(field),
    );
    this.change(
      field,
      seconds === undefined ? (field.optional ? undefined : NaN) : seconds,
    );
  }
  scalar(field: Field, e: Event) {
    if (this.readOnly) return;
    const text = (e.target as HTMLInputElement).value;
    this.buffers.set(this.key(field), text);
    this.change(
      field,
      field.optional && !text
        ? undefined
        : field.kind === "positive"
          ? text.trim()
            ? Number(text)
            : NaN
          : text,
    );
  }
}
