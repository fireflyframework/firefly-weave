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
  forwardRef,
} from "@angular/core";
import { Step, Workflow } from "./model";
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
  kind: "text" | "name" | "positive" | "expression" | "value" | "decisions";
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
      { label: "Input schema", path: ["spec", "inputSchema"], kind: "value" },
      { label: "Output schema", path: ["spec", "outputSchema"], kind: "value" },
      {
        label: "Connection slots",
        path: ["spec", "connections"],
        kind: "value",
        optional: true,
      },
      {
        label: "Workflow timeout (seconds)",
        path: ["spec", "timeoutSeconds"],
        kind: "positive",
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
    wait: [f("Duration (seconds)", "durationSeconds", "positive")],
    signal: [
      f("Signal name", "name", "name"),
      f("Timeout (seconds)", "timeoutSeconds", "positive"),
      f("Payload schema", "payloadSchema", "value"),
    ],
    fail: [f("Error code", "code", "name"), f("Message", "message", "text")],
    humanTask: [
      f("Assignment binding", "assignment", "name"),
      f("Title", "title", "expression"),
      f("Context", "context", "expression"),
      f("Decisions", "decisions", "decisions"),
      f("Due (seconds)", "dueSeconds", "positive", true),
      f("Expiry (seconds)", "expirySeconds", "positive", true),
      f("Form schema", "formSchema", "value"),
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
      label: "Default output",
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
      if (
        field.kind === "positive" &&
        !(typeof v === "number" && Number.isSafeInteger(v) && v > 0)
      )
        error = "Enter a positive whole number.";
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
        [value]="type"
        (change)="changeType($event)"
        aria-label="Value type"
      >
        @for (t of types; track t) {
          <option [value]="t">{{ t }}</option>
        }
      </select>
      @switch (type) {
        @case ("object") {
          <div class="nested-properties">
            @for (entry of entries; track entry.key) {
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
          @for (entry of entries; track entry.key) {
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
            [value]="value ? 'true' : 'false'"
            (change)="scalar($event)"
            aria-label="Boolean value"
          >
            <option value="true">True</option>
            <option value="false">False</option>
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
    this.buffer = String(this.value ?? "");
    this.advanced = JSON.stringify(this.value, null, 2);
    this.error = "";
    this.invalid.clear();
    this.validityChange.emit(true);
  }
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
    this.emit(defaults[type]);
  }
  emit(value: unknown) {
    if (this.readOnly) return;
    this.error = "";
    this.value = value;
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

@Component({
  selector: "weave-expression-editor",
  standalone: true,
  imports: [PropertyValue, forwardRef(() => ExpressionEditor)],
  styleUrl: "./property-grid.css",
  template: `<div class="expression-editor">
    <select
      [disabled]="readOnly"
      [value]="mode"
      (change)="changeMode($event)"
      [attr.aria-label]="label + ' expression mode'"
    >
      <option value="literal">Literal value</option>
      <option value="ref">Reference</option>
      <option value="object">Mapped object</option>
      <option value="array">Mapped array</option>
      <option value="op">Operation</option>
    </select>
    @if (mode === "literal") {
      <weave-property-value
        [value]="body"
        [readOnly]="readOnly"
        (valueChange)="replaceBody($event)"
        (validityChange)="validityChange.emit($event)"
      />
    } @else if (mode === "ref") {
      <input
        [disabled]="readOnly"
        [value]="body"
        (input)="replaceBody(text($event))"
        placeholder="/input/customer"
        [attr.aria-label]="label + ' reference'"
      />
    } @else {
      @if (mode === "op") {
        <select
          [disabled]="readOnly"
          [value]="operator"
          (change)="changeOperator($event)"
          [attr.aria-label]="label + ' operator'"
        >
          @for (name of operators; track name) {
            <option [value]="name">{{ name }}</option>
          }
        </select>
      }
      @for (entry of entries; track entry.key) {
        <div class="expression-argument">
          @if (mode === "object") {
            <label
              >Key<input
                [disabled]="readOnly"
                [value]="entry.key"
                (change)="rename(entry.key, text($event))"
            /></label>
          } @else {
            <strong>Argument {{ Number(entry.key) + 1 }}</strong>
          }
          <weave-expression-editor
            [value]="entry.value"
            [readOnly]="readOnly"
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
      <button type="button" [disabled]="readOnly" (click)="add()">
        Add {{ mode === "object" ? "field" : "argument" }}
      </button>
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
  @Output() valueChange = new EventEmitter<unknown>();
  @Output() validityChange = new EventEmitter<boolean>();
  Number = Number;
  operators = supportedOperators;
  current: RecordValue = { literal: null };
  error = "";
  invalid = new Set<string>();
  ngOnChanges() {
    this.current = object(this.value)
      ? structuredClone(this.value)
      : { literal: null };
    this.invalid.clear();
    this.error = "";
  }
  get mode() {
    return Object.keys(this.current)[0] ?? "literal";
  }
  get body() {
    return this.current[this.mode];
  }
  get operator() {
    return object(this.body) ? String(this.body["name"]) : "eq";
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
    const valid = !this.invalid.size && expressionValid(this.current);
    this.error = valid
      ? ""
      : "Fix references and operation arguments before applying.";
    this.validityChange.emit(valid);
    if (valid && !this.readOnly)
      this.valueChange.emit(structuredClone(this.current));
  }
  replaceBody(value: unknown) {
    if (this.readOnly) return;
    this.current = { [this.mode]: value };
    this.emit();
  }
  changeMode(event: Event) {
    if (this.readOnly) return;
    const mode = this.text(event);
    this.invalid.clear();
    this.current = {
      [mode]:
        mode === "ref"
          ? "/input"
          : mode === "op"
            ? { name: "eq", args: [{ literal: null }, { literal: null }] }
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
    const semverPattern = String.raw`(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?`;
    const semver = new RegExp("^" + semverPattern + "$");
    const versionedReference = new RegExp(
      "^[A-Za-z0-9][A-Za-z0-9_.-]*@" + semverPattern + "$",
    );
    const name = this.text(event);
    let args = this.entries.map((e) => e.value);
    if (["not", "exists"].includes(name))
      args = [
        name === "exists" ? { ref: "/input" } : (args[0] ?? { literal: true }),
      ];
    else if (!["and", "or", "coalesce"].includes(name))
      args = [args[0] ?? { literal: null }, args[1] ?? { literal: null }];
    this.current = { op: { name, args } };
    this.emit();
  }
  childValidity(key: string, valid: boolean) {
    valid ? this.invalid.delete(key) : this.invalid.add(key);
    this.validityChange.emit(
      !this.invalid.size && expressionValid(this.current),
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
    if (this.mode === "object" && object(next)) delete next[key];
    else
      (this.mode === "op" && object(next)
        ? (next["args"] as unknown[])
        : (next as unknown[])
      ).splice(Number(key), 1);
    this.replaceBody(next);
  }
  rename(key: string, name: string) {
    if (name === key) return;
    if (this.readOnly || !object(this.body)) return;
    if (!name || (name !== key && Object.hasOwn(this.body, name))) {
      this.error = "Use a unique nonempty key.";
      this.validityChange.emit(false);
      return;
    }
    const next = Object.fromEntries(
      Object.entries(this.body).map(([entry, value]) => [
        entry === key ? name : entry,
        value,
      ]),
    );
    this.replaceBody(next);
  }
}

@Component({
  selector: "weave-step-property-grid",
  standalone: true,
  imports: [PropertyValue, ExpressionEditor],
  styleUrl: "./property-grid.css",
  template: `@if (draft) {
    <table class="property-grid">
      <thead>
        <tr>
          <th>Property</th>
          <th>Value</th>
        </tr>
      </thead>
      <tbody>
        @if (step.kind !== "Workflow") {
          <tr>
            <th scope="row">Step ID</th>
            <td>{{ step["id"] }}</td>
          </tr>
        }
        <tr>
          <th scope="row">Kind</th>
          <td>{{ step.kind }}</td>
        </tr>
        @for (field of fields; track key(field)) {
          <tr>
            <th scope="row">{{ field.label }}</th>
            <td>
              @if (field.kind === "expression") {
                <weave-expression-editor
                  [value]="draft.get(field.path)"
                  [readOnly]="readOnly"
                  [label]="field.label"
                  (valueChange)="change(field, $event)"
                  (validityChange)="validity(field, $event)"
                />
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
                />
              }
              @if (error(field)) {
                <small class="property-error" role="alert">{{
                  error(field)
                }}</small>
              }
            </td>
          </tr>
        }
      </tbody>
    </table>
    @if (step.kind === "switch" || step.kind === "parallel") {
      <p class="property-hint">
        Nested steps are edited on the canvas. Editing a branch output preserves
        its steps.
      </p>
    }
    @if (step.kind === "humanTask") {
      <p class="property-hint">
        Form schema: use type “object”, add fields under properties, and list
        required field names in a required array. Field schemas support string,
        number, integer, boolean, title and enum.
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
  @Output() stepChange = new EventEmitter<EditableDocument>();
  @Output() validityChange = new EventEmitter<boolean>();
  draft!: PropertyDraft;
  fields: Field[] = [];
  buffers = new Map<string, string>();
  invalid = new Set<string>();
  ngOnChanges() {
    this.draft = new PropertyDraft(this.step);
    this.fields = propertyFields(this.step).filter(
      (field) => !this.hiddenFields.includes(String(field.path[0])),
    );
    this.buffers.clear();
    this.invalid.clear();
    this.validityChange.emit(this.draft.valid);
  }
  key(field: Field) {
    return field.path.join("/");
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
    this.validityChange.emit(valid);
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
    this.validityChange.emit(this.draft.valid && this.invalid.size === 0);
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
