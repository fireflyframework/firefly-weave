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
// The decision grid's column dialog: a new column's name and type, a new
// name, or another type for an input column.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Modal } from "../../dialog";
import { Icon } from "../../icon";
import { COLUMN_TYPE_LABELS, type ColumnType } from "./cell-grammar";
import { FIELD_NAME } from "./rules-model";

export interface ColumnRequest {
  mode: "add" | "rename" | "type";
  side: "input" | "result";
  /** The column renamed or retyped. */
  name?: string;
  type?: ColumnType;
  /** Types the column can't take, with the reason. */
  unavailable?: Partial<Record<ColumnType, string>>;
}
export interface ColumnAnswer {
  name: string;
  type: ColumnType;
}

export const COLUMN_TYPE_ICONS: Readonly<Record<ColumnType, string>> = {
  text: "typeText",
  number: "typeNumber",
  integer: "typeNumber",
  boolean: "typeBoolean",
  dateTime: "typeDate",
  textList: "typeList",
};
const TYPES = Object.keys(COLUMN_TYPE_LABELS) as ColumnType[];

/** A step-name-like field name: spaces and dashes become underscores. */
export const normalizeColumnName = (text: string): string =>
  text.trim().replace(/[\s-]+/g, "_");

@Component({
  selector: "weave-column-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Modal, Icon],
  template: `
    <weave-modal
      class="dg-column-dialog"
      [heading]="heading()"
      closeLabel="Cancel"
      (dismiss)="cancel.emit()"
    >
      <form class="dg-column-form" (submit)="submit($event)">
        @if (request().mode !== "type") {
          <label class="dg-column-label" for="dg-column-name">Name</label>
          <input
            id="dg-column-name"
            class="param-input"
            autocomplete="off"
            spellcheck="false"
            data-initial-focus
            [attr.aria-invalid]="problem ? 'true' : null"
            [attr.aria-describedby]="problem ? 'dg-column-problem' : null"
            [value]="name"
            (input)="name = $any($event.target).value; problem = ''"
          />
        }
        @if (request().mode !== "rename") {
          <fieldset class="dg-column-types">
            <legend>Type</legend>
            @for (choice of types; track choice) {
              @let why = request().unavailable?.[choice];
              <label class="dg-column-type" [class.is-disabled]="!!why">
                <input
                  type="radio"
                  name="dg-column-type"
                  [value]="choice"
                  [checked]="type === choice"
                  [disabled]="!!why"
                  [attr.aria-describedby]="why ? 'dg-why-' + choice : null"
                  (change)="type = choice"
                />
                <weave-icon [name]="icons[choice]" [size]="16" />
                <span>{{ labels[choice] }}</span>
                @if (why) {
                  <span class="dg-column-why" [id]="'dg-why-' + choice">{{
                    why
                  }}</span>
                }
              </label>
            }
          </fieldset>
        }
        @if (problem) {
          <p id="dg-column-problem" class="field-error" role="alert">
            <weave-icon name="warning" [size]="16" />{{ problem }}
          </p>
        }
        <div class="dialog-actions">
          <button type="button" (click)="cancel.emit()">Cancel</button>
          <button type="submit" class="primary">{{ action() }}</button>
        </div>
      </form>
    </weave-modal>
  `,
})
export class ColumnDialog {
  readonly request = input.required<ColumnRequest>();
  readonly done = output<ColumnAnswer>();
  readonly cancel = output<void>();
  readonly types = TYPES;
  readonly labels = COLUMN_TYPE_LABELS;
  readonly icons = COLUMN_TYPE_ICONS;
  name = "";
  type: ColumnType = "text";
  problem = "";
  private seeded: ColumnRequest | null = null;

  heading(): string {
    this.seed();
    const request = this.request();
    const side = request.side === "input" ? "input" : "result";
    if (request.mode === "add") return `Add ${side} column`;
    if (request.mode === "rename") return `Rename ${request.name}`;
    return `Change the type of ${request.name}`;
  }
  action(): string {
    const mode = this.request().mode;
    return mode === "add"
      ? "Add column"
      : mode === "rename"
        ? "Rename column"
        : "Change type";
  }
  private seed() {
    const request = this.request();
    if (this.seeded === request) return;
    this.seeded = request;
    this.name = request.name ?? "";
    this.type = request.type ?? "text";
    this.problem = "";
  }
  submit(event: Event) {
    event.preventDefault();
    const name =
      this.request().mode === "type"
        ? (this.request().name ?? "")
        : normalizeColumnName(this.name);
    if (!FIELD_NAME.test(name)) {
      this.problem = name
        ? "Use a field name with letters, numbers or underscores."
        : "Give the column a name.";
      return;
    }
    this.done.emit({ name, type: this.type });
  }
}
