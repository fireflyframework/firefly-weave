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
// The floating toolbar for several selected steps: how many, Duplicate
// (one run of steps) and Delete (any selection). A command that can't run
// stays in place, aria-disabled, says why to assistive technology and,
// when pressed, in a toast.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../icon";

@Component({
  selector: "weave-selection-toolbar",
  standalone: true,
  imports: [Icon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let lock = locked();
    @let duplicateReason = lock ?? blocked();
    <div class="selection-toolbar" role="toolbar" aria-label="Selected steps">
      <span class="selection-count" aria-live="polite"
        >{{ count() }} steps</span
      >
      <button
        type="button"
        class="tertiary"
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
        class="tertiary danger"
        [attr.aria-disabled]="lock ? 'true' : null"
        [attr.aria-describedby]="lock ? 'selection-delete-reason' : null"
        (click)="remove.emit()"
      >
        <weave-icon name="trash" [size]="16" />Delete
      </button>
      @if (duplicateReason) {
        <span class="sr-only" id="selection-duplicate-reason">{{
          duplicateReason
        }}</span>
      }
      @if (lock) {
        <span class="sr-only" id="selection-delete-reason">{{ lock }}</span>
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
  remove = output<void>();
}
