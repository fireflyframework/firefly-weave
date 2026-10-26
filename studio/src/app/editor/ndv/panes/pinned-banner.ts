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
// The banner over pinned output: what a pin does, how to remove it, and
// (when the step, what it calls or what it reads changed) that the pin may
// be out of date, with Execute step and Keep pinned. Announced once, not on
// every render.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { StaleReason } from "../registry";

@Component({
  selector: "weave-pinned-banner",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<div
    class="pinned-banner"
    [attr.data-tone]="stale() ? 'warning' : 'info'"
  >
    <weave-icon
      [name]="stale() ? 'stale' : 'pin'"
      [size]="16"
      aria-hidden="true"
    />
    <p>
      <!-- Only the sentence is announced; the buttons beside it aren't. -->
      <span role="status">{{
        stale()
          ? "Pinned output may be out of date: " + reasonText() + "."
          : "This data is pinned for test runs. Production runs ignore it."
      }}</span
      >&ngsp;
      @if (stale()) {
        @if (canExecute()) {
          <button type="button" class="text-link" (click)="execute.emit()">
            Execute step
          </button>
        }
        <button type="button" class="text-link" (click)="keep.emit()">
          Keep pinned
        </button>
      }
      <button type="button" class="text-link" (click)="unpin.emit()">
        Unpin
      </button>
    </p>
    @if (localOnly()) {
      <p class="pinned-banner-note">
        Kept on this computer until the action is published.
      </p>
    }
  </div>`,
})
export class PinnedBanner {
  stale = input(false);
  /** Why it may be out of date; "upstream" when not known. */
  reason = input<StaleReason | "elsewhere" | null>(null);
  reasonText(): string {
    switch (this.reason()) {
      case "settings":
        return "this step's settings changed";
      case "contract":
        return "what this step calls changed";
      case "elsewhere":
        return "it was pinned in another workflow";
      default:
        return "earlier steps changed";
    }
  }
  localOnly = input(false);
  /** Execute step is offered beside Keep pinned when the pin may be out of date. */
  canExecute = input(false);
  readonly execute = output<void>();
  readonly unpin = output<void>();
  readonly keep = output<void>();
}
