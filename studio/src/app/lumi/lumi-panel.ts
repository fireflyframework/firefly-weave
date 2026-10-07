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
import { ApiError } from "../api";
import { parse } from "yaml";
import { Select } from "../forms/ui/select";
import { AiProviderConnectionForm } from "../integrations/ai-provider-connection-form";
import {
  aiConnections,
  aiSetupRows,
  type AiConnection,
} from "../integrations/ai-provider-connection";
import { Modal } from "../dialog";
import { AiProfileEditor } from "../integrations/ai-profile-editor";
import { AiSetupWizard } from "../integrations/ai-setup-wizard";
import type { Schema } from "../task-schema";
import { missingData } from "../forms/core/form-model";
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
  imports: [
    Modal,
    AiProfileEditor,
    AiSetupWizard,
    Select,
    AiProviderConnectionForm,
  ],
  template: ` @if (host.lumiOpen) {
    <weave-modal
      heading="Ask Lumi"
      closeLabel="Close Lumi"
      [wide]="true"
      (dismiss)="host.lumiOpen = false"
    >
      <div class="lumi-panel">
        <div class="lumi-tools" [hidden]="settings">
          @if (status?.configured) {
            <span class="hint">{{ status.provider }} · {{ status.model }}</span>
          }
          <button
            type="button"
            (click)="clear()"
            [disabled]="busy || configBusy"
          >
            New conversation
          </button>
          @if (host.profile && host.can("lumi.manage")) {
            <button
              type="button"
              [disabled]="configBusy"
              (click)="openSettings()"
            >
              Lumi settings
            </button>
          }
        </div>
        @if (!host.profile) {
          <p class="hint">
            Connect to a platform to ask Lumi. Your workflow can stay a local
            draft.
          </p>
        } @else if (!host.can("lumi.use")) {
          <p class="hint">
            Ask an administrator for access to Lumi in this environment.
          </p>
        } @else if (!settings && !status?.configured && !loading) {
          <p class="hint">
            Lumi is unavailable in this environment. An administrator configures
            its model and connection; a platform operator enables the Lumi
            gateway. Saving model settings alone does not deploy the gateway.
          </p>
        }
        @if (loading) {
          <p role="status">Loading Lumi…</p>
        }
        @if (settings) {
          <section class="lumi-settings">
            <p class="hint">
              Configure Lumi for this environment. Workflow AI profiles have
              their own settings.
            </p>
            @if (!host.can("connection.manage")) {
              <p role="status">
                To select or save a provider connection, ask an administrator
                for connection.manage in this environment as well as Lumi
                manager.
              </p>
            }
            @if (configSchema) {
              <weave-ai-setup-wizard
                [(step)]="settingsStep"
                [headings]="settingsHeadings"
                progressLabel="Lumi setup progress"
                [canContinue]="
                  settingsStep === 0 ? modelComplete : configComplete
                "
                [busy]="configBusy"
                [navigationBlocked]="newConnection"
                finishLabel="Save Lumi settings"
                (finish)="saveSettings()"
              >
                <fieldset ai-model [disabled]="configBusy">
                  <weave-ai-profile-editor
                    [schema]="configSchema"
                    [initialData]="configInitial"
                    [validateProfile]="validateAiProfile"
                    (dataChange)="config = $event"
                    (validityChange)="configValid = $event"
                  />
                </fieldset>
                <section ai-connection class="settings-connection">
                  <p class="hint">
                    Choose Lumi's provider connection. Credentials stay with the
                    platform operator.
                  </p>
                  <weave-select
                    label="Provider connection"
                    [value]="configConnection"
                    [options]="connectionOptions"
                    [disabled]="configBusy || !host.can('connection.manage')"
                    (choose)="configConnection = $event"
                  />
                  <p class="hint">
                    Lumi pins this exact revision. Choose a connection with the
                    same provider as the profile; changing a workflow profile
                    will not change these settings.
                  </p>
                  @if (configConnection && !selectedConnection) {
                    <p class="field-error">
                      Choose an available connection that matches this provider.
                    </p>
                  }
                  @if (host.can("connection.manage")) {
                    @if (!connectionOptions.length) {
                      <p>
                        No matching provider connections yet. Create one with an
                        operator-approved secret handle, or change the provider.
                      </p>
                    }
                    <div class="action-row">
                      <button
                        type="button"
                        [disabled]="configBusy"
                        (click)="newConnection = !newConnection"
                      >
                        New AI connection</button
                      ><button
                        type="button"
                        [disabled]="configBusy"
                        (click)="loadConnections()"
                      >
                        Refresh connections
                      </button>
                    </div>
                    @if (newConnection) {
                      <weave-ai-provider-connection-form
                        [api]="host.api"
                        [canManage]="host.can('connection.manage')"
                        (created)="connectionCreated($event)"
                        (finished)="newConnection = false"
                      />
                    }
                  }
                </section>
                <section ai-review class="settings-connection">
                  <dl class="settings-summary">
                    @for (row of settingsSummary; track row.label) {
                      <div>
                        <dt>{{ row.label }}</dt>
                        <dd>{{ row.value }}</dd>
                      </div>
                    }
                  </dl>
                  <p class="hint">
                    Saving applies these settings to Lumi in the current
                    environment. It does not change workflow AI profiles or send
                    a model request. The platform operator must enable the Lumi
                    gateway and authorize this connection before you can ask
                    Lumi.
                  </p>
                </section>
                <div ai-error>
                  @if (configError) {
                    <p class="field-error" role="alert">{{ configError }}</p>
                  }
                </div>
                <div ai-secondary class="action-row">
                  <button
                    type="button"
                    [disabled]="configBusy"
                    (click)="back()"
                  >
                    Back to conversation
                  </button>
                </div>
              </weave-ai-setup-wizard>
            } @else if (configBusy) {
              <p role="status">Loading Lumi settings…</p>
            }
            @if (!configSchema && configError) {
              <p class="field-error" role="alert">{{ configError }}</p>
            }
            <div class="action-row">
              <button
                type="button"
                [disabled]="configBusy"
                (click)="openSettings()"
              >
                Reload settings
              </button>
            </div>
          </section>
        } @else if (review; as item) {
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
                The local draft changed after this request. Ask Lumi again with
                the current source.
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
            aria-label="Lumi conversation"
            aria-live="polite"
          >
            @for (turn of conversation.turns; track $index) {
              <article [class.lumi-answer]="turn.role === 'assistant'">
                <h3>{{ turn.role === "user" ? "You" : "Lumi" }}</h3>
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
                  aria-label="Message to Lumi"
                  data-initial-focus
                  maxlength="20000"
                  [value]="message"
                  (input)="message = text($event)"
                ></textarea>
              </label>
              @if (host.model.opened && host.view !== "operations") {
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
              @if (host.view === "operations") {
                <p class="hint">
                  Lumi explains only the saved Operations records you select.
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
          <p role="status">Lumi is thinking…</p>
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
      .settings-connection {
        display: grid;
        gap: 12px;
        min-width: 0;
      }
      .settings-summary {
        margin: 0;
        border-top: 1px solid var(--line);
      }
      .settings-summary div {
        display: grid;
        grid-template-columns: minmax(100px, 1fr) minmax(0, 2fr);
        gap: 16px;
        padding: 12px 0;
        border-bottom: 1px solid var(--line);
      }
      .settings-summary dt {
        color: var(--muted);
      }
      .settings-summary dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
      @media (max-width: 480px) {
        .settings-summary div {
          grid-template-columns: 1fr;
          gap: 4px;
        }
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
      .lumi-settings,
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
  readonly validateAiProfile = (profile: Record<string, unknown>) =>
    this.host.api.validateLlmProfile({ ...profile, outputSchema: {} });
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
    this.settings = false;
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
  settings = false;
  settingsStep = 0;
  readonly settingsHeadings = [
    "Choose Lumi's model",
    "Choose a provider connection",
    "Review Lumi settings",
  ];
  get settingsSummary() {
    const profile = object(this.config["profile"]);
    const connection = this.connections.find(
      (c) => c.id === this.configConnection,
    );
    return [
      {
        label: "Assistant",
        value: this.config["enabled"] ? "Enabled" : "Disabled",
      },
      { label: "Provider", value: profile["provider"] },
      { label: "Model or deployment", value: profile["model"] },
      {
        label: "Connection",
        value: connection
          ? `${connection.name} · revision ${connection.revision}`
          : "Unavailable",
      },
      {
        label: "Endpoint",
        value: connection?.config.endpoint ?? "Unavailable",
      },
      {
        label: "Reasoning",
        value: object(profile["reasoning"])["pattern"] ?? "none",
      },
      { label: "Maximum calls", value: profile["maxCalls"] },
      { label: "Timeout", value: `${profile["timeoutSeconds"]} seconds` },
      {
        label: "Maximum output tokens",
        value: object(profile["options"])["max_tokens"] ?? "Provider default",
      },
    ];
  }
  configBusy = false;
  configError = "";
  configSchema: Schema | null = null;
  configInitial: Record<string, unknown> = {};
  config: Record<string, unknown> = {};
  configValid = true;
  configRevision: number | null = null;
  configConnection = "";
  connections: AiConnection[] = [];
  newConnection = false;
  private replySchema: unknown;
  private wasOpen = false;
  ngDoCheck() {
    const key = JSON.stringify([
      this.host.profile,
      this.host.identity?.principal_id,
      this.host.can("lumi.use"),
      this.host.can("lumi.manage"),
      this.host.view === "operations"
        ? this.host.lumiOperationAttachments.map(({ kind, id }) => [kind, id])
        : null,
    ]);
    if (this.conversation.sync(key)) {
      this.reset();
      this.wasOpen = false;
    }
    if (this.host.lumiOpen && !this.wasOpen) void this.loadStatus();
    this.wasOpen = this.host.lumiOpen;
    if (this.host.lumiOpen && this.host.lumiSettingsRequested) {
      this.host.lumiSettingsRequested = false;
      void this.openSettings();
    }
  }
  private reset() {
    this.message = "";
    this.includeSource = false;
    this.attachments.clear();
    this.review = null;
    this.status = null;
    this.busy = false;
    this.settings = false;
    this.configSchema = null;
    this.config = {};
    this.configInitial = {};
    this.configRevision = null;
    this.configConnection = "";
    this.connections = [];
    this.newConnection = false;
    this.error = "";
    this.loading = false;
    this.configBusy = false;
    this.validating = false;
    this.configError = "";
    this.replySchema = undefined;
  }
  clear() {
    if (this.busy || this.configBusy) return;
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
    if (this.host.view === "operations")
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
    if (this.host.view !== "operations") this.host.flushInspector();
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
    if (this.includeSource && this.host.view !== "operations")
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
        proposals: (this.host.view === "operations" ? [] : reply.proposals).map(
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
    this.host.notify("Applied the reviewed Lumi draft.", {
      label: "Undo",
      run: () => {
        if (
          this.host.model.revision === revision &&
          this.host.model.opened === opened
        )
          this.host.undo();
        else
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
  get connectionOptions() {
    const provider = object(this.config["profile"])["provider"];
    return this.connections
      .filter((c) => c.config.provider === provider)
      .map((c) => ({
        value: c.id,
        label: `${c.name} · revision ${c.revision}`,
        description: c.config.endpoint,
      }));
  }
  get selectedConnection() {
    return this.connectionOptions.some(
      (option) => option.value === this.configConnection,
    );
  }
  async loadConnections() {
    if (!this.host.can("connection.manage")) return;
    const generation = this.conversation.generation;
    try {
      const rows = await aiSetupRows(this.host.api, "connections", true);
      if (generation === this.conversation.generation)
        this.connections = aiConnections(rows);
    } catch (error) {
      if (generation === this.conversation.generation)
        this.configError = describeError(error).message;
    } finally {
      this.cdr.markForCheck();
    }
  }
  connectionCreated(connection: AiConnection) {
    this.connections = [
      ...this.connections.filter((c) => c.id !== connection.id),
      connection,
    ];
    this.configConnection = connection.id;
  }
  get modelComplete() {
    return (
      !!this.configSchema &&
      this.configValid &&
      !missingData(this.configSchema, this.config).length
    );
  }
  get configComplete() {
    return (
      this.host.can("connection.manage") &&
      this.selectedConnection &&
      this.modelComplete
    );
  }
  async openSettings() {
    if (this.configBusy || !this.host.profile || !this.host.can("lumi.manage"))
      return;
    this.keepFocus();
    this.settings = true;
    this.settingsStep = 0;
    this.newConnection = false;
    this.configSchema = null;
    this.keepFocus();
    this.configBusy = true;
    this.configError = "";
    const generation = this.conversation.generation;
    try {
      const schema = await this.host.api.request<Record<string, any>>(
        "/studio/contracts/lumi-configuration",
      );
      const defs = object(schema["$defs"]);
      const profile = object(defs["LumiProfile"]);
      this.replySchema = object(object(profile["properties"])["outputSchema"])[
        "const"
      ];
      if (!this.replySchema)
        throw Error("The host did not provide the fixed Lumi reply schema.");
      const properties = { ...object(profile["properties"]) };
      delete properties["outputSchema"];
      const { connection_revision_id: _, ...configProperties } = object(
        schema["properties"],
      );
      const editable = {
        ...schema,
        properties: configProperties,
        required: (schema["required"] as string[]).filter(
          (key) => key !== "connection_revision_id",
        ),
        $defs: {
          ...defs,
          LumiProfile: {
            ...profile,
            properties,
            required: (profile["required"] as string[]).filter(
              (key) => key !== "outputSchema",
            ),
          },
        },
      };
      let configuration: Record<string, any> = {
        enabled: true,
        profile: {
          options: { max_tokens: 2048 },
          reasoning: { pattern: "none", maxSteps: 6 },
          maxCalls: 8,
          timeoutSeconds: 120,
        },
      };
      try {
        configuration = await this.host.api.request<Record<string, any>>(
          `${this.host.api.environment}/lumi/configuration`,
        );
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) throw error;
      }
      if (generation !== this.conversation.generation) return;
      this.configRevision = configuration["revision"] ?? null;
      const { revision, connection_revision_id, ...data } = configuration;
      this.configConnection = connection_revision_id ?? "";
      const cleanProfile = { ...object(data["profile"]) };
      delete cleanProfile["outputSchema"];
      this.configInitial = { ...data, profile: cleanProfile };
      this.config = structuredClone(this.configInitial);
      this.configSchema = editable as Schema;
      await this.loadConnections();
    } catch (error) {
      if (generation === this.conversation.generation)
        this.configError = describeError(error).message;
    } finally {
      if (generation === this.conversation.generation) {
        this.configBusy = false;
        this.cdr.markForCheck();
      }
    }
  }
  async saveSettings() {
    if (
      this.configBusy ||
      this.settingsStep !== 2 ||
      !this.configComplete ||
      !this.host.can("lumi.manage")
    )
      return;
    this.configBusy = true;
    this.configError = "";
    const generation = this.conversation.generation;
    try {
      const body = {
        ...this.config,
        connection_revision_id: this.configConnection,
        profile: {
          ...object(this.config["profile"]),
          outputSchema: this.replySchema,
        },
      };
      const result = await this.host.api.request<Record<string, any>>(
        `${this.host.api.environment}/lumi/configuration`,
        "PUT",
        body,
        this.configRevision === null
          ? {}
          : { "If-Match": `"${this.configRevision}"` },
      );
      if (generation !== this.conversation.generation) return;
      this.configRevision = result["revision"];
      this.settings = false;
      await this.loadStatus();
    } catch (error) {
      if (generation === this.conversation.generation)
        this.configError = describeError(error).message;
    } finally {
      if (generation === this.conversation.generation) {
        this.configBusy = false;
        this.cdr.markForCheck();
      }
    }
  }
}
