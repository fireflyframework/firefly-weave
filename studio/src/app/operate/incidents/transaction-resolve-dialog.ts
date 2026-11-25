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
// One decision on a transaction in doubt: what it asks for, why it waits while
// something is missing, and what the platform refused. Its checklist and what
// stays done come from the incident it is given, which the page reads right
// before it opens; once that incident changed, nothing is sent until it is
// read again, and every step is ticked again.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from "@angular/core";
import { Modal } from "../../dialog";
import type { PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { REASON_LIMIT } from "../operate-limits";
import {
  RESOLUTION_LABELS,
  staysDone,
} from "../transactions/transaction-record";
import {
  transactionStep,
  type IncidentView,
  type ResolutionRequest,
} from "./incidents-model";
import {
  afterRevisionRefresh,
  checkForceEnd,
  checkReconcile,
  checkRetry,
  checkStepOutcome,
  checklistChanged,
  checklistRows,
  confirmReason,
  endingHelp,
  outputRefusalAt,
  transactionActions,
  type Checked,
  type ReconcileDraft,
  type TransactionAction,
} from "./transaction-resolutions";

/** The platform refuses the result, or the record of how the transaction ended. */
const OUTPUT = "WV-RUNTIME-OUTPUT";
const ENDINGS = [
  { as: "compensated", label: "Undone" },
  { as: "completed", label: "Finished" },
] as const;

@Component({
  selector: "weave-transaction-resolve-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, Modal],
  styleUrl: "./transaction-resolve-dialog.css",
  template: `@let d = draft();
    @let wrong = problems();
    <weave-modal
      [heading]="offer().label"
      describedBy="decide-help"
      (dismiss)="dismiss.emit()"
    >
      <form class="decide-form" novalidate (submit)="submit($event)">
        <p class="dialog-message" id="decide-help">{{ offer().help }}</p>
        <p class="context">
          @if (about(); as subject) {
            {{ subject.what }} <code>{{ subject.name }}</code>
            {{ subject.joined }}
          }
          {{ about() ? "run" : "Run" }} <code>{{ short() }}</code>
        </p>
        @if (notice(); as refused) {
          <div class="notice" data-tone="danger" role="alert">
            <p>{{ refused.message }}</p>
            @if (refused.code) {
              <p class="support-code">Support code: {{ refused.code }}</p>
            }
            @if (stale()) {
              <button
                type="button"
                class="sm"
                [attr.aria-busy]="refreshing() ? 'true' : null"
                (click)="refresh.emit()"
              >
                @if (refreshing()) {
                  <weave-icon name="spinner" [size]="16" class="spin" />
                }
                Refresh incident
              </button>
            }
          </div>
        }
        @if (action() === "mark_reconciled") {
          @if (rebuilt(); as told) {
            <div class="notice" data-tone="info" role="status">
              <weave-icon name="info" [size]="16" />
              <p>{{ told }}</p>
            </div>
          }
          <fieldset class="endings">
            <legend>How did it end?</legend>
            @for (ending of endings(); track ending.as) {
              <label class="ending"
                ><input
                  type="radio"
                  name="decide-as"
                  [value]="ending.as"
                  [checked]="d.as === ending.as"
                  (change)="end(ending.as)"
                /><span
                  ><span>{{ ending.label }}</span
                  ><small>{{ ending.help }}</small></span
                ></label
              >
            }
          </fieldset>
          @if (rows().length) {
            @let refused = outputRefused() === "checklist";
            <fieldset
              class="checklist"
              [attr.aria-invalid]="refused ? 'true' : null"
              [attr.aria-describedby]="
                refused ? 'decide-checklist-error' : 'decide-checklist-help'
              "
            >
              <legend>Check each step in its system</legend>
              @for (row of rows(); track row.instanceKey) {
                <label class="tick"
                  ><input
                    type="checkbox"
                    [checked]="d.ticked.has(row.instanceKey)"
                    [attr.aria-labelledby]="'decide-line-' + $index"
                    [attr.aria-describedby]="'decide-caption-' + $index"
                    (change)="tick(row.instanceKey, $event)"
                  /><span [id]="'decide-line-' + $index">{{
                    row.sentence
                  }}</span
                  ><small [id]="'decide-caption-' + $index"
                    ><span>{{
                      row.caption.slice(0, -row.operationKey.length)
                    }}</span
                    ><code>{{ row.operationKey }}</code></small
                  ></label
                >
              }
              @if (refused) {
                <p class="field-error" id="decide-checklist-error" role="alert">
                  <weave-icon name="failCircle" [size]="16" />{{ refusal() }}
                </p>
              }
              <p class="field-help" id="decide-checklist-help">
                Only the steps you need to check for this ending are listed.
              </p>
            </fieldset>
          }
        }
        @if (resultLabel(); as label) {
          @let error = resultError();
          <div class="field">
            <label for="decide-result">{{ label }} (JSON)</label>
            <textarea
              id="decide-result"
              class="monospace"
              spellcheck="false"
              autocomplete="off"
              [value]="d.blockOutput"
              [attr.aria-invalid]="error ? 'true' : null"
              [attr.aria-describedby]="
                (error ? 'decide-result-error ' : '') + 'decide-result-help'
              "
              (input)="set({ blockOutput: value($event) })"
            ></textarea>
            @if (error) {
              <p class="field-error" id="decide-result-error" role="alert">
                <weave-icon name="failCircle" [size]="16" />{{ error }}
              </p>
            }
            <p class="field-help" id="decide-result-help">
              @if (action() === "it_happened") {
                What {{ step() ?? "the step" }} would have returned. The steps
                after it read this.
              } @else {
                What the transaction returns to the steps after it, as its
                output says.
              }
            </p>
          </div>
        }
        @if (action() === "force_end" && lines().length) {
          <section class="stays-done" aria-labelledby="decide-stays-title">
            <h3 id="decide-stays-title">Stays done in other systems</h3>
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
        <div class="field">
          <div class="label-row">
            <label for="decide-reason">Reason</label>
            <span class="count" aria-hidden="true">{{ count() }}</span>
          </div>
          <textarea
            id="decide-reason"
            [value]="d.reason"
            [attr.aria-invalid]="wrong.reason ? 'true' : null"
            [attr.aria-describedby]="
              wrong.reason ? 'decide-reason-error' : null
            "
            (input)="set({ reason: value($event) })"
          ></textarea>
          @if (wrong.reason) {
            <p class="field-error" id="decide-reason-error" role="alert">
              <weave-icon name="failCircle" [size]="16" />{{ wrong.reason }}
            </p>
          }
        </div>
        @if (asksEvidence()) {
          <div class="field">
            <label for="decide-evidence">Evidence reference</label>
            <input
              id="decide-evidence"
              autocomplete="off"
              [value]="d.evidence"
              [attr.aria-invalid]="wrong.evidence ? 'true' : null"
              [attr.aria-describedby]="
                (wrong.evidence ? 'decide-evidence-error ' : '') +
                'decide-evidence-help'
              "
              (input)="set({ evidence: value($event) })"
            />
            @if (wrong.evidence) {
              <p class="field-error" id="decide-evidence-error" role="alert">
                <weave-icon name="failCircle" [size]="16" />{{ wrong.evidence }}
              </p>
            }
            <p class="field-help" id="decide-evidence-help">
              A ticket, log line or document that shows it. Stored as text in
              the audit log.
            </p>
          </div>
        }
        @if (action() === "force_end") {
          <div class="field">
            <label for="decide-typed">Type {{ short() }} to confirm</label>
            <input
              id="decide-typed"
              class="typed-id"
              autocomplete="off"
              autocapitalize="none"
              spellcheck="false"
              [value]="typed()"
              (input)="typed.set(value($event))"
            />
          </div>
        }
        <div class="dialog-footer">
          @if (waits(); as reason) {
            <p class="confirm-reason" id="decide-reason-missing">
              @for (part of parts(reason); track $index) {
                @if ($odd) {
                  <code>{{ part }}</code>
                } @else {
                  <span>{{ part }}</span>
                }
              }
            </p>
          }
          <button type="button" (click)="dismiss.emit()">Cancel</button>
          <button
            type="submit"
            [class.primary]="action() !== 'force_end'"
            [class.danger-solid]="action() === 'force_end'"
            [attr.aria-disabled]="waits() || busy() ? 'true' : null"
            [attr.aria-busy]="busy() ? 'true' : null"
            [attr.aria-describedby]="waits() ? 'decide-reason-missing' : null"
          >
            @if (busy()) {
              <weave-icon name="spinner" [size]="16" class="spin" />Resolving…
            } @else {
              {{ offer().label }}
            }
          </button>
        </div>
      </form>
    </weave-modal>`,
})
export class TransactionResolveDialog {
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  /** The incident being decided on, as it was read last. */
  incident = input.required<IncidentView>();
  action = input.required<TransactionAction>();
  /** The decision is on its way. */
  busy = input(false);
  /** The last refusal, explained. */
  failure = input<PlainError | null>(null);
  /** The incident is being read again. */
  refreshing = input(false);
  /** A refused record may be of steps that changed: the incident couldn't be read to tell. */
  unread = input(false);
  decide = output<ResolutionRequest>();
  dismiss = output<void>();
  refresh = output<void>();

  readonly endings = computed(() =>
    ENDINGS.map((ending) => ({
      ...ending,
      help: endingHelp(ending.as, this.incident().transaction),
    })),
  );
  readonly draft = signal<ReconcileDraft>({
    as: "",
    ticked: new Set(),
    blockOutput: "",
    reason: "",
    evidence: "",
  });
  readonly typed = signal("");
  /** The confirm was pressed with something wrong: its problems show. */
  private readonly attempted = signal(false);
  /** The revision the platform refused as an older one, and that refusal. */
  private readonly outdated = signal<{
    revision: number;
    refused: PlainError;
  } | null>(null);
  /** The refusal whose result, ticks or ending were changed since. */
  private readonly corrected = signal<PlainError | null>(null);
  /** The checklist was built again since a decision was sent last. */
  private readonly builtAgain = signal(false);

  /** The decision as the drawer offered it: the platform checks who may take it. */
  readonly offer = computed(() => {
    const action = this.action();
    return transactionActions(this.incident(), () => true).find(
      (offer) => offer.action === action,
    )!;
  });
  readonly short = computed(() => shortId(this.incident().run_id));
  readonly step = computed(() => transactionStep(this.incident()));
  /** What the decision is about: the transaction, or the step it waits for. */
  readonly about = computed(() => {
    const action = this.action();
    const transaction = this.incident().transaction?.instance_key;
    const whole =
      action === "mark_reconciled" ||
      action === "force_end" ||
      this.offer().label === RESOLUTION_LABELS.wait_again;
    const name = whole ? transaction : this.step();
    if (!name) return null;
    return {
      what: whole ? "Transaction" : "Step",
      name,
      joined: action === "force_end" ? "and" : "in",
    };
  });
  readonly lines = computed(() => {
    const transaction = this.incident().transaction;
    return transaction ? staysDone([transaction]) : [];
  });
  /** The checklist, once the person said how the transaction ended. */
  readonly rows = computed(() => {
    const { as } = this.draft();
    return this.action() === "mark_reconciled" && as
      ? checklistRows(this.incident(), as)
      : [];
  });
  readonly asksEvidence = computed(
    () => this.action() !== "retry" && this.action() !== "force_end",
  );
  readonly resultLabel = computed(() =>
    this.action() === "it_happened"
      ? "Result of the step"
      : this.action() === "mark_reconciled" && this.draft().as === "completed"
        ? "Result of the transaction"
        : null,
  );
  readonly count = computed(
    () =>
      `${this.draft().reason.length.toLocaleString("en-US")} / ${REASON_LIMIT.toLocaleString("en-US")}`,
  );

  private readonly checked = computed<Checked<"blockOutput" | "output">>(() => {
    const draft = this.draft();
    switch (this.action()) {
      case "retry":
        return checkRetry(draft);
      case "mark_reconciled":
        // A result typed for a finished transaction stays out of sight, and unsent, with one that was undone.
        return checkReconcile(
          draft.as === "completed" ? draft : { ...draft, blockOutput: "" },
          this.rows(),
        );
      case "force_end":
        return checkForceEnd(
          { reason: draft.reason, typed: this.typed() },
          this.incident().run_id,
        );
      case "it_happened":
      case "it_didnt_happen":
        return checkStepOutcome({
          happened: this.action() === "it_happened",
          output: draft.blockOutput,
          reason: draft.reason,
          evidence: draft.evidence,
        });
    }
  });
  readonly problems = computed(() => {
    const checked = this.checked();
    return "problems" in checked ? checked.problems : {};
  });
  /** The incident changed, or may have: nothing is sent until it is read again. */
  readonly stale = computed(
    () =>
      this.unread() || this.outdated()?.revision === this.incident().revision,
  );
  /** What to do now that its checklist was built again, even with no step left on it. */
  readonly rebuilt = computed(() => {
    if (!this.builtAgain() || this.stale()) return "";
    return this.rows().length
      ? "Studio read the incident again and rebuilt the checklist. Tick each step again; your reason and evidence are kept."
      : "Studio read the incident again and it changed: no step is left to tick. Check it again, then decide; your reason and evidence are kept.";
  });
  /** Why the confirm waits; null when it can be pressed. */
  readonly waits = computed(() => {
    const { action, label } = this.offer();
    const checked = this.checked();
    return confirmReason(
      action,
      this.stale()
        ? [{ kind: "refresh" }]
        : "missing" in checked
          ? checked.missing
          : [],
      label,
    );
  });

  /** The last refusal, until what the platform refused is changed. */
  private readonly refused = computed(() => {
    const failure = this.failure();
    return failure?.code === OUTPUT && failure === this.corrected()
      ? null
      : failure;
  });
  /** Where a refused output shows; null when the refusal isn't about one. */
  readonly outputRefused = computed(() => {
    const failure = this.refused();
    if (failure?.code !== OUTPUT) return null;
    if (this.action() === "it_happened") return "blockOutput";
    if (this.action() !== "mark_reconciled") return null;
    const { as } = this.draft();
    const at = outputRefusalAt(failure.message, as, this.rows());
    return at === "checklist" && !this.rows().length ? null : at;
  });
  readonly refusal = computed(() => {
    const failure = this.failure();
    return failure ? `${failure.message} (${failure.code})` : "";
  });
  /** A refusal no field shows; why the incident must be read again, until it is. */
  readonly notice = computed(() => {
    if (this.outputRefused()) return null;
    return (
      this.refused() ??
      (this.stale() ? (this.outdated()?.refused ?? null) : null)
    );
  });
  readonly resultError = computed(() => {
    const { blockOutput, output } = this.problems() as Partial<
      Record<"blockOutput" | "output", string>
    >;
    if (this.attempted() && (blockOutput ?? output))
      return (blockOutput ?? output)!;
    return this.outputRefused() === "blockOutput" ? this.refusal() : "";
  });

  constructor() {
    // The dialog's title takes the focus, so what it is about is read first.
    afterNextRender(() =>
      queueMicrotask(() => {
        const heading = this.element.nativeElement.querySelector("h2");
        if (!heading) return;
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }),
    );
    effect(() => {
      const refused = this.failure();
      if (refused?.code === "WV-INCIDENT-REVISION")
        this.outdated.set({
          revision: untracked(() => this.incident().revision),
          refused,
        });
    });
    // The steps of another record are checked again, whatever was ticked.
    let read: IncidentView | null = null;
    effect(() => {
      const now = this.incident();
      if (read && checklistChanged(read, now)) {
        this.draft.update(afterRevisionRefresh);
        this.builtAgain.set(true);
      }
      read = now;
    });
    // What was refused takes the focus, unless the person is in another field.
    effect(() => {
      const at = this.outputRefused();
      if (at && !document.activeElement?.matches("input, textarea"))
        this.focusWhenDrawn(
          at === "checklist" ? ".tick input" : "#decide-result",
        );
    });
  }

  set(change: Partial<ReconcileDraft>) {
    if ("blockOutput" in change || "ticked" in change)
      this.corrected.set(this.failure());
    this.draft.update((draft) => ({ ...draft, ...change }));
  }
  /** Another ending asks another question of each step: nothing is ticked for it yet. */
  end(as: ReconcileDraft["as"]) {
    if (as !== this.draft().as) this.set({ as, ticked: new Set() });
  }
  tick(key: string, event: Event) {
    const ticked = new Set(this.draft().ticked);
    if ((event.target as HTMLInputElement).checked) ticked.add(key);
    else ticked.delete(key);
    this.set({ ticked });
  }
  /** A reason's words, and between them the step it names. */
  parts(reason: string) {
    return reason.split("`");
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }
  submit(event: Event) {
    event.preventDefault();
    if (this.busy() || this.waits()) return;
    const checked = this.checked();
    this.attempted.set(!("request" in checked));
    if ("request" in checked) {
      this.builtAgain.set(false);
      this.decide.emit(checked.request);
    } else this.focusWhenDrawn(':is(input, textarea)[aria-invalid="true"]');
  }
  private focusWhenDrawn(selector: string) {
    afterNextRender(
      () =>
        this.element.nativeElement
          .querySelector<HTMLElement>(selector)
          ?.focus(),
      { injector: this.injector },
    );
  }
}
