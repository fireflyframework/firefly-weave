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
// and Remove. Over pinned output it says the pin is kept for later. When the
// step can no longer take a failure, it says Execute leaves it out, and why.
// A failure of one of its undo steps says whose it is and how often it fails.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { ErrorEntry } from "../../state/test-data";
import type { Phase } from "../../transactions/phase-keys";
import {
  bannerLine,
  bannerSentence,
  PHASE_NOUNS,
} from "../../transactions/simulate-undo";
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
      <button
        type="button"
        class="text-link"
        [attr.aria-label]="undo() && 'Remove the failure of its ' + undo()"
        (click)="remove.emit()"
      >
        Remove
      </button>
    </p>
    <p class="failure-banner-code">
      <span class="failure-code">{{ codeLine() }}</span>
      @if (undoLine(); as line) {
        <span class="failure-undo"> · {{ line }}</span>
      }
      &ngsp;
      <!-- An undo step the step no longer has has nothing to edit. -->
      @if (!undo() || !leftOut()) {
        <button
          type="button"
          class="text-link"
          [attr.aria-label]="undo() && 'Edit the failure of its ' + undo()"
          (click)="edit.emit()"
        >
          Edit
        </button>
      }
    </p>
    @if (sessionOnly()) {
      <p class="pinned-banner-note">
        This platform can't save simulated failures yet. They last until you
        close this workflow.
      </p>
    }
    @if (overPin()) {
      <p class="pinned-banner-note">
        Its {{ undo() ? "test output" : "pinned output" }} is kept for when you
        remove the failure.
      </p>
    }
  </div>`,
})
export class FailureBanner {
  failure = input.required<ErrorEntry>();
  /** The step it is over, and which of its undo steps fails; null when the step does. */
  step = input("");
  phase = input<Phase | null>(null);
  /** How often a failing undo step runs: its transaction's Max tries now. */
  tries = input(1);
  /** "step" outside loops; inside one, the picked item's own or every item's. */
  scope = input<"step" | "item" | "every">("step");
  /** The platform can't save simulated failures: this one lasts for the session. */
  sessionOnly = input(false);
  /** Pinned output is there too, kept while the failure is. */
  overPin = input(false);
  /** Why Execute leaves the failure out under the step's current setting; null when it applies. */
  leftOut = input<string | null>(null);
  readonly remove = output<void>();
  readonly edit = output<void>();
  /** "undo", "release" or "confirm"; null when the step itself fails. */
  undo(): string | null {
    const phase = this.phase();
    return phase && PHASE_NOUNS[phase];
  }
  sentence(): string {
    const leftOut = this.leftOut();
    if (leftOut) return leftOut;
    const phase = this.phase();
    if (phase) return bannerSentence(phase, this.scope());
    const who =
      this.scope() === "item"
        ? "This item fails"
        : this.scope() === "every"
          ? "Every item fails"
          : "This step fails";
    return `${who} when you execute it in Studio. Real runs ignore it.`;
  }
  /** After an undo step's code: which undo step, and how often it fails. */
  undoLine(): string | null {
    const phase = this.phase();
    return phase && bannerLine(this.step(), phase, this.tries());
  }
  codeLine(): string {
    const { code, outcome } = this.failure();
    if (this.phase()) return code;
    const parts = [code, OUTCOMES[outcome ?? "failed"] ?? OUTCOMES["failed"]];
    if (this.scope() === "every") parts.push("Fails for every item");
    return parts.join(" · ");
  }
}
