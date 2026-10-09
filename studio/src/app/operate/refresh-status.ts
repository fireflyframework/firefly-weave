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
// "Updated 12 s ago" and Refresh, at the top of every Operate page.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  inject,
  input,
  output,
} from "@angular/core";
import { Icon } from "../icon";
import { updatedText } from "./operate-store";

@Component({
  selector: "weave-refresh-status",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styleUrl: "./operate.css",
  template: `<span class="updated">{{ label() }}</span
    ><button
      type="button"
      class="tertiary"
      [attr.aria-disabled]="busy() ? 'true' : null"
      (click)="!busy() && refresh.emit()"
    >
      <weave-icon name="refresh" [size]="16" />Refresh
    </button>`,
  host: { class: "refresh-status" },
})
export class RefreshStatus implements OnDestroy {
  /** When the page last loaded (epoch milliseconds). */
  updatedAt = input<number | null>(null);
  busy = input(false);
  refresh = output<void>();
  private cdr = inject(ChangeDetectorRef);
  // The label counts up on its own between loads.
  private timer = setInterval(() => this.cdr.markForCheck(), 1000);
  label() {
    return updatedText(this.updatedAt(), Date.now());
  }
  ngOnDestroy() {
    clearInterval(this.timer);
  }
}
