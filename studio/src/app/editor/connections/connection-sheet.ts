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
// Create connection and Edit, from a step's Connection field: the HTTP
// connection form in a side sheet over step details, saying which step and
// slot it's for. Creating starts with no name; editing starts from the bound
// revision and saves a new one. Either way the step binds the result and
// tests it, and the sheet closes back onto the field.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
  signal,
  type OnInit,
} from "@angular/core";
import type { StudioApi } from "../../api";
import type { Identity } from "../../connection";
import { Modal } from "../../dialog";
import { describeError } from "../../errors";
import type { ConnectionDraft } from "../../integrations/connection-copy";
import {
  HttpConnectionForm,
  type CreatedConnection,
} from "../../integrations/http-connection-form";

/** What the sheet opens for. */
export interface ConnectionSheetRequest {
  mode: "create" | "edit";
  /** The step the connection is for. */
  step: string;
  /** The slot whose connection this is. */
  slot: string;
  /** Edit: the bound revision the form starts from. */
  revisionId?: string;
  /** Edit: the connection's name. */
  name?: string;
}

let sheets = 0;
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) => (typeof value === "string" ? value : "");

/** A connection revision as the form's starting point: its settings and secret handle names, never values. */
export function prefillFromRevision(
  revision: Record<string, unknown>,
): Partial<ConnectionDraft> {
  const config = isRecord(revision["config"]) ? revision["config"] : {};
  const auth = isRecord(config["auth"]) ? config["auth"] : {};
  const kind = text(auth["kind"]) || "none";
  const origin = text(config["baseUrl"]);
  const prefill: Partial<ConnectionDraft> = {
    name: text(revision["name"]),
    origin,
    auth: kind as ConnectionDraft["auth"],
  };
  if (kind === "api-key") prefill.header = text(auth["header"]);
  if (kind === "machine-token") {
    prefill.clientId = text(auth["client_id"]);
    prefill.endpoint = text(auth["endpoint"]);
    if (Array.isArray(auth["scopes"]))
      prefill.scopes = auth["scopes"]
        .filter((s) => typeof s === "string")
        .join(" ");
    if (auth["authentication"] === "client_secret_basic")
      prefill.authentication = "client_secret_basic";
  }
  const secrets = isRecord(revision["secretRef"]) ? revision["secretRef"] : {};
  prefill.secrets = Object.fromEntries(
    Object.entries(secrets).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
  const destinations = revision["allowed_destinations"];
  if (Array.isArray(destinations))
    prefill.extraDestinations = destinations.filter(
      (item): item is string =>
        typeof item === "string" && item !== origin && !origin.startsWith(item),
    );
  return prefill;
}

@Component({
  selector: "weave-connection-sheet",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Modal, HttpConnectionForm],
  template: `<weave-modal
    [heading]="heading()"
    placement="side"
    closeLabel="Close connection"
    [describedBy]="forId"
    (dismiss)="closed.emit()"
  >
    <div class="connection-sheet">
      <p class="connection-sheet-for" [id]="forId">
        For {{ request().step }} · slot {{ request().slot }}
      </p>
      @if (problem()) {
        <p class="notice" data-tone="danger" role="alert">{{ problem() }}</p>
      } @else if (prefill(); as start) {
        <weave-http-connection-form
          class="fills-sheet"
          [api]="api()"
          [identity]="identity()"
          heading=""
          [workspace]="workspace()"
          [prefill]="start"
          [showReadiness]="false"
          (created)="saved.emit($event)"
          (cancel)="closed.emit()"
          (finished)="closed.emit()"
        />
      } @else {
        <p class="dialog-status" role="status">Loading the connection…</p>
      }
    </div>
  </weave-modal>`,
  styles: `
    .connection-sheet {
      display: flex;
      flex-direction: column;
      gap: var(--space-4);
      min-width: 0;
    }
    .connection-sheet-for {
      margin: calc(-1 * var(--space-2)) 0 0;
      color: var(--muted);
      font: var(--type-small);
    }
  `,
})
export class ConnectionSheet implements OnInit {
  api = input.required<StudioApi>();
  identity = input<Identity | null>(null);
  workspace = input("");
  request = input.required<ConnectionSheetRequest>();
  saved = output<CreatedConnection>();
  closed = output<void>();
  readonly prefill = signal<Partial<ConnectionDraft> | null>(null);
  readonly forId = `connection-sheet-for-${++sheets}`;
  readonly problem = signal("");

  heading(): string {
    const request = this.request();
    return request.mode === "edit"
      ? `Edit ${request.name ?? "connection"}`
      : "New API connection";
  }

  ngOnInit(): void {
    const request = this.request();
    if (request.mode === "create" || !request.revisionId) {
      // The slot's name ("http") makes a poor connection name; the line under the title names the slot.
      this.prefill.set({});
      return;
    }
    void this.load(request.revisionId);
  }

  private async load(revisionId: string) {
    const api = this.api();
    try {
      const revision = await api.request<unknown>(
        `${api.environment}/connections/${encodeURIComponent(revisionId)}`,
      );
      // The sheet may have closed or opened elsewhere meanwhile.
      if (this.request().revisionId !== revisionId) return;
      if (!isRecord(revision)) throw Error("The connection couldn't be read.");
      this.prefill.set(prefillFromRevision(revision));
    } catch (error) {
      if (this.request().revisionId === revisionId)
        this.problem.set(
          `Studio couldn't read this connection: ${describeError(error).message}`,
        );
    }
  }
}
