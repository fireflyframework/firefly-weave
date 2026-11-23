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
// The two cancels of a run with transactions that the shell's form can't
// hold. Cancel run while a transaction is in doubt says so before anything is
// sent, with its incident, and can't be confirmed. Cancel without undoing
// lists what stays done in other systems, asks for a reason and for the run's
// short ID typed out, and sends only when both are there.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from "@angular/core";
import { Modal } from "../../dialog";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { REASON_LIMIT } from "../operate-limits";
import { reasonProblem } from "../runs/run-actions";
import { typedConfirmation, type StaysDoneLine } from "./transaction-record";

@Component({
  selector: "weave-run-cancel-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, Modal],
  styleUrl: "./run-cancel-dialog.css",
  template: `<weave-modal
    [heading]="
      mode() === 'in_doubt' ? 'Cancel this run?' : 'Cancel without undoing?'
    "
    [describedBy]="messageId"
    (dismiss)="dismiss.emit()"
  >
    <form class="cancel-form" (submit)="submit($event)">
      <div class="cancel-body">
        @if (mode() === "in_doubt") {
          <div class="notice" data-tone="danger" role="alert" [id]="messageId">
            <weave-icon name="failCircle" [size]="16" />
            <p>A transaction is in doubt. Resolve the transaction first.</p>
            @if (incidentId(); as incident) {
              <button
                type="button"
                class="sm"
                (click)="openIncident.emit(incident)"
              >
                Open incident
              </button>
            }
          </div>
        } @else {
          <p class="dialog-message" [id]="messageId">
            Steps already done stay done in other systems: Weave won't undo
            them, and the run's transactions end Force ended.
          </p>
          @if (lines().length) {
            <section class="stays-done" aria-labelledby="stays-done-title">
              <h3 id="stays-done-title">Stays done in other systems</h3>
              <ul>
                @for (line of lines(); track $index) {
                  <li>
                    <weave-icon [name]="line.howIcon" [size]="16" /><span
                      ><strong>{{ line.step }}</strong> · {{ line.how }}:
                      {{ line.what }}</span
                    >
                  </li>
                }
              </ul>
            </section>
          }
        }
        <div class="field">
          <div class="label-row">
            <label for="cancel-reason">Reason</label>
            @if (mode() === "without_undo") {
              <span class="reason-count" aria-hidden="true">{{ count() }}</span>
            }
          </div>
          <textarea
            id="cancel-reason"
            spellcheck="false"
            [attr.aria-invalid]="!!reasonError() || null"
            [attr.aria-describedby]="
              reasonError() ? 'cancel-reason-error' : null
            "
            [value]="reason()"
            (input)="reason.set(value($event))"
          ></textarea>
          @if (reasonError(); as error) {
            <p class="error" id="cancel-reason-error" role="alert">
              {{ error }}
            </p>
          }
        </div>
        @if (mode() === "without_undo") {
          <div class="field">
            <label for="cancel-typed">Type {{ short() }} to confirm</label>
            <input
              id="cancel-typed"
              class="typed-id"
              autocomplete="off"
              spellcheck="false"
              [value]="typed()"
              (input)="typed.set(value($event))"
            />
          </div>
        }
      </div>
      <div class="dialog-footer">
        <button type="button" (click)="dismiss.emit()">Keep running</button>
        <button type="submit" class="danger-solid" [disabled]="!ready()">
          {{
            mode() === "in_doubt"
              ? "Cancel run"
              : sending()
                ? "Canceling…"
                : "Cancel without undoing"
          }}
        </button>
      </div>
    </form>
  </weave-modal>`,
})
export class RunCancelDialog {
  mode = input.required<"in_doubt" | "without_undo">();
  runId = input.required<string>();
  lines = input<StaysDoneLine[]>([]);
  incidentId = input<string | null>(null);
  sending = input(false);
  confirm = output<string>();
  dismiss = output<void>();
  openIncident = output<string>();

  readonly messageId = "run-cancel-message";
  readonly reason = signal("");
  readonly typed = signal("");
  readonly short = computed(() => shortId(this.runId()));
  readonly count = computed(
    () =>
      `${this.reason().length.toLocaleString("en-US")} / ${REASON_LIMIT.toLocaleString("en-US")}`,
  );
  readonly reasonError = computed(() => reasonProblem(this.reason()));
  /** Only Cancel without undoing sends: with a reason and the run's short ID typed. */
  readonly ready = computed(
    () =>
      this.mode() === "without_undo" &&
      !this.sending() &&
      !!this.reason().trim() &&
      !this.reasonError() &&
      typedConfirmation(this.runId(), this.typed()),
  );

  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }
  submit(event: Event) {
    event.preventDefault();
    if (this.ready()) this.confirm.emit(this.reason());
  }
}
