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
// The dialog that publishes the actions a workflow owns before Publish… or
// Simulate goes on: it lists the actions of Call an API, email and file
// steps that aren't published yet with the steps that use them, checks
// them, asks once, publishes them one by one, and says what happened when it
// stops partway.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  ViewEncapsulation,
  afterNextRender,
  inject,
  input,
  output,
  type OnDestroy,
  type OnInit,
} from "@angular/core";
import { Modal } from "../../../dialog";
import { HttpActionClient } from "../../../integrations/http-action-client";
import type { StepDetailsHost } from "../step-details-host";
import type {
  OwnedPublishOutcome,
  OwnedPublishRequest,
} from "../step-details-service";
import { reservedActionNames } from "./insert-owned";
import type { OwnedDocuments } from "./owned-documents";
import {
  OwnedPublisher,
  publishLabel,
  type OwnedRow,
  type OwnedRowState,
} from "./owned-publish";
import { publishedVersionsOf } from "./owned-versions";

const STATES: Record<OwnedRowState, { label: string; tone: string }> = {
  checking: { label: "Checking…", tone: "neutral" },
  ready: { label: "Ready", tone: "neutral" },
  conflict: { label: "Taken", tone: "warning" },
  blocked: { label: "Can't publish", tone: "danger" },
  publishing: { label: "Publishing…", tone: "neutral" },
  published: { label: "Published", tone: "success" },
  failed: { label: "Not published", tone: "danger" },
  unknown: { label: "Not confirmed", tone: "warning" },
};
let sequence = 0;

@Component({
  selector: "weave-owned-publish-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [Modal],
  styleUrl: "./owned-publish.css",
  template: `<weave-modal
    [heading]="title()"
    [describedBy]="messageId"
    closeLabel="Close"
    (dismiss)="dismiss()"
  >
    <div class="owned-publish">
      <p [id]="messageId" class="dialog-message">{{ message() }}</p>
      @if (publisher.rows().length) {
        <ul class="owned-list" [attr.aria-label]="listLabel()">
          @for (row of publisher.rows(); track row.uses) {
            <li class="owned-row" [attr.data-uses]="row.uses">
              <span class="owned-text">
                <span class="owned-title"
                  ><code class="owned-name">
                    @for (part of nameParts(row.name); track $index) {
                      <span class="owned-part">{{ part }}</span
                      ><wbr />
                    }</code
                  >&ngsp;<span class="owned-part"
                    >· {{ row.version }}</span
                  ></span
                >
                <span class="owned-meta">{{ usedBy(row) }}</span>
                @if (row.message) {
                  <span class="owned-note" [attr.data-tone]="state(row).tone">{{
                    row.message
                  }}</span>
                }
              </span>
              <span class="owned-state" [attr.data-tone]="state(row).tone">{{
                state(row).label
              }}</span>
            </li>
          }
        </ul>
      }
      <p class="owned-summary" role="status">{{ publisher.summary() }}</p>
      <p class="sr-only" aria-live="polite">{{ publisher.proposals() }}</p>
      <!-- One main button through every phase, so focus stays on it while
           the dialog is busy. -->
      <div class="dialog-footer">
        @if (secondaryLabel(); as label) {
          <button type="button" (click)="secondary()">{{ label }}</button>
        }
        <button
          type="button"
          class="primary"
          [attr.aria-disabled]="waiting() ? 'true' : null"
          (click)="primary()"
        >
          {{ primaryLabel() }}
        </button>
      </div>
    </div>
  </weave-modal>`,
})
export class OwnedPublishDialog implements OnInit, OnDestroy {
  readonly host = input.required<StepDetailsHost>();
  readonly request = input.required<OwnedPublishRequest>();
  readonly documents = input.required<OwnedDocuments>();
  readonly closed = output<OwnedPublishOutcome>();
  readonly messageId = `owned-publish-message-${++sequence}`;
  publisher!: OwnedPublisher;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private finished = false;

  ngOnInit(): void {
    const host = this.host();
    const documents = this.documents();
    const client = new HttpActionClient(host.api);
    this.publisher = new OwnedPublisher({
      model: host.model,
      purpose: this.request().purpose,
      editingLocked: () => host.editingLocked,
      canCompile: () => host.can("compile"),
      versionsOf: (name) => publishedVersionsOf(host.actionVersions, name),
      reserved: () =>
        reservedActionNames(
          host.catalogContracts,
          host.actionVersions,
          host.model.canvas,
        ),
      sync: () => documents.sync(),
      settled: () => documents.settled(),
      document: (uses) => documents.document(uses),
      problems: () => documents.problems(),
      compile: (document) => client.compile(document),
      publish: (yaml, key) => client.publishAction(yaml, key),
      perform: (edit) => {
        let done = false;
        host.perform(() => {
          edit();
          done = true;
        });
        return done;
      },
      record: (edit) => host.recordOwned(edit),
    });
    void this.publisher
      .prepare()
      .then(() =>
        this.publisher.phase() === "done" ? this.finish() : this.focusPrimary(),
      );
  }
  title(): string {
    const { purpose, workflow } = this.request();
    return purpose === "publish"
      ? `Publish ${workflow.name} ${workflow.version}?`
      : `Publish actions to simulate ${workflow.name}?`;
  }
  message(): string {
    const phase = this.publisher.phase();
    if (phase === "preparing")
      return "Checking the actions of this workflow's steps…";
    if (phase === "blocked")
      return "Studio can't publish these actions yet. Fix what each one says, or close and keep editing.";
    return this.request().purpose === "publish"
      ? "Studio publishes these actions first, then the workflow. Published versions can't change: the next change to one of them makes its next version. Activate the workflow separately to use it in an environment."
      : "A simulation runs against the project catalog, so Studio publishes these actions first; the workflow itself isn't published. Published versions can't change: the next change to one of them makes its next version.";
  }
  listLabel(): string {
    return this.request().purpose === "publish"
      ? "Actions to publish before the workflow"
      : "Actions to publish before the simulation";
  }
  confirmLabel(): string {
    return publishLabel(this.request().purpose, this.publisher.rows());
  }
  /**
   * Checking or publishing: the main button waits. Escape and Close wait
   * only while publishing (see dismiss); during the first "Checking…" they
   * still close the dialog, which is harmless: a check changes nothing, and
   * closing abandons the rest of it.
   */
  waiting(): boolean {
    const phase = this.publisher.phase();
    return phase === "preparing" || phase === "publishing" || phase === "done";
  }
  primaryLabel(): string {
    switch (this.publisher.phase()) {
      case "confirm":
        return this.confirmLabel();
      case "partial":
        return this.retryLabel();
      case "blocked":
        return "Close";
      case "preparing":
        return "Checking…";
      default:
        return "Publishing…";
    }
  }
  primary(): void {
    const phase = this.publisher.phase();
    if (phase === "confirm" || phase === "partial") void this.go();
    else if (phase === "blocked") this.finish();
  }
  secondaryLabel(): string {
    switch (this.publisher.phase()) {
      case "confirm":
        return "Keep editing";
      case "partial":
        return "Close";
      case "blocked":
        return "Check again";
      default:
        return "";
    }
  }
  secondary(): void {
    if (this.publisher.phase() === "blocked") void this.check();
    else this.finish();
  }
  retryLabel(): string {
    return this.publisher.rows().some((row) => row.proposal)
      ? "Publish as suggested"
      : "Try again";
  }
  /** A name in its parts, each kept whole on a line: "order-intake." and "send-email". */
  nameParts(name: string): string[] {
    return name.split(/(?<=\.)/);
  }
  state(row: OwnedRow): { label: string; tone: string } {
    return STATES[row.state];
  }
  usedBy(row: OwnedRow): string {
    const shown = row.steps.slice(0, 3);
    const more = row.steps.length - shown.length;
    if (!shown.length) return "";
    if (more) return `Used by ${shown.join(", ")} and ${more} more`;
    return shown.length === 1
      ? `Used by ${shown[0]}`
      : `Used by ${shown.slice(0, -1).join(", ")} and ${shown.at(-1)}`;
  }
  async go(): Promise<void> {
    if (await this.publisher.publish()) this.finish();
    else this.focusPrimary();
  }
  async check(): Promise<void> {
    // The button pressed goes away while checking: focus waits on the main one.
    this.primaryButton()?.focus();
    await this.publisher.prepare();
    if (this.publisher.phase() === "done") this.finish();
    else this.focusPrimary();
  }
  /** Escape and the close button: never while requests are on their way. */
  dismiss(): void {
    if (!this.publisher.busy()) this.finish();
  }
  finish(): void {
    if (this.finished) return;
    this.finished = true;
    this.publisher.abandon();
    this.closed.emit(this.publisher.outcome());
  }
  /** Closed, or its designer went away: nothing more is sent. */
  ngOnDestroy(): void {
    this.publisher?.abandon();
  }
  private primaryButton(): HTMLButtonElement | null {
    return this.element.nativeElement.querySelector<HTMLButtonElement>(
      ".dialog-footer .primary",
    );
  }
  private focusPrimary(): void {
    if (this.finished) return;
    afterNextRender(() => this.primaryButton()?.focus(), {
      injector: this.injector,
    });
  }
}
