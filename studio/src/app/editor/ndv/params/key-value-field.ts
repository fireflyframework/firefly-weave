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
// Key and value rows (transform fields, query parameters, headers, body
// fields, details to show): a name, a Fixed or Mapped value, a grip and a
// trash button; "Add field". With no rows, one blank row waits for a name.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  forwardRef,
  inject,
  input,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { ParamSpec } from "../registry";
import type { FieldEntry } from "./form-model";
import type { FormSession } from "./form-session";
import { ParamField } from "./param-field";
import { canMapFields } from "./drag-map";

@Component({
  selector: "weave-key-value-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, forwardRef(() => ParamField)],
  template: `@let rows = session().keyed(spec());
    @let locked = !!readOnly();
    <div
      class="param-kv"
      role="group"
      [attr.aria-labelledby]="labelId()"
      data-drop="rows"
    >
      @for (
        row of rows;
        track session().rowKey(spec(), row[0]);
        let i = $index
      ) {
        <div
          class="param-kv-row"
          role="group"
          [attr.aria-label]="'Row ' + (i + 1) + ' of ' + rows.length"
          (keydown)="rowKey($event, i, rows.length)"
        >
          <weave-icon
            class="param-grip"
            name="drag"
            [size]="16"
            aria-hidden="true"
          />
          <input
            class="param-input param-kv-name"
            [attr.aria-label]="'Name of row ' + (i + 1)"
            [value]="row[0]"
            [readOnly]="locked"
            (change)="rename(row[0], $event)"
          />
          <weave-param-field
            class="param-row-value"
            [session]="session()"
            [spec]="valueSpec(row[0])"
            [entry]="entry(row[0])"
            [row]="true"
          />
          <button
            type="button"
            class="icon-button"
            [attr.aria-label]="'Remove ' + row[0]"
            [disabled]="locked"
            (click)="remove(row[0])"
          >
            <weave-icon name="trash" [size]="16" />
          </button>
        </div>
      } @empty {
        @if (!locked) {
          <div class="param-kv-row is-blank">
            <span class="param-grip" aria-hidden="true"></span>
            <input
              class="param-input param-kv-name"
              aria-label="Name of row 1"
              placeholder="Name"
              (change)="addNamed($event)"
            />
            <span class="param-kv-hint">Name a field to add it</span>
          </div>
        }
      }
      @if (!locked) {
        <button type="button" class="text-link param-add" (click)="add()">
          <weave-icon name="plus" [size]="16" />{{
            spec().addLabel ?? "Add field"
          }}
        </button>
        @if (!session().mappingReason(spec())) {
          <button
            type="button"
            class="text-link param-add"
            (click)="addAllFields()"
          >
            <weave-icon name="plus" [size]="16" />Add all fields
          </button>
        }
      }
    </div>`,
})
export class KeyValueField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  labelId = input.required<string>();
  readOnly = input<string | null>(null);
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  valueSpec(key: string): ParamSpec {
    return this.session().keyedSpec(this.spec(), key);
  }
  entry(key: string): FieldEntry {
    const entry = this.session().childEntry(this.valueSpec(key));
    return { ...entry, readOnly: this.readOnly() ?? entry.readOnly };
  }
  private free(base: string): string {
    const taken = new Set(
      this.session()
        .keyed(this.spec())
        .map(([key]) => key),
    );
    let name = base;
    for (let n = 2; taken.has(name); n++) name = `${base}-${n}`;
    return name;
  }
  add(name = this.free("field")) {
    this.session().setKeyed(this.spec(), (entries) => [
      ...entries,
      [name, { mode: "fixed", value: "" }],
    ]);
    this.focusRow(this.session().keyed(this.spec()).length - 1);
  }
  addAllFields() {
    if (canMapFields(this.element.nativeElement, this.session()))
      this.session().addAllFields(this.spec());
  }
  private focusRow(index: number) {
    const session = this.session();
    session.focusLater(
      this.element.nativeElement,
      this.spec(),
      () =>
        this.element.nativeElement.querySelectorAll<HTMLElement>(
          ".param-kv-name",
        )[index],
      () => !this.lifetime.destroyed && this.session() === session,
    );
  }

  addNamed(event: Event) {
    const name = (event.target as HTMLInputElement).value.trim();
    if (name) this.add(this.free(name));
  }
  rename(from: string, event: Event) {
    const next = (event.target as HTMLInputElement).value.trim();
    if (!next || next === from) return;
    if (
      this.session()
        .keyed(this.spec())
        .some(([key]) => key === next)
    ) {
      (event.target as HTMLInputElement).value = from;
      return this.session().announce(
        `${next} is already used. Choose another name.`,
      );
    }
    this.session().listRename(this.spec(), from, next);
  }
  remove(key: string) {
    this.session().listRemove(this.spec(), key);
    this.session().announce(`Removed ${key}.`);
  }
  rowKey(event: KeyboardEvent, index: number, count: number) {
    if (
      !event.altKey ||
      (event.key !== "ArrowUp" && event.key !== "ArrowDown") ||
      this.readOnly()
    )
      return;
    const to = index + (event.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= count) return;
    event.preventDefault();
    event.stopPropagation();
    this.session().listMove(this.spec(), index, to);
    this.session().announce(`Moved to position ${to + 1}.`);
    this.focusRow(to);
  }
}
