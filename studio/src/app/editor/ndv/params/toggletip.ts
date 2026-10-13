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
// The "?" next to a label: a toggletip that opens on click, Enter or Space
// and closes on Escape or when focus leaves it. The text is also the
// field's accessible description.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from "@angular/core";
import { Icon } from "../../../icon";

@Component({
  selector: "weave-toggletip",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  host: { class: "toggletip", "(focusout)": "open.set(false)" },
  template: `<button
      type="button"
      class="icon-button toggletip-button"
      [attr.aria-label]="label() + ': help'"
      [attr.aria-expanded]="open()"
      (click)="open.set(!open())"
      (keydown.escape)="escape($event)"
    >
      <weave-icon name="help" [size]="16" />
    </button>
    <span
      class="toggletip-text"
      role="status"
      [id]="textId()"
      [hidden]="!open()"
      >{{ text() }}</span
    >`,
})
export class Toggletip {
  label = input.required<string>();
  text = input.required<string>();
  textId = input.required<string>();
  readonly open = signal(false);
  escape(event: Event) {
    if (!this.open()) return;
    event.preventDefault();
    event.stopPropagation();
    this.open.set(false);
  }
}
