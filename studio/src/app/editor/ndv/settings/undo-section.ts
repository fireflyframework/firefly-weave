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
// How it's undone: the way Weave undoes a step inside a transaction, with
// what it does under it and the fields of the way chosen. A step that only
// reads has nothing to undo, and a step outside a transaction can only clear
// what it still has. A choice is one undo step; leaving a way whose
// operations were chosen says which ones went.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  inject,
  input,
} from "@angular/core";
import { Select, type SelectOption } from "../../../forms/ui/select";
import { Icon } from "../../../icon";
import {
  OUTSIDE,
  undoChoiceOf,
  undoRow,
  type UndoRow,
} from "../../transactions/undo";
import { UNDO_OPTIONS } from "../../transactions/undo-fields";
import type { FieldEntry } from "../params/form-model";
import type { FormSession } from "../params/form-session";
import { ParamField } from "../params/param-field";

let sequence = 0;
const OPTIONS: SelectOption[] = UNDO_OPTIONS.map(
  ({ value, label, description }) => ({ value, label, description }),
);

@Component({
  selector: "weave-undo-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, ParamField, Select],
  host: { "[attr.data-param]": "'undo'" },
  template: `@let row = this.row();
    @let line = this.line();
    <div class="param settings-undo" [class.is-readonly]="!!reason()">
      <div class="param-label-row">
        @if (row?.kind === "choices") {
          <label class="param-label" [for]="id">How it's undone</label>
        } @else {
          <span class="param-label" [id]="id + '-label'">How it's undone</span>
        }
      </div>
      @if (row?.kind === "choices") {
        <div class="param-control">
          <weave-select
            class="param-select"
            icon="chevronDown"
            label="How it's undone"
            sheetTitle="How it's undone"
            placeholder="Choose how it's undone"
            [hideLabel]="true"
            [controlId]="id"
            [options]="options"
            [value]="row.value ?? ''"
            [disabled]="!!reason()"
            [invalid]="line?.kind === 'error'"
            [describedBy]="describedBy(row)"
            (choose)="choose($event)"
          />
        </div>
        @if (description(row); as text) {
          <p
            class="param-line settings-undo-description"
            [id]="id + '-description'"
          >
            {{ text }}
          </p>
        }
      } @else if (row?.kind === "read-only") {
        <p class="settings-undo-fact" [attr.aria-labelledby]="id + '-label'">
          <weave-icon name="info" [size]="16" />{{ row.reason }}
        </p>
      } @else if (row?.kind === "outside") {
        <p class="param-line" data-kind="error" [id]="id + '-outside'">
          <weave-icon name="warning" [size]="16" />{{ outside }}
        </p>
        @if (chosen(); as way) {
          <p class="settings-undo-fact">{{ way }}</p>
        }
        <div class="param-actions">
          <button
            type="button"
            class="param-action"
            [disabled]="!!reason()"
            [attr.aria-describedby]="id + '-outside'"
            (click)="session().clearUndo()"
          >
            Clear How it's undone
          </button>
        </div>
      }
      @if (line && row?.kind !== "outside") {
        <p
          class="param-line"
          [id]="id + '-line'"
          [attr.data-kind]="line.kind"
          [attr.role]="line.kind === 'error' ? 'alert' : null"
        >
          @if (line.kind !== "reason") {
            <weave-icon name="warning" [size]="16" />
          }
          {{ line.text }}
        </p>
      }
      @if (fields().length) {
        <div class="settings-undo-fields">
          @for (entry of fields(); track fieldKey(entry)) {
            <weave-param-field
              [session]="session()"
              [spec]="entry.spec"
              [entry]="entry"
            />
          }
        </div>
      }
    </div>`,
})
export class UndoSection {
  session = input.required<FormSession>();
  entry = input.required<FieldEntry>();
  /** The fields of the way chosen, in order. */
  fields = input<readonly FieldEntry[]>([]);
  readonly id = `sd-undo-${++sequence}`;
  readonly options = OPTIONS;
  readonly outside = OUTSIDE;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  row(): UndoRow {
    const { controller, target } = this.session();
    const step = controller.step(target);
    return step ? undoRow(step, controller.undoContext(target)) : null;
  }
  /** Why the choice can't change now (a run is shown, a simulation runs). */
  reason(): string | null {
    return this.row()?.kind === "read-only"
      ? null
      : this.session().readOnly(this.entry());
  }
  /** What the chosen way does, said right under it. */
  description(row: Extract<UndoRow, { kind: "choices" }>): string | null {
    return (
      UNDO_OPTIONS.find(({ value }) => value === row.value)?.description ?? null
    );
  }
  /** The way a step outside a transaction still has, by its name. */
  chosen(): string | null {
    const { controller, target } = this.session();
    const step = controller.step(target);
    const choice = step && undoChoiceOf(step);
    return UNDO_OPTIONS.find(({ value }) => value === choice)?.label ?? null;
  }
  /** Why it can't change, or what is wrong with it. */
  line(): { kind: string; text: string } | null {
    const reason = this.reason();
    if (reason) return { kind: "reason", text: reason };
    const line = this.session().line(this.entry().spec, this.entry());
    return line.kind === "error" || line.kind === "warning" ? line : null;
  }
  describedBy(row: Extract<UndoRow, { kind: "choices" }>): string {
    return [
      this.description(row) ? `${this.id}-description` : "",
      this.line() ? `${this.id}-line` : "",
    ]
      .filter(Boolean)
      .join(" ");
  }
  /** Keeps a field's component while its descriptor stays the same. */
  fieldKey(entry: FieldEntry): object {
    return this.session().fieldKey(entry);
  }
  choose(value: string) {
    const option = UNDO_OPTIONS.find((item) => item.value === value);
    if (!option || this.reason()) return;
    if (!this.session().chooseUndo(option.value)) return;
    // The way's first field is next; on touch its keyboard would cover it.
    if (matchMedia("(pointer: coarse)").matches) return;
    afterNextRender(
      () =>
        this.element.nativeElement
          .querySelector<HTMLElement>(
            ".settings-undo-fields .param-control :is(input, button):not([disabled])",
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
}
