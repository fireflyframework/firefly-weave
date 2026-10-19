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
// Edit formula: the input's fields (with search), the formula, and its
// Result, side by side. Edits apply as they do in the field; Done closes.
// Dropping a field from the Input column puts its path at the drop point.
import {
  ChangeDetectionStrategy,
  Component,
  afterNextRender,
  input,
  output,
  viewChild,
} from "@angular/core";
import { Modal } from "../../../dialog";
import { InputPane } from "../panes/input-pane";
import type { ParamSpec } from "../registry";
import { FormulaField } from "./formula-field";
import type { FormSession } from "./form-session";

let sequence = 0;

@Component({
  selector: "weave-edit-formula-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Modal, InputPane, FormulaField],
  template: `<weave-modal
    [heading]="'Edit formula: ' + spec().label"
    [wide]="true"
    closeLabel="Done"
    (dismiss)="done()"
  >
    <div class="edit-formula">
      <weave-input-pane
        class="edit-formula-input"
        [session]="session()"
        searchLabel="Search fields of earlier steps"
        [openSearch]="true"
        [compact]="true"
      />
      <div class="edit-formula-main">
        <span class="sr-only" [id]="controlId + '-label'">{{
          spec().label
        }}</span>
        <weave-formula-field
          [session]="session()"
          [spec]="spec()"
          [controlId]="controlId"
          [inDialog]="true"
          [escapeRestores]="false"
        />
      </div>
    </div>
    <div class="dialog-actions">
      <button type="button" class="primary" (click)="done()">Done</button>
    </div>
  </weave-modal>`,
})
export class EditFormulaDialog {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  readonly closed = output<void>();
  readonly controlId = `edit-formula-${++sequence}`;
  private readonly field = viewChild(FormulaField);
  constructor() {
    afterNextRender(() => this.field()?.focusEditor("end"));
  }
  /** Done (or Escape): text still waiting in the formula applies, then the dialog closes. */
  done() {
    this.field()?.leave();
    this.closed.emit();
  }
}
