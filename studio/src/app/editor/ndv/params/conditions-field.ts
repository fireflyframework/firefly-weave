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
// lands in a row. Conditions the rows can't show stay a formula, read-only
// here and edited as YAML.
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
import {
  missingConditionText,
  parseCondition,
} from "../../../designer/conditions";
import type { Expression, ParamSpec } from "../registry";
import type { DragRef } from "./drop";
import type { FormSession } from "./form-session";
import { ABSENT } from "./value-io";

const FORMULA =
  "This condition is a formula. Edit it with Edit as YAML in the More menu.";

@Component({
  selector: "weave-conditions-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ConditionEditor],
  template: `@if (rowsCanShow()) {
      <weave-condition-editor
        [value]="when()"
        [label]="spec().label"
        [labelId]="labelId()"
        [references]="references()"
        [readOnly]="!!readOnly()"
        [compact]="true"
        (valueChange)="changed($event)"
      />
    } @else {
      <p class="param-deferred">{{ formula }}</p>
      <pre
        class="param-viewer"
        tabindex="0"
        [attr.aria-labelledby]="labelId()"
        >{{ json() }}</pre
      >
    }`,
})
export class ConditionsField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  labelId = input.required<string>();
  readOnly = input<string | null>(null);
  readonly formula = FORMULA;
  private readonly editor = viewChild(ConditionEditor);
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
  json(): string {
    return JSON.stringify(this.when(), null, 2);
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
  /** A drop on the rows: the row under the pointer, else the first without data, else a new one. */
  drop(drag: DragRef, y: number | null): boolean {
    const session = this.session();
    const decision = session.conditionDrop(this.spec(), drag);
    if ("reason" in decision) {
      session.announce(decision.reason);
      return false;
    }
    return this.place(decision.entry.ref, y === null ? null : this.rowAt(y));
  }
  /** The row whose box is nearest the pointer, within 8 px. */
  private rowAt(y: number): number | null {
    let best: { index: number; distance: number } | null = null;
    for (const row of this.element.nativeElement.querySelectorAll<HTMLElement>(
      ".condition-row",
    )) {
      const box = row.getBoundingClientRect();
      const distance = y < box.top ? box.top - y : Math.max(0, y - box.bottom);
      if (distance <= 8 && (!best || distance < best.distance))
        best = { index: Number(row.dataset["row"]), distance };
    }
    return best?.index ?? null;
  }
  private place(ref: string, index: number | null): boolean {
    const editor = this.editor();
    if (editor?.dropRef(ref, index)) return true;
    this.session().announce(
      editor ? "This field cannot read that data." : FORMULA,
    );
    return false;
  }
}
