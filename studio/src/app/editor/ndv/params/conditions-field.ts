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
// A decision path's conditions as rows (data, test, value). Unfinished rows
// stay here until they are complete; data dropped or mapped into the field
// lands in a row. Conditions the rows can't show, or that the person chose
// to edit as a formula, show in the formula editor.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  input,
  viewChild,
} from "@angular/core";
import { ConditionEditor } from "../../../designer/condition-editor";
import { Icon } from "../../../icon";
import {
  missingConditionText,
  parseCondition,
} from "../../../designer/conditions";
import type { Expression, ParamSpec } from "../registry";
import type { DragRef } from "./drop";
import type { FormSession } from "./form-session";
import { FormulaField } from "./formula-field";
import { ABSENT } from "./value-io";

/** Said when data is dropped while the conditions show as a formula without rows. */
const NO_ROWS =
  "Type the field's path in the formula, or drag it to a place in the text.";

@Component({
  selector: "weave-conditions-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ConditionEditor, Icon, FormulaField],
  template: `@if (rowsCanShow() && !session().formulaView(spec())) {
      <weave-condition-editor
        [value]="when()"
        [label]="spec().label"
        [labelId]="labelId()"
        [references]="references()"
        [readOnly]="!!readOnly()"
        [compact]="true"
        [errorId]="errorId()"
        [actions]="actions()"
        (valueChange)="changed($event)"
      />
    } @else {
      <weave-formula-field
        [session]="session()"
        [spec]="spec()"
        [controlId]="controlId()"
        [describedBy]="errorId()"
        [readOnly]="readOnly()"
        [expandable]="false"
      />
      @if (actions().length) {
        <div class="param-group-add">
          @for (action of actions(); track action.label) {
            <button
              type="button"
              class="text-link param-add"
              (click)="action.run()"
            >
              <weave-icon name="plus" [size]="16" />{{ action.label }}
            </button>
          }
        </div>
      }
    }`,
})
export class ConditionsField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  labelId = input.required<string>();
  readOnly = input<string | null>(null);
  /** The id of the field's message line, which describes unfinished rows. */
  errorId = input("");
  /** The path's own add actions, on the Add condition line. */
  actions = input<readonly { label: string; run: () => void }[]>([]);
  /** The formula editor's id: the label's id without "-label". */
  readonly controlId = () => this.labelId().replace(/-label$/, "");
  private readonly editor = viewChild(ConditionEditor);
  private readonly formula = viewChild(FormulaField);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  constructor() {
    // Map to… reaches these rows through the session while they are on screen.
    effect((cleanup) => {
      const session = this.session();
      cleanup(
        session.attachConditionRows(this.spec(), (entry) =>
          this.place(entry.ref, null),
        ),
      );
    });
  }

  when(): unknown {
    const value = this.session().read(this.spec());
    return value.mode === "mapped"
      ? value.expression
      : value.mode === "fixed"
        ? value.value
        : undefined;
  }
  rowsCanShow(): boolean {
    return parseCondition(this.when()) !== null;
  }
  references() {
    return this.session().scope(this.spec());
  }
  changed(value: unknown) {
    this.session().write(
      this.spec(),
      value === undefined
        ? ABSENT
        : { mode: "mapped", expression: value as Expression },
    );
  }
  /** Rows typed but not finished, which a write from elsewhere would replace. */
  hasDraft(): boolean {
    return this.editor()?.hasDraft() ?? false;
  }
  /** "Finish this condition, or remove it." once the rows were left unfinished. */
  draftProblem(): string {
    const editor = this.editor();
    const problem = editor?.touched ? editor.problem : "";
    return problem && problem !== missingConditionText ? problem : "";
  }
  /**
   * A drop on the rows: on a value it compares with the dropped data;
   * otherwise it becomes the data of the row under the pointer, else of the
   * first row without data, else of a new row.
   */
  drop(drag: DragRef, x: number | null, y: number | null): boolean {
    const session = this.session();
    // Shown as a formula: the path goes in at the drop point.
    const formula = this.formula();
    if (formula && formula.view() === "editor" && x !== null && y !== null) {
      formula.dropAt(drag.ref, x, y);
      return true;
    }
    const decision = session.conditionDrop(this.spec(), drag);
    if ("reason" in decision) {
      session.announce(decision.reason);
      return false;
    }
    const row = y === null ? null : this.rowAt(y);
    const value = row?.element
      .querySelector(".condition-value")
      ?.getBoundingClientRect();
    const onValue =
      !!value &&
      x !== null &&
      y !== null &&
      x >= value.left - 8 &&
      x <= value.right + 8 &&
      y >= value.top - 8 &&
      y <= value.bottom + 8;
    return this.place(
      decision.entry.ref,
      row?.index ?? null,
      onValue ? "value" : "data",
    );
  }
  /** The row whose box is nearest the pointer, within 8 px. */
  private rowAt(y: number): { index: number; element: HTMLElement } | null {
    let best: {
      index: number;
      element: HTMLElement;
      distance: number;
    } | null = null;
    for (const row of this.element.nativeElement.querySelectorAll<HTMLElement>(
      ".condition-row",
    )) {
      const box = row.getBoundingClientRect();
      const distance = y < box.top ? box.top - y : Math.max(0, y - box.bottom);
      if (distance <= 8 && (!best || distance < best.distance))
        best = { index: Number(row.dataset["row"]), element: row, distance };
    }
    return best;
  }
  private place(
    ref: string,
    index: number | null,
    into: "data" | "value" = "data",
  ): boolean {
    const editor = this.editor();
    if (editor?.dropRef(ref, index, into)) return true;
    this.session().announce(
      editor ? "This field cannot read that data." : NO_ROWS,
    );
    return false;
  }
}
