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
// and inside a loop which items fail. Inside a transaction, what fails (the
// step or one of its undo steps) and what the step's failure may have
// changed. A native modal dialog: the browser contains focus, closes it on
// Escape and makes the page inert.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  computed,
  input,
  linkedSignal,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { Phase } from "../../transactions/phase-keys";
import {
  effectChoices,
  effectReason,
  failureLead,
  phaseNotes,
  shownEffect,
  type Effect,
} from "../../transactions/simulate-undo";
import { codeProblem } from "./simulate-failure";
import type { FailureChoice, SimulateOpening } from "./simulate-opening";

let sequence = 0;

@Component({
  selector: "weave-simulate-failure-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `@let open = opening();
    @let inside = open.inside;
    <dialog
      #dialog
      class="modal-panel simulate-failure"
      [class.in-transaction]="inside"
      [attr.aria-labelledby]="id + '-title'"
      (close)="closed.emit()"
    >
      <form class="dialog-form" novalidate (submit)="submit($event)">
        <h2 [id]="id + '-title'">Simulate a failure of {{ open.step }}</h2>
        <p class="dialog-message">{{ lead() }}</p>
        @if (inside && inside.phases.length) {
          <fieldset class="failure-what-field">
            <legend class="field-label">What fails</legend>
            <div class="failure-what">
              <label>
                <input
                  type="radio"
                  [name]="id + '-what'"
                  [checked]="phase() === null"
                  [attr.autofocus]="phase() === null ? '' : null"
                  (change)="choose(null)"
                /><span>The step</span>
              </label>
              @for (part of inside.phases; track part.phase) {
                <label>
                  <input
                    type="radio"
                    [name]="id + '-what'"
                    [checked]="phase() === part.phase"
                    [attr.autofocus]="phase() === part.phase ? '' : null"
                    (change)="choose(part.phase)"
                  /><span>{{ part.label }}</span>
                </label>
              }
            </div>
          </fieldset>
        }
        <label class="field-label" [for]="id + '-code'">Code</label>
        <!-- The browser's list of suggestions, opened from Studio's chevron. -->
        <div class="failure-code-field">
          <input
            #code
            [id]="id + '-code'"
            type="text"
            [attr.autofocus]="inside?.phases?.length ? null : ''"
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
          Capital letters, digits and . _ - (up to 128). Any code the
          {{ phase() ? ran() : "step" }} could return.
        </p>
        @if (problem()) {
          <p class="error" role="alert" [id]="id + '-error'">{{ problem() }}</p>
        }
        @for (note of notes(); track note) {
          <p class="failure-note">
            <weave-icon name="info" [size]="16" aria-hidden="true" /><span>{{
              note
            }}</span>
          </p>
        }
        @if (!phase() && !open.outcome) {
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
              <span
                ><b>Didn't happen</b>
                {{
                  inside ? "The step failed." : "The step had no effect."
                }}</span
              >
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
                ><b>Unknown</b> Nobody knows whether the step had an
                effect.</span
              >
            </label>
          </fieldset>
          <!-- Inside a transaction, what may have changed says what happens. -->
          @if (!inside && outcome() === "unknown" && open.changesData) {
            <p class="failure-warning" role="status">
              {{ open.step }} changes data, so an unknown outcome stops the run
              with an incident, whatever On error says.
            </p>
          }
        }
        @if (!phase() && inside?.participant) {
          @let words = effects()!;
          <fieldset
            class="failure-choice"
            [attr.aria-describedby]="reason() ? id + '-reason' : null"
          >
            <legend class="field-label">Did it change anything?</legend>
            <label>
              <input
                type="radio"
                [name]="id + '-effect'"
                value="unknown"
                [checked]="effect() === 'unknown'"
                (change)="picked.set('unknown')"
              />
              <span><b>It may have</b> {{ words.may }}</span>
            </label>
            <label>
              <input
                type="radio"
                [name]="id + '-effect'"
                value="none"
                [checked]="effect() === 'none'"
                [disabled]="outcome() === 'unknown'"
                (change)="picked.set('none')"
              />
              <span><b>It changed nothing</b> {{ words.nothing }}</span>
            </label>
          </fieldset>
          @if (reason(); as why) {
            <p class="failure-note failure-reason" [id]="id + '-reason'">
              <weave-icon name="info" [size]="16" aria-hidden="true" /><span>{{
                why
              }}</span>
            </p>
          }
        }
        @if (open.item; as item) {
          <fieldset class="failure-choice failure-items">
            <legend class="field-label">Which items fail</legend>
            <label>
              <input
                type="radio"
                [name]="id + '-items'"
                [checked]="!every()"
                (change)="every.set(false)"
              />
              <span>Only item {{ item }}</span>
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
                ? "Every item of " + open.loop + " fails at this step."
                : "Item " +
                  item +
                  " of " +
                  open.loop +
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
  readonly opening = input.required<SimulateOpening>();
  readonly chosen = output<FailureChoice>();
  readonly closed = output<void>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  readonly codeInput = viewChild.required<ElementRef<HTMLInputElement>>("code");
  /** What fails: the step, or one of its undo steps. */
  readonly phase = linkedSignal<Phase | null>(() => this.opening().phase);
  readonly codeValue = signal("");
  readonly outcome = signal<"failed" | "unknown">("failed");
  /** What the builder said it changed; null while it follows the code and the outcome. */
  readonly picked = signal<Effect | null>(null);
  readonly every = signal(false);
  readonly problem = signal("");

  readonly lead = computed(() =>
    failureLead(this.opening().step, this.opening().inside, this.phase()),
  );
  private readonly part = computed(() =>
    this.opening().inside?.phases.find(({ phase }) => phase === this.phase()),
  );
  /** What runs the undo step, as the hint names it. */
  readonly ran = computed(() =>
    this.part()?.kind === "callWorkflow" ? "workflow" : "action",
  );
  readonly suggestions = computed(
    () => this.part()?.suggestions ?? this.opening().suggestions,
  );
  readonly notes = computed(() => {
    const { step, inside } = this.opening();
    const phase = this.phase();
    return inside && phase ? phaseNotes(step, inside, phase) : [];
  });
  private readonly participant = computed(
    () => this.opening().inside?.participant ?? null,
  );
  readonly effects = computed(() => {
    const participant = this.participant();
    return (
      participant &&
      effectChoices(this.opening().step, participant.mode, this.outcome())
    );
  });
  readonly effect = computed(() => {
    const participant = this.participant();
    return (
      participant &&
      shownEffect(participant, this.code(), this.outcome(), this.picked())
    );
  });
  readonly reason = computed(() => {
    const participant = this.participant();
    return (
      participant &&
      effectReason(participant, this.code(), this.outcome(), this.picked())
    );
  });
  private readonly code = computed(() => this.codeValue().trim());

  constructor() {
    afterNextRender(() => {
      this.start(this.opening().phase);
      this.dialog().nativeElement.showModal();
      // Starts on the first suggestion, selected: typing replaces it.
      const field = this.codeInput().nativeElement;
      if (document.activeElement === field) field.select();
    });
  }
  /** Starts on a part's failure as it is simulated now, or on its defaults. */
  private start(phase: Phase | null) {
    this.phase.set(phase);
    const start = this.opening().current.find((at) => at.phase === phase);
    this.codeValue.set(
      start?.code ??
        this.part()?.code ??
        this.suggestions()[0]?.items[0]?.code ??
        "",
    );
    this.outcome.set(start?.outcome === "unknown" ? "unknown" : "failed");
    // An answer the code would choose by itself still follows it.
    this.picked.set(null);
    if (start?.effect && start.effect !== this.effect())
      this.picked.set(start.effect);
    this.every.set(start?.every ?? false);
    this.problem.set("");
    // The box is drawn from codeValue only when it changes.
    this.codeInput().nativeElement.value = this.codeValue();
  }
  /** Another part fails: its own failure, or its defaults. */
  choose(phase: Phase | null) {
    const every = this.every();
    this.start(phase);
    // Which items stays as chosen unless this part's failure says.
    if (!this.opening().current.some((at) => at.phase === phase))
      this.every.set(every);
  }
  typed(event: Event) {
    this.codeValue.set((event.target as HTMLInputElement).value);
    this.problem.set("");
  }
  submit(event: Event) {
    event.preventDefault();
    const code = this.code();
    const problem = codeProblem(code);
    if (problem) {
      this.problem.set(problem);
      this.codeInput().nativeElement.focus();
      return;
    }
    const effect = this.phase() ? null : this.effect();
    this.chosen.emit({
      phase: this.phase(),
      code,
      outcome: this.opening().outcome ?? this.outcome(),
      ...(effect ? { effect } : {}),
      every: this.every(),
    });
    this.dialog().nativeElement.close();
  }
}
