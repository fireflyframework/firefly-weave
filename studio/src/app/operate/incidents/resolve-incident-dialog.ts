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
// "Resolve incident": one of three decisions, a reason for the audit log, and
// for an accepted result its evidence and output. Problems stay in the dialog.
import { Component, input, output } from "@angular/core";
import { Modal } from "../../dialog";
import type { PlainError } from "../../errors";
import { REASON_LIMIT } from "../operate-limits";
import {
  checkResolution,
  resolutions,
  type DraftProblems,
  type IncidentView,
  type ResolutionDraft,
  type ResolutionRequest,
} from "./incidents-model";

@Component({
  selector: "weave-resolve-incident-dialog",
  standalone: true,
  imports: [Modal],
  styleUrls: ["../operate.css", "./incidents.css"],
  template: `<weave-modal
    heading="Resolve incident"
    closeLabel="Close resolve incident"
    describedBy="resolve-description"
    [wide]="true"
    (dismiss)="cancel.emit()"
  >
    <p id="resolve-description" class="dialog-message">
      {{ incident().code }} at {{ incident().node_id || "the run" }}. Check the
      other system first: Weave can't look it up for you.
    </p>
    <form class="dialog-form resolve-form" novalidate (submit)="send($event)">
      <fieldset
        class="resolve-choices"
        [attr.aria-invalid]="problems.kind ? 'true' : null"
        [attr.aria-describedby]="problems.kind ? 'resolve-kind-error' : null"
      >
        <legend>Decision</legend>
        @for (choice of choices; track choice.kind) {
          <label class="resolve-choice"
            ><input
              type="radio"
              name="resolution"
              [value]="choice.kind"
              [checked]="draft.kind === choice.kind"
              (change)="draft.kind = choice.kind; problems = {}"
            /><span
              ><strong>{{ choice.label }}</strong
              ><small>{{ choice.help }}</small></span
            ></label
          >
        }
        @if (problems.kind) {
          <p class="field-error" id="resolve-kind-error">{{ problems.kind }}</p>
        }
      </fieldset>
      <div class="field">
        <label for="resolve-reason">Reason</label>
        <textarea
          id="resolve-reason"
          [attr.maxlength]="limit"
          [value]="draft.reason"
          [attr.aria-invalid]="problems.reason ? 'true' : null"
          [attr.aria-describedby]="
            problems.reason ? 'resolve-reason-error' : 'resolve-reason-help'
          "
          (input)="draft.reason = value($event)"
        ></textarea>
        <p class="field-help" id="resolve-reason-help">
          Kept in the audit log with your decision.
        </p>
        @if (problems.reason) {
          <p class="field-error" id="resolve-reason-error">
            {{ problems.reason }}
          </p>
        }
      </div>
      <div class="field">
        <label for="resolve-evidence">{{
          draft.kind === "accept_reconciled_result"
            ? "Evidence reference"
            : "Evidence reference (optional)"
        }}</label>
        <input
          id="resolve-evidence"
          [value]="draft.evidence"
          [attr.aria-invalid]="problems.evidence ? 'true' : null"
          [attr.aria-describedby]="
            problems.evidence
              ? 'resolve-evidence-error'
              : 'resolve-evidence-help'
          "
          (input)="draft.evidence = value($event)"
        />
        <p class="field-help" id="resolve-evidence-help">
          Where someone can check what you verified, such as a ticket or a
          provider receipt.
        </p>
        @if (problems.evidence) {
          <p class="field-error" id="resolve-evidence-error">
            {{ problems.evidence }}
          </p>
        }
      </div>
      @if (draft.kind === "accept_reconciled_result") {
        <div class="field">
          <label for="resolve-output">Verified result (JSON)</label>
          <textarea
            id="resolve-output"
            class="monospace"
            spellcheck="false"
            [value]="draft.output"
            [attr.aria-invalid]="outputProblem() ? 'true' : null"
            [attr.aria-describedby]="
              outputProblem() ? 'resolve-output-error' : null
            "
            (input)="draft.output = value($event)"
          ></textarea>
          @if (outputProblem()) {
            <p class="field-error" id="resolve-output-error">
              {{ outputProblem() }}
            </p>
          }
        </div>
      }
      @if (failure(); as failed) {
        @if (failed.code !== "WV-RUNTIME-OUTPUT") {
          <div class="notice error-notice" role="alert">
            <p>{{ failed.message }}</p>
            @if (failed.code) {
              <small class="support-code"
                >Support code: {{ failed.code }}</small
              >
            }
            @if (failed.code === "WV-INCIDENT-REVISION") {
              <button type="button" (click)="reload.emit()">
                Refresh incident
              </button>
            }
          </div>
        }
      }
      <div class="dialog-actions">
        <button type="button" (click)="cancel.emit()">Cancel</button>
        <button
          type="submit"
          class="primary"
          [attr.aria-disabled]="busy() ? 'true' : null"
        >
          {{ busy() ? "Resolving…" : "Resolve incident" }}
        </button>
      </div>
    </form>
  </weave-modal>`,
})
export class ResolveIncidentDialog {
  incident = input.required<IncidentView>();
  busy = input(false);
  /** The last refusal; WV-RUNTIME-OUTPUT shows under the result field. */
  failure = input<PlainError | null>(null);
  /** The checked decision; named so the form's own submit event can't reach it. */
  decide = output<ResolutionRequest>();
  cancel = output<void>();
  reload = output<void>();
  readonly choices = resolutions;
  readonly limit = REASON_LIMIT;
  draft: ResolutionDraft = { kind: "", reason: "", evidence: "", output: "" };
  problems: DraftProblems = {};

  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  outputProblem() {
    const failed = this.failure();
    return (
      this.problems.output ??
      (failed?.code === "WV-RUNTIME-OUTPUT" ? failed.message : "")
    );
  }
  send(event: Event) {
    event.preventDefault();
    if (this.busy()) return;
    const checked = checkResolution(this.draft);
    if ("problems" in checked) {
      this.problems = checked.problems;
      return;
    }
    this.problems = {};
    this.decide.emit(checked.request);
  }
}
