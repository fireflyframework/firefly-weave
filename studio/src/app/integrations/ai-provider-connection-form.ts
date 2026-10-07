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
  ElementRef,
  Injector,
  afterNextRender,
  OnDestroy,
  OnInit,
  inject,
  input,
  output,
} from "@angular/core";
import { ApiError, type StudioApi } from "../api";
import { describeError } from "../errors";
import { Select } from "../forms/ui/select";
import {
  aiConnector,
  aiConnectionRequest,
  aiProviderEndpoint,
  aiProviders,
  aiSetupRows,
  type AiConnection,
  type AiConnectionDraft,
} from "./ai-provider-connection";

@Component({
  selector: "weave-ai-provider-connection-form",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Select],
  template: `
    @if (!canManage()) {
      <p role="status">
        Ask an administrator for connection.manage in this environment to create
        a provider connection.
      </p>
    } @else if (done) {
      <h3 class="wizard-title" tabindex="-1">Connection created</h3>
      <p>{{ done.name }} · revision {{ done.revision }}</p>
      <p role="status">Saved · Not tested</p>
      <p>
        The configuration was validated. Provider access has not been tested. No
        model request was sent. A platform operator must authorize the worker
        release to use this connection before a workflow can run; Lumi uses its
        separately configured gateway.
      </p>
      <button type="button" (click)="finished.emit()">Done</button>
    } @else {
      <ol class="progress" aria-label="AI connection setup">
        @for (label of stages; track label; let index = $index) {
          <li
            [attr.aria-current]="step === index ? 'step' : null"
            [class.complete]="step > index"
          >
            <span class="number" aria-hidden="true">{{ index + 1 }}</span>
            <span>{{ label }}</span>
          </li>
        }
      </ol>
      @if (loading) {
        <p role="status">Checking the published AI connector…</p>
      } @else if (!connectorId) {
        <p role="status">
          Publish {{ connector }} in this project first. A platform operator
          installs the Agentic worker and provisions the scoped API key handle.
        </p>
        <button type="button" (click)="load()">Reload AI connector</button>
      }
      <form (submit)="submit($event)">
        <fieldset [disabled]="busy || loading">
          @if (step === 0) {
            <h3 class="wizard-title" tabindex="-1">Choose a provider</h3>
            <p class="hint">
              Name this connection so authors can recognize it. Choose models or
              Azure deployments later in workflow AI profiles or Lumi settings.
            </p>
            <label
              >Connection name
              <input
                aria-label="Connection name"
                [value]="draft.name"
                (input)="draft.name = value($event)"
                autocomplete="off"
                placeholder="For example, team-ai"
              />
            </label>
            <weave-select
              label="Provider"
              [options]="providers"
              [value]="draft.provider"
              (choose)="chooseProvider($event)"
              [disabled]="busy || loading"
            />
          } @else if (step === 1) {
            <h3 class="wizard-title" tabindex="-1">Provider access</h3>
            <p class="hint">
              Choose the credential handle approved for {{ providerLabel }} in
              this environment.
            </p>
            @if (draft.provider.startsWith("azure-")) {
              <label
                >Provider endpoint
                <input
                  aria-label="Provider endpoint"
                  [value]="draft.endpoint"
                  (input)="draft.endpoint = value($event)"
                  placeholder="https://your-resource.openai.azure.com"
                  autocomplete="off"
                  spellcheck="false"
                />
              </label>
              <p class="hint">
                Enter the base endpoint for your Azure resource.
              </p>
              <label
                >Azure API version
                <input
                  aria-label="Azure API version"
                  [value]="draft.apiVersion"
                  (input)="draft.apiVersion = value($event)"
                  placeholder="For example, 2024-10-21"
                />
              </label>
              <p class="hint">
                Use the API version approved for your deployment.
              </p>
            } @else {
              <p class="hint">Standard endpoint: {{ standardEndpoint }}</p>
              <details>
                <summary>Advanced: custom endpoint</summary>
                <label
                  >Provider endpoint
                  <input
                    aria-label="Provider endpoint"
                    [value]="draft.endpoint"
                    (input)="draft.endpoint = value($event)"
                    [placeholder]="standardEndpoint"
                    autocomplete="off"
                    spellcheck="false"
                  />
                </label>
                <p class="hint">
                  Override only for an approved proxy or compatible service. The
                  worker or Lumi gateway must allow the exact endpoint.
                </p>
              </details>
            }
            <label
              >API key secret handle
              <input
                aria-label="API key secret handle"
                [value]="draft.handle"
                (input)="draft.handle = value($event)"
                autocomplete="off"
                spellcheck="false"
                aria-describedby="ai-secret-help"
              />
            </label>
            <p class="hint" id="ai-secret-help">
              Enter the handle supplied by your platform operator. The operator
              stores and grants the secret for this environment. Never paste the
              API key.
            </p>
          } @else {
            <h3 class="wizard-title" tabindex="-1">Review your connection</h3>
            <p class="hint">
              Check these details before creating an immutable connection
              revision.
            </p>
            <dl class="review">
              <div>
                <dt>Connection name</dt>
                <dd>{{ draft.name.trim() }}</dd>
              </div>
              <div>
                <dt>Provider</dt>
                <dd>{{ providerLabel }}</dd>
              </div>
              <div>
                <dt>Endpoint</dt>
                <dd>{{ reviewedEndpoint }}</dd>
              </div>
              @if (draft.provider.startsWith("azure-")) {
                <div>
                  <dt>Azure API version</dt>
                  <dd>{{ draft.apiVersion.trim() }}</dd>
                </div>
              }
              <div>
                <dt>API key secret handle</dt>
                <dd>{{ draft.handle.trim() }}</dd>
              </div>
            </dl>
            <p class="review-note">
              The platform will validate the configuration. No model request or
              connectivity test is sent. Your operator still needs to authorize
              the worker release or configure Lumi's gateway before use.
            </p>
          }
          <div class="actions">
            @if (step > 0) {
              <button type="button" (click)="back()">Back</button>
            }
            <button type="submit" class="primary" [disabled]="!connectorId">
              {{
                busy
                  ? "Creating connection…"
                  : step === 0
                    ? "Continue"
                    : step === 1
                      ? "Review connection"
                      : "Create AI connection"
              }}
            </button>
          </div>
        </fieldset>
      </form>
    }
    @if (error) {
      <p class="error" role="alert" tabindex="-1">{{ error }}</p>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: 16px;
      min-width: 0;
    }
    .progress {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      list-style: none;
      padding: 0 0 16px;
      margin: 0;
      border-bottom: 1px solid var(--line);
    }
    .progress li {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
    }
    .progress li[aria-current] {
      color: var(--forest);
      font-weight: 650;
    }
    .number {
      display: grid;
      place-items: center;
      width: 24px;
      height: 24px;
      flex: 0 0 24px;
      border: 1px solid var(--field-border);
      border-radius: 50%;
    }
    [aria-current] .number {
      background: var(--forest);
      border-color: var(--forest);
      color: var(--on-dark);
    }
    .complete .number {
      background: var(--selected);
      color: var(--forest);
    }
    fieldset {
      display: grid;
      gap: 14px;
      border: 0;
      padding: 0;
      margin: 0;
      min-width: 0;
    }
    .wizard-title {
      margin: 0;
      font-size: 17px;
      line-height: 1.4;
    }
    label {
      display: grid;
      gap: 6px;
    }
    details[open] {
      display: grid;
      gap: 10px;
    }
    summary {
      cursor: pointer;
      padding: 8px 0;
    }
    input {
      min-width: 0;
      width: 100%;
      box-sizing: border-box;
    }
    p {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .actions {
      display: flex;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 8px;
      border-top: 1px solid var(--line);
      margin-top: 4px;
      padding-top: 16px;
    }
    .actions button {
      min-height: 40px;
    }
    .actions button:first-child:not(.primary) {
      margin-right: auto;
    }
    .review {
      margin: 0;
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .review div {
      display: grid;
      grid-template-columns: minmax(100px, 1fr) minmax(0, 1.5fr);
      gap: 12px;
    }
    dt {
      color: var(--muted);
    }
    dd {
      margin: 0;
      overflow-wrap: anywhere;
      font-weight: 550;
    }
    .review-note {
      border-left: 3px solid var(--jade);
      padding: 10px 12px;
      background: var(--mist);
    }
    @media (max-width: 420px) {
      .progress li {
        flex-direction: column;
        align-items: flex-start;
      }
      .review div {
        grid-template-columns: minmax(0, 1fr);
        gap: 3px;
      }
    }
  `,
})
export class AiProviderConnectionForm implements OnInit, OnDestroy {
  api = input.required<StudioApi>();
  canManage = input.required<boolean>();
  created = output<AiConnection>();
  finished = output<void>();
  providers = aiProviders;
  connector = aiConnector;
  draft: AiConnectionDraft = {
    name: "",
    provider: "",
    endpoint: "",
    apiVersion: "",
    handle: "",
  };
  readonly stages = ["Provider", "Access", "Review"];
  step = 0;
  get providerLabel() {
    return (
      this.providers.find((provider) => provider.value === this.draft.provider)
        ?.label ?? "your provider"
    );
  }
  get standardEndpoint() {
    return aiProviderEndpoint(this.draft.provider);
  }
  get reviewedEndpoint() {
    return aiConnectionRequest(this.draft, this.connectorId).config.endpoint;
  }
  chooseProvider(provider: string) {
    if (provider === this.draft.provider) return;
    this.draft.provider = provider;
    this.draft.endpoint = aiProviderEndpoint(provider);
    this.draft.apiVersion = "";
  }
  connectorId = "";
  loading = false;
  busy = false;
  error = "";
  done: AiConnection | null = null;
  private alive = true;
  private cdr = inject(ChangeDetectorRef);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);
  private focus(selector = ".wizard-title") {
    const step = this.step;
    afterNextRender(
      () => {
        if (this.alive && this.step === step)
          this.host.nativeElement.querySelector<HTMLElement>(selector)?.focus();
      },
      { injector: this.injector },
    );
  }
  back() {
    if (this.busy || this.loading || this.step === 0) return;
    this.step--;
    this.error = "";
    this.focus();
  }
  private attempt: {
    body: ReturnType<typeof aiConnectionRequest>;
    key: string;
  } | null = null;
  ngOnInit() {
    if (this.canManage()) void this.load();
  }
  ngOnDestroy() {
    this.alive = false;
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  async load() {
    this.loading = true;
    this.error = "";
    try {
      const rows = await aiSetupRows(this.api(), "connectors", false);
      if (!this.alive) return;
      this.connectorId = String(
        rows.find(
          (row) =>
            !row["unavailable"] &&
            !row["retired"] &&
            `${row["name"]}@${row["version"]}` === aiConnector,
        )?.["id"] ?? "",
      );
    } catch (error) {
      if (this.alive)
        this.error =
          error instanceof ApiError && error.status === 403
            ? "Reading the published provider connector requires catalog.read in this project. Ask an administrator for that grant as well as connection.manage in this environment."
            : describeError(error).message;
    } finally {
      if (this.alive) {
        this.loading = false;
        this.cdr.markForCheck();
      }
    }
  }
  async submit(event: Event) {
    event.preventDefault();
    if (this.busy || this.loading || !this.canManage() || !this.connectorId)
      return;
    this.error = "";
    try {
      if (this.step === 0) {
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(this.draft.name.trim()))
          throw Error(
            "Enter a connection name using letters, numbers, dots, dashes or underscores.",
          );
        if (
          !this.providers.some(
            (provider) => provider.value === this.draft.provider,
          )
        )
          throw Error("Choose a provider.");
        this.step = 1;
        this.focus();
        return;
      }
      const body = aiConnectionRequest(this.draft, this.connectorId);
      if (this.step === 1) {
        this.step = 2;
        this.focus();
        return;
      }
      if (
        !this.attempt ||
        JSON.stringify(this.attempt.body) !== JSON.stringify(body)
      )
        this.attempt = { body, key: crypto.randomUUID() };
      this.busy = true;
      const result = await this.api().mutate<AiConnection>(
        `${this.api().environment}/connections`,
        "POST",
        this.attempt.body,
        undefined,
        this.attempt.key,
      );
      if (!this.alive) return;
      this.done = result;
      this.created.emit(result);
      this.focus();
    } catch (error) {
      if (this.alive) {
        this.error = describeError(error).message;
        this.focus(".error");
        if (
          error instanceof ApiError &&
          error.code === "WV-IDEMPOTENCY-CONFLICT"
        )
          this.attempt = null;
      }
    } finally {
      if (this.alive) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
}
