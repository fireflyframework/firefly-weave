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
// The banner over a step's Output when a failure is simulated for it: that
// executing it in Studio fails it with this code, that real runs ignore it,
// and Remove. Over pinned output it says the pin is kept for later.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { ErrorEntry } from "../../state/test-data";
import { OUTCOME_WORDS as OUTCOMES } from "./error-banner";

@Component({
  selector: "weave-failure-banner",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<div class="pinned-banner failure-banner" data-tone="warning">
    <weave-icon name="warning" [size]="16" aria-hidden="true" />
    <p>
      <span role="status">{{ sentence() }}</span
      >&ngsp;
      <button type="button" class="text-link" (click)="remove.emit()">
        Remove
      </button>
    </p>
    <p class="failure-banner-code">
      <span class="failure-code">{{ codeLine() }}</span
      >&ngsp;
      <button type="button" class="text-link" (click)="edit.emit()">
        Edit
      </button>
    </p>
    @if (sessionOnly()) {
      <p class="pinned-banner-note">
        This platform can't save simulated failures yet. They last until you
        close this workflow.
      </p>
    }
    @if (overPin()) {
      <p class="pinned-banner-note">
        Its pinned output is kept for when you remove the failure.
      </p>
    }
  </div>`,
})
export class FailureBanner {
  failure = input.required<ErrorEntry>();
  /** "step" outside loops; inside one, the picked item's own or every item's. */
  scope = input<"step" | "item" | "every">("step");
  /** The platform can't save simulated failures: this one lasts for the session. */
  sessionOnly = input(false);
  /** Pinned output is there too, kept while the failure is. */
  overPin = input(false);
  readonly remove = output<void>();
  readonly edit = output<void>();
  sentence(): string {
    const who =
      this.scope() === "item"
        ? "This item fails"
        : this.scope() === "every"
          ? "Every item fails"
          : "This step fails";
    return `${who} when you execute it in Studio. Real runs ignore it.`;
  }
  codeLine(): string {
    const { code, outcome } = this.failure();
    const parts = [code, OUTCOMES[outcome ?? "failed"] ?? OUTCOMES["failed"]];
    if (this.scope() === "every") parts.push("Fails for every item");
    return parts.join(" · ");
  }
}
