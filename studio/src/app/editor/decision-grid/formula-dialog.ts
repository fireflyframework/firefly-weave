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
// Edit formula, for a rule's whole condition or a mapped result: the formula
// editor over the table's input only, its problem in words, Apply.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
  signal,
} from "@angular/core";
import { Modal } from "../../dialog";
import { Icon } from "../../icon";
import { FormulaEditor } from "../formula/codemirror/formula-editor";
import type { PillLook } from "../formula/codemirror/decorations";
import type { Expression } from "../ndv/registry";
import { tableCompletions, tableFormula } from "./formula-cells";

let sequence = 0;

@Component({
  selector: "weave-formula-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Modal, Icon, FormulaEditor],
  template: `
    <weave-modal
      class="dg-formula-dialog"
      [heading]="heading()"
      closeLabel="Cancel"
      (dismiss)="cancel.emit()"
    >
      <p class="dg-formula-hint" [id]="id + '-hint'">
        Rules read only the table's input, such as
        <code>input.amount &gt; 1000</code>.
      </p>
      <span class="sr-only" [id]="id + '-label'">{{ heading() }}</span>
      <weave-formula-editor
        mode="formula"
        [multiline]="true"
        [controlId]="id"
        [labelledBy]="id + '-label'"
        [describedBy]="id + '-hint' + (problem() ? ' ' + id + '-problem' : '')"
        [invalid]="!!problem()"
        [text]="start()"
        [pills]="look"
        [completions]="completions()"
        (textChange)="typed($event)"
      />
      @if (problem(); as text) {
        <p class="field-error" role="alert" [id]="id + '-problem'">
          <weave-icon name="warning" [size]="16" />{{ text }}
        </p>
      }
      <div class="dialog-actions">
        <button type="button" (click)="cancel.emit()">Cancel</button>
        <button
          type="button"
          class="primary"
          [attr.aria-disabled]="problem() ? 'true' : null"
          (click)="submit()"
        >
          Apply
        </button>
      </div>
    </weave-modal>
  `,
})
export class FormulaDialog {
  readonly heading = input.required<string>();
  /** The formula when the dialog opens. */
  readonly start = input.required<string>();
  readonly inputSchema = input.required<unknown>();
  readonly apply = output<Expression>();
  readonly cancel = output<void>();
  readonly id = `dg-formula-${++sequence}`;
  private text: string | null = null;
  readonly problem = signal<string | null>(null);

  completions() {
    return tableCompletions(this.inputSchema());
  }
  readonly look = (pointer: string): PillLook => {
    const key = pointer.split("/").slice(2).join(".");
    return {
      parts: ["Input", key],
      icon: "source",
      title: `input.${key}`,
      name: `reference Input ${key}`,
    };
  };
  typed(text: string) {
    this.text = text;
    const read = tableFormula(text, this.inputSchema());
    this.problem.set(read.ok ? null : read.message);
  }
  submit() {
    const read = tableFormula(this.text ?? this.start(), this.inputSchema());
    if (!read.ok) {
      this.problem.set(read.message);
      return;
    }
    this.apply.emit(read.expression);
  }
}
