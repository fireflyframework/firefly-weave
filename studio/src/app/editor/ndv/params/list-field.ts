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
// A list: rows (with a grip, Alt+↑/↓ to move, a trash button and a specific
// add label), chips for short identifier lists such as answers, cards for
// groups such as decision paths, and named rows for a parallel's branches.
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
import type { ParamSpec, Path } from "../registry";
import type { FieldEntry } from "./form-model";
import type { FormSession } from "./form-session";
import { normalizeIdentifier } from "./identifiers";
import { ParamField } from "./param-field";
import { FieldsField } from "./fields-field";

interface Row {
  key: number | string;
  identity: string;
  spec: ParamSpec;
  path: Path;
}

@Component({
  selector: "weave-list-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, forwardRef(() => ParamField), forwardRef(() => FieldsField)],
  template: `@let rows = this.rows();
    @let locked = !!readOnly();
    @if (spec().display === "chips") {
      <div class="param-chips" role="group" [attr.aria-labelledby]="labelId()">
        @for (row of rows; track row.identity; let i = $index) {
          <span
            class="param-chip"
            role="group"
            [attr.aria-label]="rowName(i, rows.length)"
            (keydown)="rowKey($event, i)"
          >
            <input
              class="param-chip-text"
              [attr.aria-label]="rowName(i, rows.length)"
              [value]="chipText(i)"
              [readOnly]="locked"
              (change)="renameChip(i, $event)"
            />
            <button
              type="button"
              class="icon-button"
              [attr.aria-label]="'Remove ' + chipText(i)"
              [disabled]="locked || !!session().removeReason(spec(), i)"
              [attr.title]="session().removeReason(spec(), i)"
              (click)="session().listRemove(spec(), i)"
            >
              <weave-icon name="close" [size]="16" />
            </button>
          </span>
        }
        @if (!locked) {
          <input
            class="param-chip-new"
            [attr.aria-label]="spec().addLabel ?? 'Add'"
            [placeholder]="spec().addLabel ?? 'Add'"
            (keydown.enter)="addChip($event)"
          />
        }
      </div>
    } @else {
      <div class="param-list" role="group" [attr.aria-labelledby]="labelId()">
        @for (row of rows; track row.identity; let i = $index) {
          @let info = card() ? session().rowInfo(row.path) : null;
          <div
            class="param-row"
            [class.is-card]="card()"
            role="group"
            [attr.aria-label]="rowName(i, rows.length)"
            (keydown)="rowKey($event, i)"
          >
            @if (card()) {
              <div class="param-card-head">
                <weave-icon
                  class="param-grip"
                  name="drag"
                  [size]="16"
                  aria-hidden="true"
                />
                <strong>{{ spec().item?.label ?? "Item" }} {{ i + 1 }}</strong>
                @if (info) {
                  <span class="param-chip-out" [attr.title]="info.chip"
                    ><weave-icon name="switch" [size]="16" />{{
                      info.chip
                    }}</span
                  >
                  <span class="param-next">→ {{ info.next }}</span>
                }
                <span class="param-spacer"></span>
                <button
                  type="button"
                  class="icon-button"
                  [attr.aria-label]="'Move ' + rowName(i, rows.length) + ' up'"
                  [disabled]="locked || i === 0"
                  (click)="move(i, i - 1)"
                >
                  <weave-icon name="chevron" [size]="16" class="param-up" />
                </button>
                <button
                  type="button"
                  class="icon-button"
                  [attr.aria-label]="
                    'Move ' + rowName(i, rows.length) + ' down'
                  "
                  [disabled]="locked || i === rows.length - 1"
                  (click)="move(i, i + 1)"
                >
                  <weave-icon name="chevronDown" [size]="16" />
                </button>
                <button
                  type="button"
                  class="icon-button"
                  [attr.aria-label]="'Remove ' + rowName(i, rows.length)"
                  [disabled]="
                    locked || !!session().removeReason(spec(), row.key)
                  "
                  [attr.title]="session().removeReason(spec(), row.key)"
                  (click)="session().listRemove(spec(), row.key)"
                >
                  <weave-icon name="trash" [size]="16" />
                </button>
              </div>
              <weave-fields-field
                [session]="session()"
                [spec]="row.spec"
                [readOnly]="readOnly()"
              />
            } @else if (keyedNames()) {
              <input
                class="param-input"
                [attr.aria-label]="'Name of ' + rowName(i, rows.length)"
                [value]="row.key"
                [readOnly]="locked"
                (change)="renameKeyed(row.key, $event)"
              />
              <button
                type="button"
                class="icon-button"
                [attr.aria-label]="'Remove ' + row.key"
                [disabled]="locked || !!session().removeReason(spec(), row.key)"
                [attr.title]="session().removeReason(spec(), row.key)"
                (click)="session().listRemove(spec(), row.key)"
              >
                <weave-icon name="trash" [size]="16" />
              </button>
            } @else {
              <weave-icon
                class="param-grip"
                name="drag"
                [size]="16"
                aria-hidden="true"
              />
              <weave-param-field
                class="param-row-value"
                [session]="session()"
                [spec]="row.spec"
                [entry]="entry(row.spec)"
                [row]="true"
              />
              <button
                type="button"
                class="icon-button"
                [attr.aria-label]="'Remove ' + rowName(i, rows.length)"
                [disabled]="locked || !!session().removeReason(spec(), i)"
                [attr.title]="session().removeReason(spec(), i)"
                (click)="session().listRemove(spec(), i)"
              >
                <weave-icon name="trash" [size]="16" />
              </button>
            }
          </div>
        }
        @if (
          !locked &&
          (spec().maxItems === undefined || rows.length < spec().maxItems!)
        ) {
          <button type="button" class="text-link param-add" (click)="add()">
            <weave-icon name="plus" [size]="16" />{{
              spec().addLabel ?? "Add item"
            }}
          </button>
        }
      </div>
    }`,
})
export class ListField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  labelId = input.required<string>();
  readOnly = input<string | null>(null);
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  card(): boolean {
    return this.spec().item?.type === "fields";
  }
  keyedNames(): boolean {
    return this.session().structure(this.spec()) === "branches";
  }
  rows(): Row[] {
    const spec = this.spec();
    const session = this.session();
    const item = spec.item ?? {
      id: `${spec.id}.item`,
      path: [],
      type: "text" as const,
      label: "Item",
    };
    if (this.keyedNames())
      return session.keyed(spec).map(([key]) => ({
        key,
        identity: session.rowKey(spec, key),
        path: [...spec.path, key],
        spec: session.itemSpec(spec, key, item),
      }));
    const count =
      spec.display === "chips"
        ? this.chips().length
        : session.list(spec).length;
    return Array.from({ length: count }, (_, i) => ({
      key: i,
      identity: session.rowKey(spec, i),
      path: [...spec.path, i],
      spec: session.itemSpec(spec, i, item),
    }));
  }
  rowName(index: number, count: number): string {
    return `${this.spec().item?.label ?? "Item"} ${index + 1} of ${count}`;
  }
  entry(spec: ParamSpec): FieldEntry {
    const entry = this.session().childEntry(spec);
    return { ...entry, readOnly: this.readOnly() ?? entry.readOnly };
  }
  add() {
    this.session().listAdd(this.spec());
    this.session().announce(
      `Added ${(this.spec().item?.label ?? "item").toLowerCase()} ${this.rows().length}.`,
    );
    this.focusRow(this.rows().length - 1);
  }
  private focusRow(index: number) {
    const session = this.session();
    session.focusLater(
      this.element.nativeElement,
      this.spec(),
      () => {
        const row =
          this.element.nativeElement.querySelectorAll<HTMLElement>(
            ".param-row",
          )[index];
        return row
          ? session.firstControl(
              row.querySelector<HTMLElement>(".param-control") ?? row,
            )
          : null;
      },
      () => !this.lifetime.destroyed && this.session() === session,
    );
  }
  move(from: number, to: number) {
    this.session().listMove(this.spec(), from, to);
    this.session().announce(`Moved to position ${to + 1}.`);
    this.focusRow(to);
  }

  rowKey(event: KeyboardEvent, index: number) {
    if (
      !event.altKey ||
      (event.key !== "ArrowUp" && event.key !== "ArrowDown") ||
      this.readOnly()
    )
      return;
    const to = index + (event.key === "ArrowUp" ? -1 : 1);
    if (to < 0 || to >= this.rows().length) return;
    event.preventDefault();
    event.stopPropagation();
    this.move(index, to);
  }
  chips(): string[] {
    const value = this.session().read(this.spec());
    return value.mode === "fixed" && Array.isArray(value.value)
      ? value.value.map(String)
      : ((this.spec().default as string[] | undefined) ?? []);
  }
  chipText(index: number): string {
    return this.chips()[index] ?? "";
  }
  renameChip(index: number, event: Event) {
    const next = normalizeIdentifier((event.target as HTMLInputElement).value);
    if (next) this.session().listRename(this.spec(), index, next);
  }
  addChip(event: Event) {
    const input = event.target as HTMLInputElement;
    const next = normalizeIdentifier(input.value);
    event.preventDefault();
    if (
      !next ||
      this.chips().includes(next) ||
      this.readOnly() ||
      (this.spec().maxItems !== undefined &&
        this.chips().length >= this.spec().maxItems!)
    )
      return;
    this.session().setList(this.spec(), () =>
      [...this.chips(), next].map((value) => ({
        mode: "fixed" as const,
        value,
      })),
    );
    this.session().announce(`Added ${next}.`);
    input.value = "";
  }
  renameKeyed(from: number | string, event: Event) {
    const next = normalizeIdentifier((event.target as HTMLInputElement).value);
    if (next && next !== from)
      this.session().listRename(this.spec(), from, next);
  }
}
