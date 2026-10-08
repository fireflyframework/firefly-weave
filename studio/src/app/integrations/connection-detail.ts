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
// The Connections view's detail panel (WP-21): what a connection revision
// reaches and how it signs in, with secret handle names only (the platform
// never returns values), plus "Check configuration (no request is sent)",
// the platform's static check. Load it lazily (@defer).
import {
  ChangeDetectorRef,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import { ApiError, type StudioApi } from "../api";
import { describeError } from "../errors";
import {
  authKinds,
  checkCopy,
  connectionFailureCopy,
  connectionProblems,
  slotLabels,
  type FieldProblem,
} from "./connection-copy";

/** The check waits up to a minute, like the connection form's. */
const checkTimeout = 60_000;

const text = (value: unknown) =>
  typeof value === "string" && value.trim() ? value.trim() : "";
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

type CheckState =
  | { state: "idle" }
  | { state: "running" }
  | { state: "ok" }
  | {
      state: "failed";
      message: string;
      code?: string;
      problems: FieldProblem[];
    };

let sequence = 0;

@Component({
  selector: "weave-connection-detail",
  standalone: true,
  template: `<dl class="task-metadata">
      <dt>Name</dt>
      <dd>{{ text(connection()["name"]) || "—" }}</dd>
      <dt>Connector</dt>
      <dd>{{ connectorText() || "—" }}</dd>
      <dt>Revision</dt>
      <dd>{{ connection()["revision"] || "—" }}</dd>
      @if (address()) {
        <dt>API address</dt>
        <dd>
          <code>{{ address() }}</code>
        </dd>
      }
      @if (signIn()) {
        <dt>Authentication</dt>
        <dd>{{ signIn() }}</dd>
      }
      @if (handles().length) {
        <dt>Secret names</dt>
        <dd>
          <ul class="connection-handles">
            @for (handle of handles(); track handle.slot) {
              <li>
                {{ handle.label }}: <code>{{ handle.name }}</code>
              </li>
            }
          </ul>
        </dd>
      }
      <dt>Destinations</dt>
      <dd>
        {{
          destinations().length
            ? destinations().join(", ")
            : "None — this connection can't reach any host"
        }}
      </dd>
    </dl>
    <details class="disclosure">
      <summary>Technical details</summary>
      <dl class="task-metadata">
        <dt>Adapter</dt>
        <dd>{{ text(connection()["adapter"]) || "—" }}</dd>
        <dt>Revision ID</dt>
        <dd>
          <code class="mono-id">{{ connection()["id"] }}</code>
        </dd>
      </dl>
    </details>
    <p class="hint">
      You choose this connection for a connection slot when you activate a
      workflow version. Secret values stay in the platform's secret store.
    </p>
    @if (canCheck()) {
      <div class="connection-check">
        <button
          type="button"
          [id]="prefix + '-check'"
          [attr.aria-disabled]="check().state === 'running' || null"
          [attr.aria-describedby]="prefix + '-check-status'"
          (click)="runCheck()"
        >
          {{ checkLabel }}
        </button>
        <div [id]="prefix + '-check-status'" aria-live="polite">
          @switch (check().state) {
            @case ("running") {
              <p class="hint">
                <span class="loading-spinner small"></span>{{ copy.running }}
              </p>
            }
            @case ("ok") {
              <p class="notice connection-ok">{{ copy.ok }}</p>
            }
            @case ("failed") {
              <div class="notice error-notice" role="alert">
                <p>{{ failure()?.message }}</p>
                @if (failure()?.problems?.length) {
                  <ul>
                    @for (problem of failure()!.problems; track $index) {
                      <li>{{ problem.message }}</li>
                    }
                  </ul>
                }
                @if (failure()?.code) {
                  <small class="support-code"
                    >Support code: {{ failure()!.code }}</small
                  >
                }
              </div>
            }
          }
        </div>
      </div>
    }`,
  styles: [
    `
      :host {
        display: block;
      }
      .connection-handles {
        margin: 0;
        padding-left: 16px;
      }
      .connection-check {
        margin: 12px 0;
      }
      .connection-check .notice {
        margin-top: 8px;
      }
      .connection-ok {
        background: var(--success-bg);
        border-color: var(--success-bd);
        border-left-color: var(--success);
        color: var(--success-ink);
      }
      /* Busy, not unavailable: the global disabled look, a progress cursor. */
      button[aria-disabled="true"] {
        cursor: progress;
      }
    `,
  ],
})
export class ConnectionDetail {
  api = input.required<StudioApi>();
  /** A connections.list or connections.read revision. */
  connection = input<Record<string, unknown>>({});
  /** The account holds connection.manage here, which the check needs. */
  canCheck = input(false);

  readonly prefix = `connection-detail-${++sequence}`;
  readonly checkLabel = checkCopy.label;
  readonly copy = checkCopy;
  readonly text = text;
  check = signal<CheckState>({ state: "idle" });
  failure = computed(() => {
    const state = this.check();
    return state.state === "failed" ? state : null;
  });
  /** "weave-http@2.0.0" reads "weave-http 2.0.0". */
  connectorText = computed(() => {
    const reference = text(this.connection()["connector"]);
    const at = reference.lastIndexOf("@");
    return at > 0
      ? `${reference.slice(0, at)} ${reference.slice(at + 1)}`
      : reference;
  });
  address = computed(() =>
    text(record(this.connection()["config"])["baseUrl"]),
  );
  signIn = computed(() => {
    const auth = record(record(this.connection()["config"])["auth"]);
    const kind = authKinds.find((k) => k.value === auth["kind"]);
    if (!kind) return "";
    const header = text(auth["header"]);
    return kind.value === "api-key" && header
      ? `${kind.label} (${header})`
      : kind.label;
  });
  /** Slot → handle name pairs. Handles are names, never secret values. */
  handles = computed(() =>
    Object.entries(record(this.connection()["secretRef"])).map(
      ([slot, name]) => ({
        slot,
        label: slotLabels[slot] ?? slot,
        name: text(name),
      }),
    ),
  );
  destinations = computed(() => {
    const list = this.connection()["allowed_destinations"];
    return Array.isArray(list) ? list.map((item) => String(item)) : [];
  });
  private cdr = inject(ChangeDetectorRef);
  /** The revision the running check belongs to; a late answer for another is dropped. */
  private checking = "";

  constructor() {
    // Another revision starts without the previous one's result.
    effect(() => {
      this.connection();
      untracked(() => {
        this.checking = "";
        this.check.set({ state: "idle" });
      });
    });
  }

  async runCheck() {
    if (this.check().state === "running") return;
    const id = text(this.connection()["id"]);
    if (!id) return;
    const api = this.api();
    this.checking = id;
    this.check.set({ state: "running" });
    try {
      const result = await api.request<{ ok?: unknown }>(
        `${api.environment}/connections/${encodeURIComponent(id)}/test`,
        "POST",
        {},
        {},
        checkTimeout,
      );
      if (this.checking !== id) return;
      this.check.set(
        result.ok === true
          ? { state: "ok" }
          : { state: "failed", message: checkCopy.failed, problems: [] },
      );
    } catch (e) {
      if (this.checking !== id) return;
      const plain = describeError(e);
      const status = e instanceof ApiError ? e.status : 0;
      this.check.set({
        state: "failed",
        message:
          connectionFailureCopy(status, plain.code, "check") || plain.message,
        code: plain.code,
        problems:
          status === 422
            ? connectionProblems(e instanceof ApiError ? e.detail : null)
            : [],
      });
    } finally {
      this.cdr.markForCheck();
    }
  }
}
