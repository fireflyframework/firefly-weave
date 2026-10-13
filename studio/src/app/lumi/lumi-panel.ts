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
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  ElementRef,
  Input,
  inject,
} from "@angular/core";
import type { App } from "../app";
import { parse } from "yaml";
import { Modal } from "../dialog";
import { describeError } from "../errors";
import { exportFile } from "../export-file";
import {
  LumiConversation,
  unchangedDraft,
  type DraftRevision,
  type LumiProposal,
  type ProposalReview,
} from "./lumi-state";

const object = (value: unknown): Record<string, any> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, any>)
    : {};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
@Component({
  selector: "weave-lumi-panel",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Modal],
  template: ` @if (host.lumiOpen) {
    <weave-modal
      heading="Ask Weave AI"
      closeLabel="Close Weave AI"
      [wide]="true"
      (dismiss)="host.lumiOpen = false"
    >
      <div class="lumi-panel">
        <div class="lumi-tools">
          @if (status?.configured) {
            <span class="hint">{{ status.provider }} · {{ status.model }}</span>
          }
          <button type="button" (click)="clear()" [disabled]="busy">
            New conversation
          </button>
          @if (host.profile && host.can("lumi.manage")) {
            <button type="button" (click)="host.openAiModels()">
              Set up Weave AI
            </button>
          }
        </div>
        @if (!host.profile) {
          <p class="hint">
            Connect to a platform to ask Weave AI. Your workflow can stay a
            local draft.
          </p>
        } @else if (!host.can("lumi.use")) {
          <p class="hint">
            Ask an administrator for access to Weave AI in this environment.
          </p>
        } @else if (!status?.configured && !loading) {
          <p class="hint">
            Weave AI is not set up in this environment. A Weave AI manager sets
            it up in Settings › AI models.
          </p>
        }
        @if (loading) {
          <p role="status">Loading Weave AI…</p>
        }
        @if (review; as item) {
          <section class="lumi-review">
            <h3>{{ item.title }}</h3>
            <p class="hint">
              Review this {{ item.kind }} draft before using it.
            </p>
            @if (item.kind === "workflow") {
              <details>
                <summary>Current local source</summary>
                <pre>{{ item.base.buffer || item.base.source }}</pre>
              </details>
            }
            <label
              >Proposed source<textarea
                class="lumi-source"
                aria-label="Proposed source"
                spellcheck="false"
                [value]="item.source"
                (input)="editProposal($event)"
              ></textarea>
            </label>
            @if (item.problems.length) {
              <ul class="field-error" role="alert">
                @for (problem of item.problems; track $index) {
                  <li>{{ problem }}</li>
                }
              </ul>
            }
            @if (item.validated === item.source) {
              <p role="status">The proposal passed local validation.</p>
            }
            @if (item.kind === "workflow" && !current(item)) {
              <p class="field-error" role="alert">
                The local draft changed after this request. Ask Weave AI again
                with the current source.
              </p>
            }
            <div class="action-row">
              <button
                type="button"
                [disabled]="validating"
                (click)="validateProposal()"
              >
                {{ validating ? "Validating…" : "Validate proposal" }}
              </button>
              @if (item.kind === "workflow") {
                <button
                  type="button"
                  class="primary"
                  [disabled]="
                    item.validated !== item.source ||
                    !current(item) ||
                    host.editingLocked
                  "
                  (click)="applyProposal()"
                >
                  Apply to local draft
                </button>
              } @else {
                <button
                  type="button"
                  [disabled]="item.validated !== item.source"
                  (click)="saveProposal()"
                >
                  Save reviewed draft file
                </button>
              }
              <button type="button" (click)="back()">
                Back to conversation
              </button>
            </div>
          </section>
        } @else {
          <div
            class="lumi-conversation"
            role="log"
            aria-label="Weave AI conversation"
            aria-live="polite"
          >
            @for (turn of conversation.turns; track $index) {
              <article [class.lumi-answer]="turn.role === 'assistant'">
                <h3>{{ turn.role === "user" ? "You" : "Weave AI" }}</h3>
                <p class="lumi-text">{{ turn.content }}</p>
                @for (proposal of turn.proposals ?? []; track $index) {
                  <button type="button" (click)="openReview(proposal)">
                    {{ proposal.title }}
                  </button>
                }
                @for (followUp of turn.followUps ?? []; track $index) {
                  <button
                    type="button"
                    class="text-link"
                    (click)="message = followUp"
                  >
                    {{ followUp }}
                  </button>
                }
              </article>
            }
          </div>
          <form novalidate (submit)="$event.preventDefault(); send()">
            <fieldset
              [disabled]="busy || !status?.configured || !host.can('lumi.use')"
            >
              <label
                >Message<textarea
                  aria-label="Message to Weave AI"
                  data-initial-focus
                  maxlength="20000"
                  [value]="message"
                  (input)="message = text($event)"
                ></textarea>
              </label>
              @if (host.model.opened && host.view !== "clusters") {
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="includeSource"
                    (change)="includeSource = checked($event)"
                  />Include current source</label
                >
              }
              @for (
                attachment of availableAttachments;
                track attachment.kind + attachment.id
              ) {
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="attachments.has(attachment.kind + attachment.id)"
                    [disabled]="
                      attachments.size >= 4 &&
                      !attachments.has(attachment.kind + attachment.id)
                    "
                    (change)="
                      toggleAttachment(
                        attachment.kind + attachment.id,
                        checked($event)
                      )
                    "
                  />{{ attachment.label }}</label
                >
              }
              <p class="hint">
                The selected provider receives your message and any source or
                context you include. This conversation stays in memory for this
                identity and environment.
              </p>
              @if (host.view === "clusters") {
                <p class="hint">
                  Weave AI explains only the saved Clusters records you select.
                  Replica and resource limits do not show worker task capacity
                  or current cloud state. No deployment changes are made.
                </p>
              }
              <button
                type="submit"
                class="primary"
                [disabled]="!message.trim()"
              >
                Send message
              </button>
            </fieldset>
          </form>
        }
        @if (busy) {
          <p role="status">Weave AI is thinking…</p>
        }
        @if (error) {
          <p class="field-error" role="alert">{{ error }}</p>
        }
      </div>
    </weave-modal>
  }`,
  styles: [
    `
      .lumi-panel {
        display: grid;
        gap: 16px;
        min-width: 0;
      }
      .lumi-tools,
      .action-row {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        align-items: center;
      }
      [hidden] {
        display: none !important;
      }
      .lumi-conversation {
        display: grid;
        gap: 16px;
      }
      .lumi-conversation article {
        padding: 12px;
        border: 1px solid var(--line);
        border-radius: 8px;
        min-width: 0;
      }
      .lumi-conversation h3 {
        margin: 0 0 8px;
      }
      .lumi-text,
      pre {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
      }
      .lumi-conversation button {
        max-width: 100%;
        white-space: normal;
      }
      .lumi-review,
      fieldset {
        display: grid;
        gap: 12px;
        min-width: 0;
      }
      fieldset {
        border: 0;
        padding: 0;
        margin: 0;
      }
      label {
        display: grid;
        gap: 6px;
      }
      .checkbox-field {
        display: flex;
        gap: 8px;
        align-items: center;
      }
      textarea {
        width: 100%;
        box-sizing: border-box;
        min-height: 88px;
        resize: vertical;
      }
      .lumi-source {
        min-height: 220px;
        font-family: var(--mono);
      }
      .hint {
        margin: 0;
      }
    `,
  ],
})
export class LumiPanel implements DoCheck {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  private keepFocus() {
    this.element.nativeElement
      .querySelector<HTMLElement>(".modal-panel")
      ?.focus({ preventScroll: true });
  }
  openReview(proposal: ProposalReview) {
    this.keepFocus();
    this.review = proposal;
  }
  back() {
    this.keepFocus();
    this.review = null;
  }
  conversation = new LumiConversation();
  message = "";
  includeSource = false;
  attachments = new Set<string>();
  busy = false;
  loading = false;
  error = "";
  status: { configured: boolean; provider?: string; model?: string } | null =
    null;
  review: ProposalReview | null = null;
  validating = false;
  private wasOpen = false;
  ngDoCheck() {
    const key = JSON.stringify([
      this.host.profile,
      this.host.identity?.principal_id,
      this.host.can("lumi.use"),
      this.host.can("lumi.manage"),
      this.host.view === "clusters"
        ? this.host.lumiOperationAttachments.map(({ kind, id }) => [kind, id])
        : null,
    ]);
    if (this.conversation.sync(key)) {
      this.reset();
      this.wasOpen = false;
    }
    if (this.host.lumiOpen && !this.wasOpen) void this.loadStatus();
    this.wasOpen = this.host.lumiOpen;
  }
  private reset() {
    this.message = "";
    this.includeSource = false;
    this.attachments.clear();
    this.review = null;
    this.status = null;
    this.busy = false;
    this.error = "";
    this.loading = false;
    this.validating = false;
  }
  clear() {
    if (this.busy) return;
    this.conversation.clear();
    this.message = "";
    this.review = null;
    this.attachments.clear();
    this.includeSource = false;
    this.error = "";
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  capture(): DraftRevision {
    return {
      opened: this.host.model.opened,
      revision: this.host.model.revision,
      source: this.host.model.source,
      buffer: this.host.sourceBuffer,
    };
  }
  current(proposal: ProposalReview) {
    return unchangedDraft(proposal.base, this.capture());
  }
  get availableAttachments() {
    if (this.host.view === "clusters")
      return this.host.lumiOperationAttachments;
    const items: {
      kind: "draft" | "run" | "simulation";
      id: string;
      label: string;
    }[] = [];
    if (this.host.draftRevision !== undefined && uuid.test(this.host.draftId))
      items.push({
        kind: "draft",
        id: this.host.draftId,
        label: "Include saved platform draft",
      });
    const run =
      this.host.view === "runs"
        ? String(this.host.selectedRecord?.["id"] ?? "")
        : "";
    if (uuid.test(run))
      items.push({ kind: "run", id: run, label: "Include selected run" });
    const simulation = this.host.simulationSession?.id;
    if (simulation && uuid.test(simulation))
      items.push({
        kind: "simulation",
        id: simulation,
        label: "Include current simulation",
      });
    return items;
  }
  toggleAttachment(key: string, checked: boolean) {
    checked ? this.attachments.add(key) : this.attachments.delete(key);
  }
  async loadStatus() {
    if (!this.host.profile || !this.host.can("lumi.use")) return;
    const generation = this.conversation.generation;
    this.loading = true;
    try {
      const status = await this.host.api.request<any>(
        `${this.host.api.environment}/lumi/status`,
      );
      if (generation === this.conversation.generation) this.status = status;
    } catch (error) {
      if (generation === this.conversation.generation)
        this.error = describeError(error).message;
    } finally {
      if (generation === this.conversation.generation) {
        this.loading = false;
        this.cdr.markForCheck();
      }
    }
  }
  async send() {
    if (
      this.busy ||
      !this.message.trim() ||
      !this.status?.configured ||
      !this.host.can("lumi.use")
    )
      return;
    this.keepFocus();
    if (this.host.view !== "clusters") this.host.flushInspector();
    const base = this.capture();
    const generation = this.conversation.generation;
    const message = this.message.trim();
    const body: Record<string, unknown> = {
      message,
      history: this.conversation.history(),
      attachments: this.availableAttachments
        .filter((item) => this.attachments.has(item.kind + item.id))
        .map(({ kind, id }) => ({ kind, id })),
    };
    if (this.includeSource && this.host.view !== "clusters")
      body["draft"] = {
        format: this.host.model.format,
        source: this.host.sourceBuffer || this.host.model.source,
      };
    this.conversation.turns.push({ role: "user", content: message });
    this.message = "";
    this.busy = true;
    this.error = "";
    try {
      const reply = await this.host.api.request<{
        answer: string;
        proposals: LumiProposal[];
        followUps: string[];
      }>(`${this.host.api.environment}/lumi/ask`, "POST", body, {}, 610000);
      if (generation !== this.conversation.generation) return;
      this.conversation.turns.push({
        role: "assistant",
        content: reply.answer,
        proposals: (this.host.view === "clusters" ? [] : reply.proposals).map(
          (proposal) => ({
            ...proposal,
            base,
            validated: null,
            problems: [],
          }),
        ),
        followUps: reply.followUps,
      });
    } catch (error) {
      if (generation === this.conversation.generation)
        this.error = describeError(error).message;
    } finally {
      if (generation === this.conversation.generation) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
  editProposal(event: Event) {
    if (!this.review) return;
    this.review.source = this.text(event);
    this.review.validated = null;
    this.review.problems = [];
  }
  async validateProposal() {
    const item = this.review;
    if (!item) return;
    const generation = this.conversation.generation;
    const source = item.source;
    this.keepFocus();
    this.validating = true;
    try {
      const expected = {
        workflow: "Workflow",
        decisionTable: "DecisionTable",
        action: "Action",
        connector: "Connector",
      }[item.kind];
      if (object(parse(source))["kind"] !== expected)
        throw Error(`This proposal must contain a ${expected} document.`);
      const result = await this.host.api.validate(source, item.format);
      if (generation !== this.conversation.generation || item.source !== source)
        return;
      item.problems = result.diagnostics
        .filter((problem) => problem.severity === "error")
        .map((problem) => problem.message);
      item.validated =
        result.validationOk && !result.errorCount ? source : null;
      if (!item.validated && !item.problems.length)
        item.problems = ["The proposal did not pass validation."];
    } catch (error) {
      if (generation === this.conversation.generation)
        item.problems = [describeError(error).message];
    } finally {
      if (generation === this.conversation.generation) {
        this.validating = false;
        this.cdr.markForCheck();
      }
    }
  }
  applyProposal() {
    const item = this.review;
    if (
      !item ||
      item.kind !== "workflow" ||
      item.validated !== item.source ||
      !this.current(item) ||
      this.host.editingLocked
    )
      return;
    this.host.perform(() =>
      this.host.model.setSource(item.source, item.format),
    );
    if (this.host.error) {
      this.error = this.host.error;
      return;
    }
    this.host.lumiOpen = false;
    this.host.navigate("designer");
    const revision = this.host.model.revision;
    const opened = this.host.model.opened;
    this.host.notify("Applied the reviewed Weave AI draft.", {
      label: "Undo",
      run: async () => {
        const done = await this.host.undoIfUnchanged(
          () =>
            this.host.model.revision === revision &&
            this.host.model.opened === opened,
        );
        if (done === "changed")
          this.host.notify(
            "The workflow changed since. Use Undo in the toolbar.",
          );
      },
    });
  }
  saveProposal() {
    const item = this.review;
    if (!item || item.validated !== item.source) return;
    exportFile(
      `lumi-${item.kind}.${item.format === "json" ? "json" : "yaml"}`,
      item.format === "json" ? "application/json" : "application/yaml",
      item.source,
    );
  }
}
