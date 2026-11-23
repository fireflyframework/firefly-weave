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
// The banner over a step's Output once it failed and its On error went on:
// what happened first (the code and message, the outcome and attempts, the
// setting that handled it), then, when the failure was simulated and is
// still set, that you set it, with Remove and Edit. One banner, never the
// "fails when executed" banner stacked over it.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../../icon";
import { failureCodeText, failureMessageText } from "../../canvas/tile-facts";
import { genericFailureMessage } from "../../state/failure-codes";

/** What an outcome means, as the dialog offers it and the banners say it. */
export const OUTCOME_WORDS: Readonly<Record<string, string>> = {
  failed: "Didn't happen",
  unknown: "Unknown",
  not_started: "Didn't start",
};

/** A failure a step's On error handled, as the banner reads it. */
export interface HandledFailureView {
  code: string;
  message: string;
  /** null when a recorded run no longer holds it: the banner then gives the attempts alone. */
  outcome: string | null;
  attempts: number;
  policy: "continue" | "errorOutput";
}

/**
 * The banner's lines: "This step failed: CODE.", the message, the outcome
 * and attempts, the setting. A message that only repeats the code is left
 * out; the data picker's error fields still hold it.
 */
export function errorBannerLines(failure: HandledFailureView): {
  failed: string;
  message: string;
  outcome: string;
  setting: string;
} {
  const attempts = `Attempts: ${failure.attempts.toLocaleString("en-US")}.`;
  const outcome =
    failure.outcome === null
      ? null
      : (OUTCOME_WORDS[failure.outcome] ?? OUTCOME_WORDS["failed"]);
  return {
    failed: `This step failed: ${failureCodeText(failure.code)}.`,
    message:
      failure.message === genericFailureMessage(failure.code)
        ? ""
        : failureMessageText(failure.message),
    outcome: outcome ? `Outcome: ${outcome}. ${attempts}` : attempts,
    setting: `The run continued (On error: ${
      failure.policy === "errorOutput"
        ? "Continue with error output"
        : "Continue"
    }).`,
  };
}

@Component({
  selector: "weave-error-banner",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `@let said = lines();
    <div class="pinned-banner error-banner" data-tone="warning">
      <weave-icon name="degraded" [size]="16" aria-hidden="true" />
      <p role="status">
        <strong class="error-banner-failed">{{ said.failed }}</strong>
        @if (said.message) {
          &ngsp;{{ said.message }}
        }
      </p>
      <p>{{ said.outcome }}</p>
      <p class="error-banner-setting">{{ said.setting }}</p>
      @if (simulated()) {
        <p class="error-banner-simulated">
          <weave-icon name="warning" [size]="16" aria-hidden="true" /><span
            >You simulated this failure. Real runs ignore it.</span
          >&ngsp;
          <button type="button" class="text-link" (click)="remove.emit()">
            Remove</button
          >&ngsp;
          <button type="button" class="text-link" (click)="edit.emit()">
            Edit
          </button>
        </p>
        @if (overPin()) {
          <p class="pinned-banner-note">
            Its pinned output is kept for when you remove the failure.
          </p>
        }
      }
    </div>`,
})
export class ErrorBanner {
  readonly failure = input.required<HandledFailureView>();
  /** The failure was simulated and is still set: say so, with Remove and Edit. */
  readonly simulated = input(false);
  /** Pinned output is there too, kept for when the simulated failure is removed. */
  readonly overPin = input(false);
  readonly remove = output<void>();
  readonly edit = output<void>();
  lines() {
    return errorBannerLines(this.failure());
  }
}
