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
// Weave AI's settings in Settings › AI models: on or off, the AI connection
// and the model. Its limits (output tokens, calls, time) keep their saved
// values; a new configuration starts from Weave AI's defaults.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  DoCheck,
  inject,
  input,
  output,
} from "@angular/core";
import { ApiError, type StudioApi } from "../api";
import { describeError } from "../errors";
import { Select } from "../forms/ui/select";
import type { AiConnection } from "../integrations/ai-provider-connection";
import {
  FieldError,
  defaultModel,
  keylessConnection,
  modelOptions,
  weaveAiBody,
  weaveAiChoices,
  CONTEXT_PROFILES,
  type WeaveAiDraft,
} from "../integrations/ai-models-model";
import {
  AiSetupApi,
  type AiModels,
  type LumiConfiguration,
  type LumiStatus,
} from "../integrations/ai-setup-api";

@Component({
  selector: "weave-ai-card",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Select],
  template: `<div class="panel-heading">
      <h2 id="weave-ai-title" tabindex="-1">Weave AI</h2>
      @if (status) {
        <span
          class="pill"
          [attr.data-tone]="status.configured ? 'success' : null"
          >{{
            status.configured ? "Set up · " + status.model : "Not set up"
          }}</span
        >
      }
    </div>
    <p class="card-intro">
      Weave AI answers questions about your workflows and runs, and drafts
      changes for you to review.
    </p>
    @if (!manager()) {
      <p class="empty">
        {{
          canUse()
            ? "Configuring Weave AI requires the Weave AI manager role."
            : "Using Weave AI requires the Weave AI user role."
        }}
      </p>
    } @else if (!canChoose()) {
      <p class="empty">
        Choosing Weave AI's connection also requires the Tenant administrator
        role.
      </p>
    } @else if (loading) {
      <p role="status">Loading Weave AI settings…</p>
    } @else {
      <form
        class="weave-ai-form"
        novalidate
        (submit)="$event.preventDefault(); save()"
      >
        <label class="checkbox-field preference-switch"
          ><input
            type="checkbox"
            role="switch"
            [checked]="draft.enabled"
            [disabled]="busy"
            (change)="draft.enabled = checked($event)"
          />Turn on Weave AI</label
        >
        <div class="weave-ai-fields">
          <weave-select
            data-control
            label="Connection"
            placeholder="Choose an AI connection"
            [options]="choices"
            [value]="chosen?.id ?? ''"
            [disabled]="busy"
            (choose)="chooseConnection($event)"
          />
          <weave-select
            data-control
            label="Model"
            placeholder="Choose a model"
            [options]="modelChoices"
            [value]="draft.model"
            [disabled]="busy || !chosen"
            (choose)="draft.model = $event"
          />
          <weave-select
            data-control
            label="Context size"
            [options]="contextProfiles"
            [value]="draft.contextProfile ?? 'auto'"
            [disabled]="busy"
            (choose)="draft.contextProfile = $any($event)"
          />
        </div>
        @if (connectionNote; as note) {
          <p class="notice" data-tone="info" role="note">{{ note }}</p>
        }
        @if (chosen && local(chosen)) {
          <p class="notice" data-tone="info" role="note">
            Small local models often fail to produce valid changes. Use a larger
            model when you want Weave AI to propose changes.
          </p>
        }
        @if (!usable.length) {
          <p class="notice" data-tone="info" role="note">
            Weave AI needs an AI connection. Connect a provider above first.
          </p>
        }
        @if (error) {
          <p class="field-error" role="alert">{{ error }}</p>
        }
        <div class="card-actions">
          <button
            type="submit"
            class="primary"
            [disabled]="busy || !chosen || !draft.model"
          >
            {{ busy ? "Saving…" : "Save Weave AI settings" }}
          </button>
        </div>
      </form>
    }
    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>`,
  styles: `
    :host {
      display: block;
    }
    .panel-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      min-height: var(--control-md);
      margin-bottom: var(--space-3);
    }
    h2 {
      margin: 0;
      font: var(--type-title-sm);
    }
    .card-intro,
    .empty {
      margin: 0 0 var(--space-4);
      color: var(--muted);
    }
    .weave-ai-form {
      display: grid;
      gap: var(--space-4);
    }
    .weave-ai-fields {
      display: grid;
      gap: var(--space-4);
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .card-actions {
      display: flex;
      justify-content: flex-end;
    }
    /* The dropdowns match the buttons: 36 px, or 44 px for touch. */
    @media (max-width: 767px), (pointer: coarse) {
      .weave-ai-fields weave-select {
        --select-height: var(--control-touch);
      }
    }
  `,
})
export class WeaveAiCard implements DoCheck, OnDestroy {
  api = input.required<StudioApi>();
  /** lumi.manage */
  manager = input(false);
  /** lumi.manage and connection.manage: saving reads the chosen connection. */
  canChoose = input(false);
  /** lumi.use */
  canUse = input(false);
  connections = input<AiConnection[]>([]);
  /** The shell's AI setup generation: the card reloads when it changes. */
  generation = input(0);
  saved = output<void>();
  private readonly cdr = inject(ChangeDetectorRef);
  private setup!: AiSetupApi;
  private replySchema: unknown = null;
  private alive = true;
  loading = true;
  busy = false;
  error = "";
  announcement = "";
  status: LumiStatus | null = null;
  current: LumiConfiguration | null = null;
  models: AiModels | null = null;
  draft: WeaveAiDraft = { enabled: true, connection: "", model: "" };
  readonly contextProfiles = CONTEXT_PROFILES;
  /** The saved connection revision, read when the list may not hold it. */
  pinned: AiConnection | null = null;
  /** The saved connection revision could not be read. */
  pinnedMissing = false;

  /**
   * The connection Save uses: the chosen one, or for a saved revision that
   * is no longer the newest, the newest revision of that connection.
   */
  get chosen(): AiConnection | null {
    const id = this.draft.connection;
    if (!id) return null;
    const listed = this.connections().find((item) => item.id === id);
    if (listed) return listed;
    const newest = this.newest;
    return newest;
  }
  /** The newest revision of the saved connection, when the list has a newer one. */
  private get newest(): AiConnection | null {
    const pinned = this.pinned;
    if (!pinned || pinned.id !== this.draft.connection) return null;
    return (
      this.connections().find(
        (item) => item.name === pinned.name && item.id !== pinned.id,
      ) ?? null
    );
  }
  /** Why the saved connection is not the one shown, when it isn't. */
  /** A local model without a key; measured, small local models seldom write valid changes. */
  readonly local = keylessConnection;
  get connectionNote(): string {
    const id = this.draft.connection;
    if (!id || this.connections().some((item) => item.id === id)) return "";
    const newest = this.newest;
    if (newest && this.pinned)
      return `Weave AI uses revision ${this.pinned.revision} of ${this.pinned.name}; saving moves it to revision ${newest.revision}.`;
    if (this.pinnedMissing || (this.pinned && this.connections().length))
      return "Weave AI's connection is no longer available. Choose a connection, then save.";
    return "";
  }

  get choices() {
    return weaveAiChoices(this.connections());
  }
  get usable() {
    return this.connections();
  }
  get modelChoices() {
    return modelOptions(this.models?.models ?? []);
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  /** The access the card last loaded for; capabilities can arrive after it renders. */
  private loadedFor = "";
  private loads = 0;
  ngDoCheck() {
    const key = `${this.generation()}:${this.canUse()}:${this.manager()}:${this.canChoose()}`;
    if (key !== this.loadedFor) {
      this.loadedFor = key;
      void this.load();
      return;
    }
    // The list of connections can arrive after the card loaded: list the
    // models of the revision Save will use once it is known.
    const target = this.loading ? "" : (this.chosen?.id ?? "");
    if (target && target !== this.modelsFor) void this.loadModels(target);
  }
  private async load() {
    const load = ++this.loads;
    this.setup = new AiSetupApi(this.api());
    this.loading = true;
    try {
      if (this.canUse() || this.manager())
        this.status = await this.setup.lumiStatus().catch(() => null);
      if (this.canChoose()) {
        const [current, reply] = await Promise.all([
          this.setup.lumiConfiguration(),
          this.setup.lumiReplySchema(),
        ]);
        const pinnedId = current?.connection_revision_id ?? "";
        let pinned: AiConnection | null = null;
        let unreadable = "";
        if (pinnedId)
          try {
            pinned = await this.setup.connection(pinnedId);
          } catch (error) {
            // Only a missing revision means it is gone; any other failure is
            // reported as it is, and Save waits for a connection.
            if (!(error instanceof ApiError && error.status === 404))
              unreadable = describeError(error).message;
          }
        if (!this.alive || load !== this.loads) return;
        this.current = current;
        this.pinned = pinned;
        this.pinnedMissing = !!pinnedId && !pinned && !unreadable;
        this.error = unreadable
          ? `Studio couldn't read the connection Weave AI uses. ${unreadable}`
          : "";
        this.replySchema = reply;
        this.draft = {
          enabled: current?.enabled ?? true,
          connection: current?.connection_revision_id ?? "",
          model: String(current?.profile?.["model"] ?? ""),
          contextProfile: current?.context_profile ?? "auto",
        };
        this.modelsFor = "";
        const target = this.chosen;
        if (target) await this.loadModels(target.id);
      }
    } catch (error) {
      if (this.alive && load === this.loads)
        this.error = describeError(error).message;
    } finally {
      if (this.alive && load === this.loads) {
        this.loading = false;
        this.cdr.markForCheck();
      }
    }
  }
  ngOnDestroy() {
    this.alive = false;
  }
  async chooseConnection(id: string) {
    if (id === this.draft.connection) return;
    this.draft = { ...this.draft, connection: id, model: "" };
    await this.loadModels(id);
  }
  /** The connection revision whose models were last asked for. */
  private modelsFor = "";
  private async loadModels(id: string) {
    this.modelsFor = id;
    try {
      const models = await this.setup.models(id);
      if (!this.alive || this.modelsFor !== id) return;
      this.models = models;
      this.draft = {
        ...this.draft,
        model: defaultModel(models.models, this.draft.model),
      };
    } catch (error) {
      if (this.alive) this.error = describeError(error).message;
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  async save() {
    if (this.busy) return;
    const connection = this.chosen;
    if (!connection) {
      this.error = "Choose a connection for Weave AI.";
      return;
    }
    this.error = "";
    let body: Record<string, unknown>;
    try {
      body = weaveAiBody(
        this.draft,
        connection,
        this.current,
        this.replySchema,
      );
    } catch (error) {
      if (error instanceof FieldError) {
        this.error = error.message;
        return;
      }
      throw error;
    }
    this.busy = true;
    try {
      this.current = await this.setup.saveLumi(
        body,
        this.current?.revision ?? null,
      );
      if (!this.alive) return;
      this.status = await this.setup.lumiStatus().catch(() => this.status);
      this.announcement = "Saved Weave AI settings.";
      this.saved.emit();
    } catch (error) {
      if (!this.alive) return;
      this.error =
        error instanceof ApiError && error.code === "WV-LUMI-REVISION"
          ? "Weave AI settings changed since you opened them. Reload the page to see them, then save again."
          : describeError(error).message;
    } finally {
      if (this.alive) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
}
