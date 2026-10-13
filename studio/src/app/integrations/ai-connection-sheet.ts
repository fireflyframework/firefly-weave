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
// The one form for AI connections, a side sheet that opens over any view:
// Settings › AI models, Build › Connections, or an AI step's connection row.
// It saves an immutable connection revision, then lists the server's models
// and tests one through the AI gateway. It asks for a secret handle, never for
// a key, and offers plain HTTP only through the platform's approved endpoints.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  Injector,
  OnDestroy,
  OnInit,
  afterNextRender,
  inject,
  input,
  output,
} from "@angular/core";
import { ApiError, type StudioApi } from "../api";
import { DialogService, Modal } from "../dialog";
import { describeError } from "../errors";
import { Icon } from "../icon";
import { Select } from "../forms/ui/select";
import type { AiConnection } from "./ai-provider-connection";
import {
  AiSetupApi,
  type AiEndpoint,
  type AiModels,
  type AiTestResult,
  type ConnectionRequestBody,
} from "./ai-setup-api";
import {
  API_STYLES,
  FieldError,
  PROVIDER_CARDS,
  aiMessage,
  connectionRequest,
  defaultModel,
  draftFor,
  draftKey,
  endpointFor,
  keyless,
  modelOptions,
  originOf,
  presetsFor,
  refusal,
  secretSetCommand,
  sheetProblems,
  testView,
  type ProviderCard,
  type SheetDraft,
  type SheetField,
  type SheetProblem,
  type TestState,
} from "./ai-models-model";

/** What the shell asks the sheet to show. */
export interface AiSheetRequest {
  card?: ProviderCard;
  connection?: AiConnection | null;
  /** Start at the model and its test (a saved connection). */
  focus?: "model";
}
/** One test result, kept by the shell for this Studio session only. */
export interface AiTestRecord {
  revision: string;
  result: AiTestResult;
  at: number;
}

@Component({
  selector: "weave-ai-connection-sheet",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Modal, Icon, Select],
  styleUrl: "./ai-connection-sheet.css",
  template: `<weave-modal
    [heading]="heading()"
    placement="side"
    closeLabel="Close AI connection"
    (dismiss)="close()"
  >
    <form
      class="ai-sheet"
      novalidate
      (submit)="$event.preventDefault(); save()"
    >
      @if (!canManage()) {
        <p class="notice" data-tone="neutral" role="note">
          Creating AI connections requires the tenant administrator role.
        </p>
      }
      @if (loading) {
        <p role="status">Loading approved endpoints…</p>
      } @else if (policy === "absent") {
        <p class="notice" role="note">
          This platform does not publish its approved models. The worker refuses
          models it has not approved.
        </p>
      }
      <fieldset
        class="sheet-fields"
        [disabled]="!canManage() || busy || loading"
      >
        <legend class="sr-only">Connection</legend>
        @if (!saved) {
          <fieldset class="choice-group">
            <legend>Provider</legend>
            @for (card of cards; track card.id) {
              <label class="choice-row">
                <input
                  type="radio"
                  name="ai-provider"
                  [checked]="draft.card === card.id"
                  (change)="chooseCard(card.id)"
                />
                <span class="choice-text">{{ card.title }}</span>
                <span class="choice-url">{{ card.kind }}</span>
              </label>
            }
          </fieldset>
        }
        <fieldset
          class="choice-group"
          [attr.aria-describedby]="
            problem('endpoint') ? 'ai-endpoint-error' : null
          "
        >
          <legend>Endpoint</legend>
          @for (preset of presets; track preset.id) {
            <label class="choice-row">
              <input
                type="radio"
                name="ai-endpoint"
                [checked]="!draft.custom && same(preset)"
                (change)="usePreset(preset)"
              />
              <span class="choice-text"
                ><span>{{ preset.label }}</span
                ><span class="choice-url">{{ preset.url }}</span></span
              >
              @if (preset.development_only) {
                <span class="pill" data-tone="neutral">Development only</span>
              }
            </label>
          }
          @if (customAllowed) {
            <label class="choice-row">
              <input
                type="radio"
                name="ai-endpoint"
                [checked]="draft.custom"
                (change)="draft.custom = true"
              />
              <span class="choice-text">{{
                draft.card === "ollama"
                  ? "Custom address…"
                  : "Use a compatible endpoint"
              }}</span>
            </label>
          }
        </fieldset>
        @if (draft.custom) {
          <div class="field">
            <label for="ai-endpoint-url">Endpoint address</label>
            <input
              id="ai-endpoint-url"
              type="url"
              autocomplete="off"
              spellcheck="false"
              [value]="draft.endpoint"
              (input)="draft.endpoint = value($event)"
              [attr.aria-invalid]="problem('endpoint') ? 'true' : null"
              aria-describedby="ai-endpoint-help"
            />
            <p class="field-help" id="ai-endpoint-help">
              {{
                draft.card === "azure"
                  ? "Your Azure OpenAI resource, for example https://NAME.openai.azure.com."
                  : "An https:// address. Plain http:// works only for addresses a platform operator approved."
              }}
            </p>
          </div>
        }
        @if (problem("endpoint"); as message) {
          <p class="field-error" id="ai-endpoint-error" role="alert">
            {{ message }}
          </p>
        }
        @if (draft.card === "openai" || draft.card === "azure") {
          <fieldset class="choice-group">
            <legend>API style</legend>
            @for (style of styles; track style.value) {
              <label class="choice-row">
                <input
                  type="radio"
                  name="ai-style"
                  [checked]="draft.provider === style.value"
                  (change)="draft.provider = style.value"
                />
                <span class="choice-text">{{ style.label }}</span>
              </label>
            }
          </fieldset>
        }
        @if (draft.card === "azure") {
          <div class="field">
            <label for="ai-api-version">API version</label>
            <input
              id="ai-api-version"
              autocomplete="off"
              [value]="draft.apiVersion"
              (input)="draft.apiVersion = value($event)"
              [attr.aria-invalid]="problem('apiVersion') ? 'true' : null"
              placeholder="For example, 2025-04-01-preview"
            />
            @if (problem("apiVersion"); as message) {
              <p class="field-error" role="alert">{{ message }}</p>
            }
          </div>
        }
        <div class="field">
          <label for="ai-connection-name">Connection name</label>
          <input
            id="ai-connection-name"
            autocomplete="off"
            spellcheck="false"
            [value]="draft.name"
            (input)="draft.name = value($event)"
            [attr.aria-invalid]="problem('name') ? 'true' : null"
            aria-describedby="ai-name-help"
          />
          <p class="field-help" id="ai-name-help">
            Letters, numbers, dots, dashes or underscores.
          </p>
          @if (problem("name"); as message) {
            <p class="field-error" role="alert">{{ message }}</p>
          }
        </div>
        @if (noKey) {
          <p class="notice" data-tone="info" role="note">
            Local endpoints use no credential. Weave never sends credentials to
            a model over plain HTTP.
          </p>
        } @else if (canManage()) {
          <div class="field">
            <label for="ai-secret-handle">Secret handle</label>
            <input
              id="ai-secret-handle"
              autocomplete="off"
              spellcheck="false"
              [value]="draft.handle"
              (input)="draft.handle = value($event)"
              [attr.aria-invalid]="problem('handle') ? 'true' : null"
              aria-describedby="ai-secret-help"
            />
            <p class="field-help" id="ai-secret-help">
              Weave stores a reference to your key, never the key. Store the key
              on the platform:
            </p>
            <div class="command">
              <!-- A pasted key is never echoed: anything that isn't a handle shows HANDLE. -->
              <code>{{ secretCommand }}</code>
              <button
                type="button"
                class="tertiary"
                (click)="copy(secretCommand)"
              >
                Copy
              </button>
            </div>
            <p class="field-help">
              Then run <code>weave platform start</code>. On a shared platform,
              ask a platform operator to grant this secret handle.
            </p>
            @if (problem("handle"); as message) {
              <p class="field-error" role="alert">{{ message }}</p>
            }
          </div>
        }
      </fieldset>
      @if (saved) {
        <section class="sheet-section">
          <h3 id="ai-model-title">Test a model</h3>
          <p class="field-help">
            Choose a model to test. AI steps and Weave AI choose their own
            model.
          </p>
          @if (draft.card === "azure") {
            <div class="field">
              <label for="ai-deployment">Deployment name</label>
              <input
                id="ai-deployment"
                autocomplete="off"
                [value]="model"
                (input)="model = value($event)"
                [disabled]="testing"
              />
              <p class="field-help">
                Use the deployment name, not the model family.
              </p>
            </div>
          } @else {
            <div class="model-row">
              <weave-select
                data-control
                label="Model"
                [options]="modelChoices"
                [value]="model"
                placeholder="Choose a model"
                [disabled]="testing || !modelChoices.length"
                (choose)="model = $event"
              />
              @if (canManage()) {
                <button
                  type="button"
                  class="icon-button"
                  aria-label="Refresh models"
                  [disabled]="testing"
                  (click)="loadModels(true)"
                >
                  <weave-icon name="refresh" />
                </button>
              }
            </div>
            @if (models && !models.models.length) {
              <p class="field-help">No models on this server yet. Pull one:</p>
              <div class="command">
                <code>ollama pull qwen3:4b</code>
                <button
                  type="button"
                  class="tertiary"
                  (click)="copy('ollama pull qwen3:4b')"
                >
                  Copy
                </button>
              </div>
            }
            @if (modelsNote) {
              <p class="field-help" role="status">{{ modelsNote }}</p>
            }
          }
          @if (canManage()) {
            <div class="notice test-status" [attr.data-tone]="view.tone">
              @if (testing) {
                <weave-icon name="spinner" [size]="16" />
              }
              <!-- Only the result is announced, not the Cancel button. -->
              <span role="status" aria-live="polite">{{ view.text }}</span>
              @if (testing) {
                <button
                  type="button"
                  aria-label="Cancel test"
                  (click)="cancelTest()"
                >
                  Cancel
                </button>
              }
            </div>
            @if (view.details.length) {
              <details class="test-details">
                <summary>Show details</summary>
                <dl>
                  @for (detail of view.details; track detail[0]) {
                    <dt>{{ detail[0] }}</dt>
                    <dd>{{ detail[1] }}</dd>
                  }
                </dl>
              </details>
            }
          }
        </section>
      }
      @if (problem("form") || error; as message) {
        <p class="field-error" role="alert">
          {{ message }}
          @if (errorCode) {
            <small class="support-code">Support code: {{ errorCode }}</small>
          }
        </p>
      }
      <footer class="dialog-footer">
        @if (canManage()) {
          <button
            type="button"
            data-test-connection
            [disabled]="!canTest"
            (click)="runTest()"
          >
            Test connection
          </button>
          <button
            type="submit"
            class="primary"
            [disabled]="!dirty || busy || loading"
          >
            {{
              busy
                ? "Saving…"
                : saved
                  ? "Save as a new revision"
                  : "Save connection"
            }}
          </button>
        } @else {
          <button type="button" (click)="close()">Close</button>
        }
      </footer>
      <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>
    </form>
  </weave-modal>`,
})
export class AiConnectionSheet implements OnInit, OnDestroy {
  api = input.required<StudioApi>();
  canManage = input.required<boolean>();
  request = input.required<AiSheetRequest>();
  connectionSaved = output<AiConnection>();
  tested = output<AiTestRecord>();
  closed = output<void>();
  readonly cards = PROVIDER_CARDS;
  private readonly dialogs = inject(DialogService);
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private setup!: AiSetupApi;
  policy: "loaded" | "absent" | null = null;
  endpoints: AiEndpoint[] = [];
  connectorId = "";
  draft: SheetDraft = draftFor("ollama", []);
  /** The saved revision this sheet shows, or null before the first save. */
  saved: AiConnection | null = null;
  models: AiModels | null = null;
  model = "";
  modelsNote = "";
  test: TestState = { kind: "idle" };
  loading = true;
  busy = false;
  error = "";
  errorCode = "";
  announcement = "";
  problems: SheetProblem[] = [];
  /** The draft as saved (or as opened); differences are unsaved changes. */
  private opened = "";
  private controller: AbortController | null = null;
  private attempt: { body: ConnectionRequestBody; key: string } | null = null;
  private alive = true;

  heading() {
    return (
      this.saved?.name ?? this.request().connection?.name ?? "New AI connection"
    );
  }
  get presets() {
    return presetsFor(this.draft.card, this.endpoints);
  }
  get styles() {
    return API_STYLES[this.draft.card === "azure" ? "azure" : "openai"];
  }
  get customAllowed() {
    return (
      this.draft.card === "ollama" ||
      this.policy === "absent" ||
      !this.presets.length
    );
  }
  get noKey() {
    return keyless(this.draft, this.endpoints);
  }
  get secretCommand() {
    return secretSetCommand(this.draft.handle);
  }
  get changed() {
    return draftKey(this.draft) !== this.opened;
  }
  get dirty() {
    return !this.saved || this.changed;
  }
  get testing() {
    return this.test.kind === "running";
  }
  get canTest() {
    return (
      this.canManage() &&
      !!this.saved &&
      !this.changed &&
      !!this.model.trim() &&
      !this.testing &&
      !this.busy
    );
  }
  get view() {
    return testView(this.test, this.saved?.name ?? this.draft.name.trim());
  }
  get modelChoices() {
    return modelOptions(this.models?.models ?? []);
  }
  problem(field: SheetField) {
    return this.problems.find((item) => item.field === field)?.message ?? "";
  }
  same(preset: AiEndpoint) {
    return endpointFor(this.draft.endpoint, [preset]) !== undefined;
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }

  async ngOnInit() {
    this.setup = new AiSetupApi(this.api());
    const request = this.request();
    this.saved = request.connection ?? null;
    const card = request.card ?? "ollama";
    this.draft = draftFor(card, [], this.saved);
    try {
      const [endpoints, connectorId] = await Promise.all([
        this.setup.endpoints(),
        this.canManage() && !this.saved
          ? this.setup.connectorId()
          : Promise.resolve(""),
      ]);
      if (!this.alive) return;
      this.policy = endpoints.policy;
      this.endpoints = endpoints.endpoints ?? [];
      this.connectorId = connectorId;
      this.draft = draftFor(card, this.endpoints, this.saved);
      this.opened = draftKey(this.draft);
      if (this.saved) await this.loadModels(false);
      if (this.alive && this.saved && request.focus === "model")
        afterNextRender(
          () =>
            this.element.nativeElement
              .querySelector<HTMLElement>("#ai-model-title")
              ?.scrollIntoView({ block: "nearest" }),
          { injector: this.injector },
        );
    } catch (error) {
      if (this.alive) this.fail(error);
    } finally {
      if (this.alive) {
        this.loading = false;
        this.cdr.markForCheck();
        // The fields were disabled while loading, so the panel held focus.
        this.focusSoon(
          this.saved && request.focus === "model"
            ? "#ai-deployment, .model-row [role='combobox'], [data-test-connection]"
            : ".sheet-fields input:checked, .sheet-fields input:not(:disabled)",
        );
      }
    }
  }
  ngOnDestroy() {
    this.alive = false;
    this.cancelTest();
  }
  chooseCard(card: ProviderCard) {
    if (card === this.draft.card) return;
    this.draft = draftFor(card, this.endpoints);
    this.problems = [];
  }
  usePreset(preset: AiEndpoint) {
    this.draft = { ...this.draft, endpoint: preset.url, custom: false };
    this.problems = this.problems.filter((item) => item.field !== "endpoint");
  }
  async save() {
    if (this.busy || this.loading || !this.canManage() || !this.dirty) return;
    this.error = this.errorCode = "";
    this.problems = [];
    if (!this.connectorId && this.saved)
      this.connectorId = await this.setup.connectorId().catch(() => "");
    let body: ConnectionRequestBody;
    try {
      body = connectionRequest(
        this.draft,
        this.connectorId,
        this.endpoints,
        this.policy ?? "absent",
      );
    } catch (error) {
      if (!(error instanceof FieldError)) throw error;
      this.problems = [{ field: error.field, message: error.message }];
      return;
    }
    // A retry of an uncertain save sends the same request with the same key, so it never adds a second revision.
    if (
      !this.attempt ||
      JSON.stringify(this.attempt.body) !== JSON.stringify(body)
    )
      this.attempt = { body, key: crypto.randomUUID() };
    this.busy = true;
    try {
      const connection = await this.setup.create(
        this.attempt.body,
        this.attempt.key,
      );
      if (!this.alive) return;
      this.attempt = null;
      this.saved = connection;
      this.opened = draftKey(this.draft);
      this.test = { kind: "idle" };
      this.announcement = `Saved ${connection.name}, revision ${connection.revision}.`;
      this.connectionSaved.emit(connection);
      await this.loadModels(false);
      // Saving disabled the focused Save button: continue at the model.
      this.focusSoon(
        "#ai-deployment, .model-row [role='combobox'], [data-test-connection]",
      );
    } catch (error) {
      if (!this.alive) return;
      if (error instanceof ApiError && error.code === "WV-IDEMPOTENCY-CONFLICT")
        this.attempt = null;
      this.problems = sheetProblems(
        error instanceof ApiError ? error.detail : null,
      );
      if (!this.problems.length) this.fail(error);
    } finally {
      if (this.alive) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
  async loadModels(refresh: boolean) {
    const saved = this.saved;
    if (!saved) return;
    this.modelsNote = "Loading models…";
    this.cdr.markForCheck();
    try {
      let models = await this.setup.models(
        saved.id,
        refresh && this.canManage(),
      );
      if (!refresh && models.discovery === "unknown" && this.canManage())
        models = await this.setup.models(saved.id, true);
      if (!this.alive || this.saved !== saved) return;
      this.models = models;
      this.model =
        this.draft.card === "azure"
          ? this.model
          : defaultModel(models.models, this.model);
      this.modelsNote =
        models.discovery === "ok" || models.discovery === "unknown"
          ? ""
          : aiMessage(models.discovery, {
              endpoint: originOf(saved.config.endpoint),
            });
    } catch (error) {
      if (this.alive) this.modelsNote = describeError(error).message;
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  async runTest() {
    const saved = this.saved;
    const model = this.model.trim();
    if (!this.canTest || !saved) return;
    const controller = new AbortController();
    this.controller = controller;
    const origin = originOf(saved.config.endpoint);
    this.test = { kind: "running", model };
    // The disabled Test button can't keep focus; Cancel is the next thing to press.
    this.focusSoon(".test-status button", true);
    try {
      const result = await this.setup.test(saved.id, model, controller.signal);
      if (!this.alive || controller.signal.aborted) return;
      this.test = { kind: "done", result, origin };
      this.tested.emit({ revision: saved.id, result, at: Date.now() });
    } catch (error) {
      if (!this.alive) return;
      this.test = controller.signal.aborted
        ? { kind: "canceled" }
        : refusal(error, origin);
    } finally {
      if (this.controller === controller) this.controller = null;
      if (this.alive) {
        this.cdr.markForCheck();
        this.focusSoon("[data-test-connection]");
      }
    }
  }
  /**
   * Focuses a control of the sheet once it is rendered, when focus was lost
   * (its control was disabled or removed) or `always` is set and focus is
   * still in the sheet.
   */
  private focusSoon(selector: string, always = false) {
    afterNextRender(
      () => {
        const sheet =
          this.element.nativeElement.querySelector<HTMLElement>(".modal-panel");
        const active = document.activeElement;
        const lost = !active || active === document.body || active === sheet;
        if (!sheet || !(lost || (always && sheet.contains(active)))) return;
        sheet.querySelector<HTMLElement>(selector)?.focus();
      },
      { injector: this.injector },
    );
  }
  cancelTest() {
    this.controller?.abort(
      new DOMException("The test was canceled.", "AbortError"),
    );
  }
  async copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      this.announcement = "Copied.";
    } catch {
      this.announcement = "Copy isn't available here. Select the text instead.";
    }
    this.cdr.markForCheck();
  }
  async close() {
    if (this.busy) return;
    if (this.canManage() && this.changed) {
      const discard = await this.dialogs.confirm({
        title: "Discard your changes?",
        message: "This AI connection has changes you haven't saved.",
        confirmLabel: "Discard changes",
        cancelLabel: "Keep editing",
        danger: true,
      });
      if (!discard || !this.alive) return;
    }
    this.cancelTest();
    this.closed.emit();
  }
  private fail(error: unknown) {
    const plain = describeError(error);
    this.error = plain.message;
    this.errorCode = plain.code;
  }
}
