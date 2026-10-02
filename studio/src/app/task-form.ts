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
import { Component, input, output, OnChanges } from "@angular/core";
export interface Schema {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  properties?: Record<string, Schema>;
  required?: string[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
}
@Component({
  selector: "weave-task-form",
  standalone: true,
  template: `<div class="task-fields">
    @for (field of fields(); track field.name) {
      <label [for]="'task-field-' + field.name"
        >{{ field.schema.title || field.name }}
        @if (schema().required?.includes(field.name)) {
          <span class="required-mark"> *</span>
        }
      </label>
      @if (field.schema.enum) {
        <select
          [id]="'task-field-' + field.name"
          [required]="schema().required?.includes(field.name)"
          [value]="enumIndex(field.name, field.schema)"
          (change)="set(field.name, $event, field.schema, true)"
        >
          <option value="">Choose a value</option>
          @for (option of field.schema.enum; track $index) {
            <option [value]="$index">{{ option }}</option>
          }
        </select>
      } @else if (field.schema.type === "boolean") {
        <label class="checkbox-field"
          ><input
            [id]="'task-field-' + field.name"
            type="checkbox"
            [checked]="values[field.name] === true"
            (change)="set(field.name, $event, field.schema)"
          />Yes</label
        >
      } @else if (
        field.schema.type === "object" || field.schema.type === "array"
      ) {
        <textarea
          [id]="'task-field-' + field.name"
          [placeholder]="field.schema.type === 'array' ? '[]' : '{}'"
          [value]="complexValue(field.name)"
          [required]="schema().required?.includes(field.name)"
          (input)="set(field.name, $event, field.schema)"
        ></textarea>
      } @else {
        <input
          [id]="'task-field-' + field.name"
          [type]="
            field.schema.type === 'number' || field.schema.type === 'integer'
              ? 'number'
              : 'text'
          "
          [value]="values[field.name] ?? ''"
          [attr.step]="field.schema.type === 'integer' ? 1 : 'any'"
          [attr.min]="field.schema.minimum"
          [attr.max]="field.schema.maximum"
          [attr.minlength]="field.schema.minLength"
          [attr.maxlength]="field.schema.maxLength"
          [required]="schema().required?.includes(field.name)"
          (input)="set(field.name, $event, field.schema)"
        />
      }
      @if (field.schema.description) {
        <p class="hint">{{ field.schema.description }}</p>
      }
    }
    @if (error) {
      <p class="error" role="alert">{{ error }}</p>
    }
  </div>`,
})
export class TaskForm implements OnChanges {
  schema = input<Schema>({});
  initialData = input<Record<string, unknown>>({});
  ngOnChanges() {
    this.values = structuredClone(this.initialData());
    this.error = "";
  }
  dataChange = output<Record<string, unknown>>();
  values: Record<string, unknown> = {};
  error = "";
  fields() {
    return Object.entries(this.schema().properties ?? {}).map(
      ([name, schema]) => ({ name, schema }),
    );
  }
  enumIndex(name: string, schema: Schema) {
    const index = schema.enum?.findIndex(
      (value) => JSON.stringify(value) === JSON.stringify(this.values[name]),
    );
    return index === undefined || index < 0 ? "" : String(index);
  }
  complexValue(name: string) {
    return this.values[name] === undefined
      ? ""
      : JSON.stringify(this.values[name], null, 2);
  }
  set(name: string, event: Event, schema: Schema, enumerated = false) {
    const el = event.target as HTMLInputElement;
    try {
      const value = enumerated
        ? el.value === ""
          ? undefined
          : schema.enum?.[Number(el.value)]
        : schema.type === "boolean"
          ? el.checked
          : schema.type === "number" || schema.type === "integer"
            ? Number(el.value)
            : schema.type === "object" || schema.type === "array"
              ? JSON.parse(el.value)
              : el.value;
      this.values = { ...this.values, [name]: value };
      el.setCustomValidity("");
      this.error = "";
      this.dataChange.emit(this.values);
    } catch {
      this.error = `${name} must contain valid JSON.`;
      el.setCustomValidity(this.error);
      delete this.values[name];
      this.dataChange.emit({ ...this.values });
    }
  }
}
