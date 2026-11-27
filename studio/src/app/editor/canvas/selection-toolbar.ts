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
// The floating toolbar for several selected steps: how many, Copy and
// Duplicate, Group in a transaction (one run of steps) and Delete (any
// selection). A command that can't run stays in place, aria-disabled, says
// why to assistive technology and, when pressed, in a toast. On a narrow
// canvas the toolbar keeps one row: Group in a transaction moves into a More
// menu first, then Copy and Duplicate with it.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";

@Component({
  selector: "weave-selection-toolbar",
  standalone: true,
  imports: [Icon, RowMenu],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let lock = locked();
    @let duplicateReason = lock ?? blocked();
    @let groupReason = lock ?? groupBlocked();
    <div class="selection-toolbar" role="toolbar" aria-label="Selected steps">
      <span class="selection-count" aria-live="polite"
        >{{ count() }} steps</span
      >
      <button
        type="button"
        class="tertiary selection-wide"
        [attr.aria-disabled]="copyBlocked() ? 'true' : null"
        [attr.aria-describedby]="copyBlocked() ? 'selection-copy-reason' : null"
        (click)="copy.emit()"
      >
        <weave-icon name="copy" [size]="16" />Copy
      </button>
      <button
        type="button"
        class="tertiary selection-wide"
        [attr.aria-disabled]="duplicateReason ? 'true' : null"
        [attr.aria-describedby]="
          duplicateReason ? 'selection-duplicate-reason' : null
        "
        (click)="duplicate.emit()"
      >
        <weave-icon name="copy" [size]="16" />Duplicate
      </button>
      <button
        type="button"
        class="tertiary selection-roomy"
        [attr.aria-disabled]="groupReason ? 'true' : null"
        [attr.aria-describedby]="groupReason ? 'selection-group-reason' : null"
        [attr.title]="groupReason"
        (click)="group.emit()"
      >
        <weave-icon name="transaction" [size]="16" />Group in a transaction
      </button>
      <button
        type="button"
        class="tertiary danger"
        [attr.aria-disabled]="lock ? 'true' : null"
        [attr.aria-describedby]="lock ? 'selection-delete-reason' : null"
        (click)="remove.emit()"
      >
        <weave-icon name="trash" [size]="16" />Delete
      </button>
      <weave-row-menu
        class="selection-medium"
        label="More commands for the selected steps"
        [items]="groupOnly()"
      />
      <weave-row-menu
        class="selection-narrow"
        label="More commands for the selected steps"
        [items]="more()"
      />
      @if (duplicateReason) {
        <span class="sr-only" id="selection-duplicate-reason">{{
          duplicateReason
        }}</span>
      }
      @if (groupReason) {
        <span class="sr-only" id="selection-group-reason">{{
          groupReason
        }}</span>
      }
      @if (lock) {
        <span class="sr-only" id="selection-delete-reason">{{ lock }}</span>
      }
      @if (copyBlocked(); as reason) {
        <span class="sr-only" id="selection-copy-reason">{{ reason }}</span>
      }
    </div>`,
})
export class SelectionToolbar {
  count = input.required<number>();
  /** Why Duplicate can't run on this selection, or null. */
  blocked = input<string | null>(null);
  /** Why nothing can change now (a simulation runs, or the source doesn't parse), or null. */
  locked = input<string | null>(null);
  duplicate = output<void>();
  /** Why Group in a transaction can't run on this selection, or null. */
  groupBlocked = input<string | null>(null);
  group = output<void>();
  /** Why Copy can't run on this selection, or null. */
  copyBlocked = input<string | null>(null);
  copy = output<void>();
  remove = output<void>();
  /** Group where there is no room for its button; when it can't run it says why. */
  readonly groupOnly = computed((): RowMenuItem[] => [
    {
      label: "Group in a transaction",
      detail: (this.locked() ?? this.groupBlocked()) || undefined,
      blocked: !!(this.locked() ?? this.groupBlocked()),
      run: () => this.group.emit(),
    },
  ]);
  /** Copy and Duplicate too where the canvas is narrower still. */
  readonly more = computed((): RowMenuItem[] => [
    {
      label: "Copy",
      detail: this.copyBlocked() ?? undefined,
      run: () => this.copy.emit(),
    },
    {
      label: "Duplicate",
      detail: (this.locked() ?? this.blocked()) || undefined,
      run: () => this.duplicate.emit(),
    },
    ...this.groupOnly(),
  ]);
}
