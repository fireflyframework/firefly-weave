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
    <p class="hint">
      Choose the provider's approved endpoint and stored API key handle for this
      environment. Model or Azure deployment names belong in workflow AI
      profiles or Lumi settings.
    </p>
    @if (!canManage()) {
      <p role="status">
        Ask an administrator for connection.manage in this environment to create
        a provider connection.
      </p>
    } @else if (done) {
      <h3>Connection created</h3>
      <p>{{ done.name }} · revision {{ done.revision }}</p>
      <p>
        The platform checked this configuration. No model request was sent. A
        platform operator must authorize the worker release to use this
        connection before a workflow can run; Lumi uses its separately
        configured gateway.
      </p>
      <button type="button" (click)="finished.emit()">Done</button>
    } @else {
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
          <label
            >Connection name<input
              aria-label="Connection name"
              [value]="draft.name"
              (input)="draft.name = value($event)"
              autocomplete="off"
          /></label>
          <weave-select
            label="Provider"
            [options]="providers"
            [value]="draft.provider"
            (choose)="draft.provider = $event"
          />
          <label
            >Provider endpoint<input
              aria-label="Provider endpoint"
              [value]="draft.endpoint"
              (input)="draft.endpoint = value($event)"
              placeholder="https://your-resource.openai.azure.com/"
              autocomplete="off"
          /></label>
          <p class="hint">
            Only this endpoint's exact HTTPS origin is allowed. The worker and
            Lumi gateway must also allow it.
          </p>
          @if (draft.provider.startsWith("azure-")) {
            <label
              >Azure API version<input
                aria-label="Azure API version"
                [value]="draft.apiVersion"
                (input)="draft.apiVersion = value($event)"
                placeholder="API version approved for your deployment"
            /></label>
          }
          <label
            >API key secret handle<input
              aria-label="API key secret handle"
              [value]="draft.handle"
              (input)="draft.handle = value($event)"
              autocomplete="off"
              spellcheck="false"
          /></label>
          <p class="hint">
            Enter the handle supplied by your platform operator. The operator
            stores and grants the secret for this environment. Never paste the
            API key.
          </p>
          <button type="submit" class="primary" [disabled]="!connectorId">
            {{ busy ? "Creating connection…" : "Create AI connection" }}
          </button>
        </fieldset>
      </form>
    }
    @if (error) {
      <p class="error" role="alert">{{ error }}</p>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    fieldset {
      display: grid;
      gap: 12px;
      border: 0;
      padding: 0;
      margin: 0;
      min-width: 0;
    }
    label {
      display: grid;
      gap: 6px;
    }
    input {
      min-width: 0;
      width: 100%;
      box-sizing: border-box;
    }
    .hint,
    p {
      margin: 0;
    }
    button {
      justify-self: start;
    }
    p {
      overflow-wrap: anywhere;
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
  connectorId = "";
  loading = false;
  busy = false;
  error = "";
  done: AiConnection | null = null;
  private alive = true;
  private cdr = inject(ChangeDetectorRef);
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
    if (this.busy || !this.canManage()) return;
    this.error = "";
    try {
      const body = aiConnectionRequest(this.draft, this.connectorId);
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
    } catch (error) {
      if (this.alive) {
        this.error = describeError(error).message;
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
