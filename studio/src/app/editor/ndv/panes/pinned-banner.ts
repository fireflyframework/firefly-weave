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
// be out of date, with Keep pinned. Announced once, not on every render.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../../icon";

@Component({
  selector: "weave-pinned-banner",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<div
    class="pinned-banner"
    role="status"
    [attr.data-tone]="stale() ? 'warning' : 'info'"
  >
    <weave-icon
      [name]="stale() ? 'stale' : 'pin'"
      [size]="16"
      aria-hidden="true"
    />
    <p>
      @if (stale()) {
        Pinned output may be out of date: earlier steps changed.
        <button type="button" class="text-link" (click)="keep.emit()">
          Keep pinned
        </button>
      } @else {
        This data is pinned for test runs. Production runs ignore it.
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
  localOnly = input(false);
  readonly unpin = output<void>();
  readonly keep = output<void>();
}
