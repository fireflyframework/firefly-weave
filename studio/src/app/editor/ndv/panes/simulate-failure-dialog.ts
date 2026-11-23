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
// Simulate a failure of a step: the code it fails with (suggested from the
// action's own failures, then the platform's for its kind), the outcome,
// and inside a loop which items fail. A native modal dialog: the browser
// contains focus, closes it on Escape and makes the page inert.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  input,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { CodeSuggestion } from "./simulate-failure";
import { codeProblem } from "./simulate-failure";

export interface FailureChoice {
  code: string;
  outcome: "failed" | "unknown" | "not_started";
  every: boolean;
}

let sequence = 0;

@Component({
  selector: "weave-simulate-failure-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<dialog
    #dialog
    class="modal-panel simulate-failure"
    [attr.aria-labelledby]="id + '-title'"
    (close)="closed.emit()"
  >
    <form class="dialog-form" novalidate (submit)="submit($event)">
      <h2 [id]="id + '-title'">Simulate a failure of {{ step() }}</h2>
      <p class="dialog-message">
        Execute step and Execute workflow fail this step with this code; its On
        error setting decides what happens next. Real runs ignore it.
      </p>
      <label class="field-label" [for]="id + '-code'">Code</label>
      <!-- The browser's list of suggestions, opened from Studio's chevron. -->
      <div class="failure-code-field">
        <input
          #code
          [id]="id + '-code'"
          type="text"
          autofocus
          autocomplete="off"
          spellcheck="false"
          maxlength="128"
          [attr.list]="id + '-codes'"
          [attr.aria-describedby]="
            id + '-hint' + (problem() ? ' ' + id + '-error' : '')
          "
          [attr.aria-invalid]="problem() ? 'true' : null"
          [value]="codeValue()"
          (input)="typed($event)"
        />
        <weave-icon
          class="failure-code-chevron"
          name="chevronDown"
          [size]="16"
          aria-hidden="true"
        />
      </div>
      <datalist [id]="id + '-codes'">
        @for (group of suggestions(); track group.label) {
          @for (item of group.items; track item.code) {
            <option [value]="item.code" [label]="item.label"></option>
          }
        }
      </datalist>
      <p class="hint" [id]="id + '-hint'">
        Capital letters, digits and . _ - (up to 128). Any code the step could
        return.
      </p>
      @if (problem()) {
        <p class="error" role="alert" [id]="id + '-error'">{{ problem() }}</p>
      }
      @if (!fixedOutcome()) {
        <fieldset class="failure-choice">
          <legend class="field-label">Outcome</legend>
          <label>
            <input
              type="radio"
              [name]="id + '-outcome'"
              value="failed"
              [checked]="outcome() === 'failed'"
              (change)="outcome.set('failed')"
            />
            <span><b>Didn't happen</b> The step had no effect.</span>
          </label>
          <label>
            <input
              type="radio"
              [name]="id + '-outcome'"
              value="unknown"
              [checked]="outcome() === 'unknown'"
              (change)="outcome.set('unknown')"
            />
            <span
              ><b>Unknown</b> Nobody knows whether the step had an effect.</span
            >
          </label>
        </fieldset>
        @if (outcome() === "unknown" && changesData()) {
          <p class="failure-warning" role="status">
            {{ step() }} changes data, so an unknown outcome stops the run with
            an incident, whatever On error says.
          </p>
        }
      }
      @if (item(); as picked) {
        <fieldset class="failure-choice failure-items">
          <legend class="field-label">Which items fail</legend>
          <label>
            <input
              type="radio"
              [name]="id + '-items'"
              [checked]="!every()"
              (change)="every.set(false)"
            />
            <span>Only item {{ picked }}</span>
          </label>
          <label>
            <input
              type="radio"
              [name]="id + '-items'"
              [checked]="every()"
              (change)="every.set(true)"
            />
            <span>Every item</span>
          </label>
        </fieldset>
        <p class="hint">
          {{
            every()
              ? "Every item of " + loop() + " fails at this step."
              : "Item " +
                picked +
                " of " +
                loop() +
                " fails; the other items run as usual."
          }}
        </p>
      }
      <div class="dialog-actions">
        <button type="button" (click)="dialog.close()">Cancel</button>
        <button type="submit" class="primary">Simulate failure</button>
      </div>
    </form>
  </dialog>`,
})
export class SimulateFailureDialog {
  readonly id = `simulate-failure-${++sequence}`;
  step = input.required<string>();
  suggestions = input<{ label: string; items: CodeSuggestion[] }[]>([]);
  /** The item picked inside a loop's body ("3"); null outside loops. */
  item = input<string | null>(null);
  /** The innermost loop around the step, for the hint. */
  loop = input("");
  /** A step that changes data and is never retried: Unknown stops the run. */
  changesData = input(false);
  /** The outcome the failure takes without asking (a transform's didn't start); null asks. */
  fixedOutcome = input<"not_started" | null>(null);
  /** The failure being edited, when there is one. */
  initial = input<FailureChoice | null>(null);
  readonly chosen = output<FailureChoice>();
  readonly closed = output<void>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  readonly codeInput = viewChild.required<ElementRef<HTMLInputElement>>("code");
  readonly codeValue = signal("");
  readonly outcome = signal<"failed" | "unknown">("failed");
  readonly every = signal(false);
  readonly problem = signal("");

  constructor() {
    afterNextRender(() => {
      const start = this.initial();
      this.codeValue.set(
        start?.code ?? this.suggestions()[0]?.items[0]?.code ?? "",
      );
      this.outcome.set(start?.outcome === "unknown" ? "unknown" : "failed");
      this.every.set(start?.every ?? false);
      this.dialog().nativeElement.showModal();
      // Starts on the first suggestion, selected: typing replaces it.
      const field = this.codeInput().nativeElement;
      field.value = this.codeValue();
      field.select();
    });
  }
  typed(event: Event) {
    this.codeValue.set((event.target as HTMLInputElement).value);
    this.problem.set("");
  }
  submit(event: Event) {
    event.preventDefault();
    const code = this.codeValue().trim();
    const problem = codeProblem(code);
    if (problem) {
      this.problem.set(problem);
      this.codeInput().nativeElement.focus();
      return;
    }
    this.chosen.emit({
      code,
      outcome: this.fixedOutcome() ?? this.outcome(),
      every: this.every(),
    });
    this.dialog().nativeElement.close();
  }
}
