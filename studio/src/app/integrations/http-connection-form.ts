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
// Creating a weave-http@2.0.0 connection from Studio, and the readiness
// checklist that names who must act before API actions can run. Both are
// meant to be mounted lazily (@defer or a dynamic import). The form holds
// secret handle NAMES only; it never asks for, stores or sends a secret
// value, and it performs no request to the called API: "Check configuration"
// asks the platform for a static check.
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  Injector,
  OnInit,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from "@angular/core";
import { ApiError, StudioApi } from "../api";
import { copyText, type Identity } from "../connection";
import { describeError } from "../errors";
import { exportFile } from "../export-file";
import { Icon } from "../icon";
import { grantCommand } from "./grant-command";
import {
  readConnectorVersions,
  readDescriptor,
  readReleases,
} from "./activation-requirements";
import {
  authKinds,
  authSlots,
  checkCopy,
  checkDraft,
  connectionFailureCopy,
  connectionProblems,
  connectionRequest,
  destinationEntries,
  emptyDraft,
  handoffCommand,
  handoffRequest,
  prefillFromBuilder,
  sameRequest,
  slotLabels,
  suggestedName,
  type AuthKind,
  type BuilderConnection,
  type ConnectionDraft,
  type ConnectionRequestBody,
  type FieldProblem,
} from "./connection-copy";
import {
  grantedIn,
  httpAdapter,
  httpConnector,
  integrationReadiness,
  isLoopbackPlatform,
  type Observation,
  type Readiness,
  type ReadinessFacts,
  type ReadinessStep,
} from "./readiness";

/** "Check configuration" waits this long: the platform check can reach an identity provider. */
export const connectionCheckTimeout = 60_000;
const maximumPages = 4;
let sequence = 0;

/** True when a request may have reached the platform but its answer was lost. */
function lostAnswer(error: unknown): boolean {
  if (error instanceof ApiError)
    return error.status >= 500 || error.status === 408;
  if (error instanceof DOMException)
    return error.name === "TimeoutError" || error.name === "AbortError";
  return error instanceof TypeError;
}

/** A created connection revision, as the form reports it. */
export interface CreatedConnection {
  id: string;
  name: string;
  revision: number;
}

async function observe<T>(read: () => Promise<T>): Promise<Observation<T>> {
  try {
    return { state: "ok", value: await read() };
  } catch (e) {
    if (e instanceof ApiError && e.status === 403)
      return { state: "forbidden" };
    return { state: "unavailable", code: describeError(e).code };
  }
}

async function pages(
  api: StudioApi,
  collection: string,
  environment: boolean,
): Promise<unknown[]> {
  const items: unknown[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maximumPages; page++) {
    const result = await api.page(collection, environment, cursor);
    items.push(...result.items);
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
  }
  return items;
}

/**
 * Reads what readiness needs from the platform, in parallel: installed
 * adapters, the weave-http-v2 descriptor, published connectors and the
 * environment's releases. Refusals and failures become observations.
 */
export async function gatherReadinessFacts(
  api: StudioApi,
  identity: Identity | null,
): Promise<ReadinessFacts> {
  const profile = api.session.profile;
  const [adapters, descriptor, connectorVersions, releases] = await Promise.all(
    [
      observe(async () => {
        const value = await api.request<{ connectors?: unknown }>(
          `${api.project}/capabilities`,
        );
        return Array.isArray(value.connectors)
          ? value.connectors.filter(
              (item): item is string => typeof item === "string",
            )
          : [];
      }),
      observe(async () => {
        try {
          return readDescriptor(
            await api.request<unknown>(
              `${api.project}/connector-descriptors/${httpAdapter}`,
            ),
          );
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) return null;
          throw e;
        }
      }),
      observe(async () =>
        readConnectorVersions(await pages(api, "connectors", false)),
      ),
      observe(async () =>
        readReleases(await pages(api, "worker-releases", true)),
      ),
    ],
  );
  return {
    identity,
    profile,
    adapters,
    descriptor,
    connectorVersions,
    releases,
  };
}

const statusLabels: Record<ReadinessStep["status"], string> = {
  done: "Done",
  action: "Needs action",
  unknown: "Not checked",
  info: "Note",
};
const marks: Record<ReadinessStep["status"], string> = {
  done: "✓",
  action: "!",
  unknown: "?",
  info: "i",
};

/**
 * The readiness checklist: what the platform, this project and this
 * environment still need before API actions can run, and who must act.
 */
@Component({
  selector: "weave-integration-readiness",
  standalone: true,
  styles: `
    :host {
      display: block;
    }
    .readiness {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      background: var(--surface);
    }
    .readiness-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
    .readiness-head h3 {
      margin: 0;
      font-size: 14px;
    }
    .readiness-tools {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .readiness-tools button {
      min-height: 32px;
      padding: 4px 10px;
    }
    .readiness ul[hidden] {
      display: none;
    }
    .readiness.compact {
      padding: 8px 12px;
    }
    .readiness-line {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px 10px;
    }
    .readiness-line h3 {
      margin: 0;
      font: var(--type-label);
      font-size: 14px;
      margin-right: auto;
    }
    .readiness-line .text-link {
      font: var(--type-small);
      font-weight: 600;
    }
    .readiness-icon {
      display: inline-grid;
      place-items: center;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      font: 700 12px/1 var(--font-sans);
      background: var(--raised);
      color: var(--muted);
    }
    .readiness-icon[data-tone="ok"] {
      background: var(--success-bg);
      color: var(--success-ink);
    }
    .readiness-icon[data-tone="todo"] {
      background: var(--warning-bg);
      color: var(--warning-ink);
    }
    .readiness ul {
      list-style: none;
      margin: 8px 0 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }
    .readiness li {
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      gap: 8px;
    }
    .mark {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      font-weight: 700;
      background: var(--raised);
      color: var(--muted);
    }
    .done .mark {
      background: var(--success-bg);
      color: var(--success-ink);
    }
    .action .mark {
      background: var(--warning-bg);
      color: var(--warning-ink);
      border: 1px solid var(--warning-bd);
    }
    .title {
      margin: 2px 0 0;
      font-weight: 600;
      overflow-wrap: anywhere;
    }
    .who {
      margin: 2px 0 0;
      font-size: 12px;
    }
    .readiness .hint {
      margin: 2px 0 0;
    }
    .command {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      margin-top: 6px;
    }
    .command code {
      flex: 1;
      min-width: 0;
      overflow-wrap: anywhere;
      padding: 6px 8px;
      border-radius: 5px;
      background: var(--sunken);
      font-size: 12px;
    }
    .command button {
      min-height: 30px;
      padding: 4px 10px;
    }
  `,
  template: `<section
    class="readiness"
    [class.compact]="compact()"
    [attr.aria-labelledby]="id + '-title'"
  >
    @if (compact()) {
      <!-- The builder's one line: ready, or how many things to set up. -->
      <div class="readiness-line">
        <span
          class="readiness-icon"
          [attr.data-tone]="lineTone()"
          aria-hidden="true"
          >{{
            lineTone() === "ok" ? "✓" : lineTone() === "todo" ? "!" : "…"
          }}</span
        >
        <h3 [id]="id + '-title'">
          <span role="status">{{ readiness().short }}</span>
        </h3>
        <button
          type="button"
          class="text-link"
          [attr.aria-expanded]="open()"
          [attr.aria-controls]="id + '-steps'"
          (click)="expanded.set(!open())"
        >
          {{ open() ? "Hide" : "Show" }}
        </button>
        <button
          type="button"
          class="text-link"
          [attr.aria-disabled]="loading() ? 'true' : null"
          (click)="refresh()"
        >
          Check again
        </button>
      </div>
      @if (open()) {
        <p class="hint">{{ readiness().summary }}</p>
      }
    } @else {
      <div class="readiness-head">
        <h3 [id]="id + '-title'">{{ heading() }}</h3>
        <div class="readiness-tools">
          <button
            type="button"
            [attr.aria-expanded]="open()"
            [attr.aria-controls]="id + '-steps'"
            (click)="expanded.set(!open())"
          >
            {{ open() ? "Hide steps" : "Show steps" }}
          </button>
          <!-- aria-disabled, not disabled: a disabled button drops keyboard focus
               to the page, where Escape and Tab no longer reach the dialog. -->
          <button
            type="button"
            [attr.aria-disabled]="loading() ? 'true' : null"
            (click)="refresh()"
          >
            Check again
          </button>
        </div>
      </div>
      <p class="hint" role="status">{{ readiness().summary }}</p>
    }
    <ul [id]="id + '-steps'" [hidden]="!open()">
      @for (step of readiness().steps; track step.id) {
        <li [class]="step.status">
          <span class="mark" aria-hidden="true">{{ mark(step) }}</span>
          <div>
            <p class="title">
              <span class="sr-only">{{ statusLabel(step) }}: </span
              >{{ step.title }}
            </p>
            @if (step.who) {
              <p class="who">Who acts: {{ step.who }}</p>
            }
            @if (step.detail) {
              <p class="hint">{{ step.detail }}</p>
            }
            @if (step.command; as command) {
              <div class="command">
                <code>{{ command }}</code>
                <button
                  type="button"
                  [attr.aria-label]="'Copy the command for: ' + step.title"
                  (click)="copy(command)"
                >
                  {{ copied() === command ? "Copied" : "Copy" }}
                </button>
              </div>
            }
          </div>
        </li>
      }
    </ul>
  </section>`,
})
export class IntegrationReadiness implements OnInit {
  api = input.required<StudioApi>();
  /** The signed-in identity; null shows the access steps as not checked. */
  identity = input<Identity | null>(null);
  heading = input("Before API actions can run");
  /** One line with Show, for the API action builder. */
  compact = input(false);
  /** Emitted after every check, so a host can use the connector version and releases found. */
  checked = output<Readiness>();
  facts = signal<ReadinessFacts | null>(null);
  loading = signal(false);
  copied = signal("");
  /** The person's choice; until then the steps show when someone must act. */
  expanded = signal<boolean | null>(null);
  readonly id = `readiness-${++sequence}`;
  readiness = computed(() =>
    integrationReadiness({
      ...(this.facts() ?? {
        adapters: { state: "pending" },
        descriptor: { state: "pending" },
        connectorVersions: { state: "pending" },
        releases: { state: "pending" },
      }),
      // Identity can finish loading after the connector inventory does.
      identity: this.identity(),
      profile: this.api().session.profile,
    }),
  );
  open = computed(() => {
    const readiness = this.readiness();
    // The builder's line stays folded until the person asks.
    if (this.compact()) return this.expanded() ?? false;
    return (
      this.expanded() ??
      (!readiness.checking &&
        (!readiness.looksReady ||
          readiness.steps.some((step) => step.status === "action")))
    );
  });
  /** The line's icon: ready, something to set up, or still checking. */
  lineTone = computed(() => {
    const readiness = this.readiness();
    if (readiness.checking) return "pending";
    return readiness.steps.some((step) => step.status === "action") ||
      !readiness.looksReady
      ? "todo"
      : "ok";
  });
  private cdr = inject(ChangeDetectorRef);
  private generation = 0;
  ngOnInit() {
    void this.refresh();
  }
  async refresh() {
    if (this.loading()) return;
    const generation = ++this.generation;
    this.loading.set(true);
    this.facts.set(null);
    try {
      const facts = await gatherReadinessFacts(this.api(), this.identity());
      if (generation !== this.generation) return;
      this.facts.set(facts);
      this.checked.emit(this.readiness());
    } finally {
      if (generation === this.generation) this.loading.set(false);
      this.cdr.markForCheck();
    }
  }
  mark(step: ReadinessStep) {
    return marks[step.status];
  }
  statusLabel(step: ReadinessStep) {
    return statusLabels[step.status];
  }
  async copy(command: string) {
    if (await copyText(command)) this.copied.set(command);
    this.cdr.markForCheck();
  }
}

/** Prefilled settings, for example from the API action builder or an OpenAPI import. */
export type ConnectionPrefill = Partial<ConnectionDraft>;

/**
 * Creates a weave-http@2.0.0 connection: name, API origin, authentication
 * (field names exactly as the platform's AuthProfile), one secret handle
 * name per slot and the allowed destinations, prefilled with the API origin
 * and the OAuth token endpoint origin. Without connection access it prepares
 * the request and command for an administrator instead.
 */
@Component({
  selector: "weave-http-connection-form",
  standalone: true,
  imports: [IntegrationReadiness, Icon],
  styles: `
    :host {
      display: block;
    }
    .connection-form {
      display: grid;
      gap: 16px;
    }
    fieldset {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 14px 14px;
      margin: 0;
      min-width: 0;
    }
    legend {
      font-weight: 700;
      padding: 0 4px;
    }
    /* A hint sits 4 px under its own control and 16 px above the next
       label, so it reads as part of the field it explains. */
    .field {
      margin-top: var(--space-4);
    }
    legend + .field {
      margin-top: var(--space-2);
    }
    /* A select keeps the width of its longest option; at 360 px that
       overflows the dialog. */
    .field select {
      display: block;
      width: 100%;
    }
    form {
      min-width: 0;
    }
    .field .hint,
    .field .error {
      margin: calc(var(--space-1) - var(--space-2)) 0 0;
      font-size: 12px;
    }
    [aria-invalid="true"] {
      border-color: var(--danger);
    }
    .destinations {
      list-style: none;
      margin: 8px 0 0;
      padding: 0;
      display: grid;
      gap: 6px;
    }
    .destination {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }
    .destination input {
      flex: 1 1 220px;
    }
    .locked {
      flex: 1 1 220px;
      min-width: 0;
      overflow-wrap: anywhere;
      padding: 8px 10px;
      border-radius: 5px;
      background: var(--raised);
    }
    .destination .error {
      flex-basis: 100%;
      margin: 0;
      font-size: 12px;
    }
    .problems {
      margin: 0;
      padding-left: 18px;
    }
    .handoff textarea {
      width: 100%;
      min-height: 160px;
    }
    .handoff code {
      display: block;
      overflow-wrap: anywhere;
      padding: 8px;
      border-radius: 5px;
      background: var(--sunken);
      font-size: 12px;
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    /* A long label ("Check configuration (no request is sent)") wraps inside
       the dialog. On one line it is wider than a 360 px column and widens
       every section of the grid with it. */
    .row button {
      max-width: 100%;
      padding-block: var(--space-1);
      white-space: normal;
      text-align: center;
    }
    .created {
      display: grid;
      gap: 8px;
    }
    .created-notice {
      display: flex;
      align-items: center;
      gap: var(--space-3);
    }
    .last-step {
      display: grid;
      gap: var(--space-2);
      padding-top: var(--space-3);
      border-top: 1px solid var(--line);
    }
    .last-step h4 {
      margin: 0;
      font: var(--type-label);
    }
    .last-step .hint {
      margin: 0;
    }
    .last-step textarea {
      width: 100%;
      min-height: 120px;
    }
    code.grant {
      display: block;
      overflow-wrap: anywhere;
      padding: 8px;
      border-radius: var(--radius-xs);
      background: var(--sunken);
      font: var(--type-mono);
    }
    /* The dialog's footer stays in view at the bottom of the scrolling
       panel (whose padding is 28px). */
    .connection-footer {
      position: sticky;
      bottom: -28px;
      z-index: 2;
      margin: 0 0 -28px;
      padding: 12px 0 16px;
      /* Paint over the panel's side padding without widening the panel. */
      box-shadow:
        -28px 0 0 var(--surface),
        28px 0 0 var(--surface);
    }
  `,
  template: `<section
    class="connection-form"
    [attr.aria-labelledby]="heading() ? id + '-title' : null"
  >
    @if (heading()) {
      <h3 [id]="id + '-title'">{{ heading() }}</h3>
    }
    <p class="hint intro">
      Saves one API's address and sign-in method in
      {{ workspace() || "the selected environment" }}. You enter only the names
      of stored secrets, never the secrets.
    </p>
    @if (showReadiness()) {
      <weave-integration-readiness
        [api]="api()"
        [identity]="identity()"
        (checked)="adopt($event)"
      />
    }
    <form novalidate [id]="id + '-form'" (submit)="submit($event)">
      <fieldset>
        <legend>API</legend>
        <div class="field">
          <label [for]="id + '-name'">Connection name</label>
          <input
            [id]="id + '-name'"
            autocomplete="off"
            spellcheck="false"
            [value]="draft.name"
            [attr.aria-invalid]="invalid('name')"
            [attr.aria-describedby]="described('name', true)"
            (input)="edit('name', $event)"
          />
          <p class="hint" [id]="id + '-name-hint'">
            You'll choose this name when you activate a workflow, for example
            pets.
          </p>
          @for (problem of problemsFor("name"); track $index) {
            <p class="error" [id]="id + '-name-error'">{{ problem.message }}</p>
          }
        </div>
        <div class="field">
          <label [for]="id + '-origin'">API address</label>
          <input
            [id]="id + '-origin'"
            type="url"
            inputmode="url"
            autocomplete="off"
            spellcheck="false"
            placeholder="https://api.example.com"
            [value]="draft.origin"
            [attr.aria-invalid]="invalid('origin')"
            [attr.aria-describedby]="described('origin', true)"
            (input)="edit('origin', $event)"
          />
          <p class="hint" [id]="id + '-origin-hint'">
            The HTTPS origin only. Base paths belong in each action.
          </p>
          @for (problem of problemsFor("origin"); track $index) {
            <p class="error" [id]="id + '-origin-error'">
              {{ problem.message }}
            </p>
          }
        </div>
      </fieldset>
      <fieldset>
        <legend>Authentication</legend>
        <div class="field">
          <label [for]="id + '-auth'">How the API checks who is calling</label>
          <select
            [id]="id + '-auth'"
            [attr.aria-invalid]="invalid('auth')"
            [attr.aria-describedby]="described('auth', true)"
            (change)="chooseAuth($event)"
          >
            @for (kind of authKinds; track kind.value) {
              <option
                [value]="kind.value"
                [selected]="draft.auth === kind.value"
              >
                {{ kind.label }}
              </option>
            }
          </select>
          <p class="hint" [id]="id + '-auth-hint'">{{ authHint() }}</p>
          @for (problem of problemsFor("auth"); track $index) {
            <p class="error" [id]="id + '-auth-error'">{{ problem.message }}</p>
          }
        </div>
        @if (draft.auth === "api-key") {
          <div class="field">
            <label [for]="id + '-header'">Header name</label>
            <input
              [id]="id + '-header'"
              autocomplete="off"
              spellcheck="false"
              placeholder="X-API-Key"
              [value]="draft.header"
              [attr.aria-invalid]="invalid('header')"
              [attr.aria-describedby]="described('header')"
              (input)="edit('header', $event)"
            />
            @for (problem of problemsFor("header"); track $index) {
              <p class="error" [id]="id + '-header-error'">
                {{ problem.message }}
              </p>
            }
          </div>
        }
        @if (draft.auth === "machine-token") {
          <div class="field">
            <label [for]="id + '-clientId'">Client ID</label>
            <input
              [id]="id + '-clientId'"
              autocomplete="off"
              spellcheck="false"
              [value]="draft.clientId"
              [attr.aria-invalid]="invalid('clientId')"
              [attr.aria-describedby]="described('clientId', true)"
              (input)="edit('clientId', $event)"
            />
            <p class="hint" [id]="id + '-clientId-hint'">
              The client ID isn't a secret. The client secret is a stored secret
              named below.
            </p>
            @for (problem of problemsFor("clientId"); track $index) {
              <p class="error" [id]="id + '-clientId-error'">
                {{ problem.message }}
              </p>
            }
          </div>
          <div class="field">
            <label [for]="id + '-endpoint'">Token endpoint</label>
            <input
              [id]="id + '-endpoint'"
              type="url"
              inputmode="url"
              autocomplete="off"
              spellcheck="false"
              placeholder="https://login.example.com/oauth2/token"
              [value]="draft.endpoint"
              [attr.aria-invalid]="invalid('endpoint')"
              [attr.aria-describedby]="described('endpoint', true)"
              (input)="edit('endpoint', $event)"
            />
            <p class="hint" [id]="id + '-endpoint-hint'">
              Its origin is added to the allowed destinations.
            </p>
            @for (problem of problemsFor("endpoint"); track $index) {
              <p class="error" [id]="id + '-endpoint-error'">
                {{ problem.message }}
              </p>
            }
          </div>
          <div class="field">
            <label [for]="id + '-scopes'">Scopes (optional)</label>
            <input
              [id]="id + '-scopes'"
              autocomplete="off"
              spellcheck="false"
              placeholder="pets.read pets.write"
              [value]="draft.scopes"
              [attr.aria-invalid]="invalid('scopes')"
              [attr.aria-describedby]="described('scopes')"
              (input)="edit('scopes', $event)"
            />
            @for (problem of problemsFor("scopes"); track $index) {
              <p class="error" [id]="id + '-scopes-error'">
                {{ problem.message }}
              </p>
            }
          </div>
          <div class="field">
            <label [for]="id + '-authentication'"
              >How the client secret is sent</label
            >
            <select
              [id]="id + '-authentication'"
              [attr.aria-invalid]="invalid('authentication')"
              [attr.aria-describedby]="described('authentication')"
              (change)="edit('authentication', $event)"
            >
              <option
                value="client_secret_post"
                [selected]="draft.authentication === 'client_secret_post'"
              >
                In the request body (most providers)
              </option>
              <option
                value="client_secret_basic"
                [selected]="draft.authentication === 'client_secret_basic'"
              >
                As a basic authentication header
              </option>
            </select>
            @for (problem of problemsFor("authentication"); track $index) {
              <p class="error" [id]="id + '-authentication-error'">
                {{ problem.message }}
              </p>
            }
          </div>
        }
        @for (slot of slots(); track slot) {
          <div class="field">
            <label [for]="id + '-secret-' + slot"
              >{{ slotLabel(slot) }} handle</label
            >
            <input
              [id]="id + '-secret-' + slot"
              autocomplete="off"
              spellcheck="false"
              [placeholder]="'pets-' + slot.replace('_', '-')"
              [value]="draft.secrets[slot] ?? ''"
              [attr.aria-invalid]="invalid('secret.' + slot)"
              [attr.aria-describedby]="described('secret.' + slot, true)"
              (input)="editSecret(slot, $event)"
            />
            <p class="hint" [id]="id + '-secret.' + slot + '-hint'">
              The name the
              {{
                local ? "secret store on this computer" : "platform operator"
              }}
              keeps this secret under. Never the secret itself.
            </p>
            @for (problem of problemsFor("secret." + slot); track $index) {
              <p class="error" [id]="id + '-secret.' + slot + '-error'">
                {{ problem.message }}
              </p>
            }
          </div>
        }
      </fieldset>
      <fieldset>
        <legend>Allowed destinations</legend>
        <p class="hint" [id]="id + '-destinations-hint'">
          The only addresses this connection may call. The API address and an
          OAuth token endpoint are always included.
        </p>
        <ul class="destinations">
          @for (entry of destinations(); track $index) {
            <li class="destination">
              @if (entry.source === "extra") {
                <input
                  type="url"
                  inputmode="url"
                  autocomplete="off"
                  spellcheck="false"
                  placeholder="https://files.example.com"
                  [attr.aria-label]="'Allowed destination ' + ($index + 1)"
                  [value]="entry.value"
                  [attr.aria-invalid]="
                    entry.index >= 0
                      ? invalid('destination.' + entry.index)
                      : null
                  "
                  [attr.aria-describedby]="
                    id +
                    '-destinations-hint' +
                    (entry.index >= 0 &&
                    problemsFor('destination.' + entry.index).length
                      ? ' ' + id + '-destination.' + entry.index + '-error'
                      : '')
                  "
                  (input)="editDestination(entry.extra, $event)"
                />
                <button type="button" (click)="removeDestination(entry.extra)">
                  Remove
                </button>
              } @else {
                <span class="locked"
                  >{{ entry.value }}
                  <small class="hint">{{
                    entry.source === "api" ? "API address" : "Token endpoint"
                  }}</small></span
                >
              }
              @if (entry.index >= 0) {
                @for (
                  problem of problemsFor("destination." + entry.index);
                  track $index
                ) {
                  <p
                    class="error"
                    [id]="
                      $first
                        ? id + '-destination.' + entry.index + '-error'
                        : null
                    "
                  >
                    {{ problem.message }}
                  </p>
                }
              }
            </li>
          } @empty {
            <li class="hint">Enter the API address first.</li>
          }
        </ul>
        @for (problem of problemsFor("destinations"); track $index) {
          <p class="error">{{ problem.message }}</p>
        }
        <div class="row">
          <button type="button" (click)="addDestination()">
            Add destination
          </button>
        </div>
      </fieldset>
      @if (generalProblems().length) {
        <div
          class="notice error-notice general-problems"
          role="alert"
          tabindex="-1"
        >
          @if (failure()) {
            <p>{{ failure() }}</p>
          }
          <ul class="problems">
            @for (problem of generalProblems(); track $index) {
              <li>
                {{ problem.message }}
                @if (problem.code) {
                  <small class="support-code"
                    >Support code: {{ problem.code }}</small
                  >
                }
              </li>
            }
          </ul>
        </div>
      } @else if (failure()) {
        <div
          class="notice error-notice general-problems"
          role="alert"
          tabindex="-1"
        >
          <p>{{ failure() }}</p>
          @if (failureCode()) {
            <small class="support-code"
              >Support code: {{ failureCode() }}</small
            >
          }
        </div>
      }
      @if (phase() === "creating") {
        <p class="dialog-status" role="status">
          <span class="loading-spinner small"></span>Creating the connection…
        </p>
      }
    </form>
    @if (handoff() && prepared(); as request) {
      <section
        class="notice handoff"
        [attr.aria-labelledby]="id + '-handoff-title'"
      >
        <h4 [id]="id + '-handoff-title'">Ask an administrator</h4>
        <p>
          Your account can't create connections here. Send this request to
          someone who can. It names stored secrets by handle only.
        </p>
        <label [for]="id + '-request'"
          >Connection request (connection.json)</label
        >
        <textarea
          [id]="id + '-request'"
          class="monospace"
          readonly
          spellcheck="false"
          [value]="requestText(request)"
        ></textarea>
        @if (request.connector_version_id === "CONNECTOR_VERSION_ID") {
          <p class="hint">
            Replace CONNECTOR_VERSION_ID with the published {{ connector }}
            version ID, or use the command, which looks it up.
          </p>
        }
        <p [id]="id + '-command-label'">Command</p>
        <code [attr.aria-labelledby]="id + '-command-label'">{{
          command(request)
        }}</code>
        <div class="row">
          <button type="button" (click)="copy(requestText(request), 'request')">
            {{ copied() === "request" ? "Copied the request" : "Copy request" }}
          </button>
          <button type="button" (click)="download(request)">
            Download connection.json
          </button>
          <button type="button" (click)="copy(command(request), 'command')">
            {{ copied() === "command" ? "Copied the command" : "Copy command" }}
          </button>
        </div>
      </section>
    }
    @if (done(); as revision) {
      <section class="created" [attr.aria-labelledby]="id + '-created-title'">
        <p
          class="notice created-notice"
          data-tone="success"
          role="status"
          [id]="id + '-created-title'"
        >
          <weave-icon name="check" [size]="20" /><span
            >Created {{ revision.name }} (revision
            {{ revision.revision }}).</span
          >
        </p>
        <div class="row">
          <button
            type="button"
            [attr.aria-disabled]="check().state === 'running' ? 'true' : null"
            (click)="checkConfiguration()"
          >
            {{ checkLabel }}
          </button>
        </div>
        @switch (check().state) {
          @case ("running") {
            <p class="dialog-status" role="status">
              <span class="loading-spinner small"></span>{{ checkCopy.running }}
            </p>
          }
          @case ("ok") {
            <p class="notice ok" data-tone="success" role="status">
              {{ checkCopy.ok }}
            </p>
          }
          @case ("failed") {
            <div class="notice error-notice" role="alert">
              <p>{{ check().message }}</p>
              @if (check().code) {
                <small class="support-code"
                  >Support code: {{ check().code }}</small
                >
              }
            </div>
          }
        }
        @let grant = grantStep(revision);
        <section class="last-step" [attr.aria-labelledby]="id + '-last-step'">
          <h4 [id]="id + '-last-step'">
            Last step: let the connector use this connection
          </h4>
          <p class="hint">
            {{
              local
                ? "Run this on this computer:"
                : "An administrator grants the connector release access with this command and request file:"
            }}
          </p>
          <code class="grant">{{ grant.command }}</code>
          @if (grant.request) {
            <label [for]="id + '-grant-request'">{{ grant.file }}</label>
            <textarea
              [id]="id + '-grant-request'"
              class="monospace"
              readonly
              spellcheck="false"
              [value]="grant.request"
            ></textarea>
          }
          <div class="row">
            <button
              type="button"
              (click)="
                copy(
                  grant.request
                    ? grant.command +
                        '

' +
                        grant.file +
                        ':
' +
                        grant.request
                    : grant.command,
                  'grant'
                )
              "
            >
              <weave-icon name="copy" [size]="16" />{{
                copied() === "grant"
                  ? "Copied"
                  : local
                    ? "Copy command"
                    : "Copy for an administrator"
              }}
            </button>
          </div>
        </section>
      </section>
    }
    <div class="dialog-footer connection-footer">
      @if (done() && !editedSince() && phase() === "idle") {
        @if (fromWorkflow()) {
          <button type="button" (click)="back.emit()">
            Back to the workflow
          </button>
        }
        <button type="button" class="primary" (click)="finished.emit()">
          Done
        </button>
      } @else {
        <button type="button" (click)="cancel.emit()">Cancel</button>
        @if (handoff()) {
          <button type="submit" class="primary" [attr.form]="id + '-form'">
            Prepare the request for an administrator
          </button>
        } @else {
          <!-- While creating, the button stays focusable (aria-disabled), so
               keyboard focus and Escape stay in the dialog. -->
          <button
            type="submit"
            class="primary"
            [attr.form]="id + '-form'"
            [attr.aria-disabled]="phase() !== 'idle' ? 'true' : null"
          >
            {{ done() ? "Create a new revision" : "Create connection" }}
          </button>
        }
      }
    </div>
  </section>`,
})
export class HttpConnectionForm implements OnInit {
  api = input.required<StudioApi>();
  /** The signed-in identity; decides between creating and the administrator hand-off. */
  identity = input<Identity | null>(null);
  /** The API's HTTPS origin, for example from the API action builder. */
  origin = input("");
  /** A suggested connection name; defaults to a slug of the origin. */
  name = input("");
  /** Further prefilled settings, such as the authentication kind and handle names. */
  prefill = input<ConnectionPrefill | null>(null);
  /** The API action builder's connection hand-off (`HttpActionUse.connection`). */
  fromBuilder = input<BuilderConnection | null>(null);
  heading = input("New API connection");
  /** Shows the readiness checklist above the form. */
  showReadiness = input(true);
  /** "Payments / Production": where the connection is saved. */
  workspace = input("");
  /** Opened from a workflow: the footer offers "Back to the workflow". */
  fromWorkflow = input(false);
  /** Emitted once the platform created the connection revision. */
  created = output<CreatedConnection>();
  cancel = output<void>();
  /** "Done" after creating. */
  finished = output<void>();
  /** "Back to the workflow" after creating. */
  back = output<void>();
  readonly id = `http-connection-${++sequence}`;
  readonly connector = httpConnector;
  readonly authKinds = authKinds;
  readonly checkCopy = checkCopy;
  readonly checkLabel = checkCopy.label;
  draft: ConnectionDraft = emptyDraft();
  problems = signal<FieldProblem[]>([]);
  failure = signal("");
  failureCode = signal("");
  phase = signal<"idle" | "creating">("idle");
  /** The created revision; editing afterwards allows creating a new revision. */
  done = signal<CreatedConnection | null>(null);
  editedSince = signal(false);
  check = signal<{
    state: "idle" | "running" | "ok" | "failed";
    message?: string;
    code?: string;
  }>({ state: "idle" });
  /** Shown after a 403 even when the identity seemed to allow creating. */
  refused = signal(false);
  prepared = signal<ConnectionRequestBody | null>(null);
  copied = signal("");
  connectorVersionId = signal("");
  /** The published connector version is still being looked up. */
  resolving = signal(true);
  /** Bumped on every draft edit, so computed views re-read the mutable draft. */
  private revision = signal(0);
  private touchedOrigin = false;
  private attempt: {
    key: string;
    request: ConnectionRequestBody;
    before: Set<string> | null | undefined;
  } | null = null;
  private cdr = inject(ChangeDetectorRef);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);
  get local() {
    return isLoopbackPlatform(this.api().session.profile?.baseUrl);
  }
  handoff = computed(
    () =>
      this.refused() ||
      (!!this.identity() &&
        !grantedIn(
          this.identity(),
          this.api().session.profile,
          "connection.manage",
        )),
  );
  slots = computed(() => {
    this.revision();
    return authSlots[this.draft.auth];
  });
  destinations = computed(() => {
    this.revision();
    return destinationEntries(this.draft);
  });
  generalProblems = computed(() =>
    this.problems().filter((problem) =>
      ["general", "connector", "secrets"].includes(problem.field),
    ),
  );
  constructor() {
    // A later origin from the builder fills the field until the person edits it.
    effect(() => {
      const origin = this.origin();
      untracked(() => {
        if (this.touchedOrigin || !origin || origin === this.draft.origin)
          return;
        const named =
          !this.draft.name ||
          this.draft.name === suggestedName(this.draft.origin);
        this.draft = {
          ...this.draft,
          origin,
          name: named ? this.name() || suggestedName(origin) : this.draft.name,
        };
        this.changed();
      });
    });
  }
  ngOnInit() {
    const prefill = {
      ...prefillFromBuilder(this.fromBuilder()),
      ...(this.prefill() ?? {}),
    };
    const origin = this.origin() || prefill.origin || "";
    this.draft = emptyDraft({
      ...prefill,
      origin,
      name: this.name() || prefill.name || suggestedName(origin),
      secrets: { ...(prefill.secrets ?? {}) },
      extraDestinations: [...(prefill.extraDestinations ?? [])],
    });
    this.changed();
    if (!this.showReadiness()) void this.resolveConnector();
  }
  /** Takes the connector version the readiness check found. */
  adopt(readiness: Readiness) {
    this.connectorVersionId.set(readiness.connectorVersionId);
    this.resolving.set(false);
    if (readiness.connectorVersionId)
      this.problems.update((all) => all.filter((p) => p.field !== "connector"));
    this.cdr.markForCheck();
  }
  private async resolveConnector() {
    const facts = await gatherReadinessFacts(this.api(), this.identity());
    this.adopt(integrationReadiness(facts));
  }
  authHint() {
    this.revision();
    return authKinds.find((kind) => kind.value === this.draft.auth)?.hint ?? "";
  }
  slotLabel(slot: string) {
    return slotLabels[slot] ?? slot;
  }
  problemsFor(field: string) {
    return this.problems().filter((problem) => problem.field === field);
  }
  invalid(field: string) {
    return this.problemsFor(field).length ? "true" : null;
  }
  described(field: string, hint = false) {
    const ids = [
      hint ? `${this.id}-${field}-hint` : "",
      this.problemsFor(field).length ? `${this.id}-${field}-error` : "",
    ].filter(Boolean);
    return ids.join(" ") || null;
  }
  private changed() {
    this.revision.update((value) => value + 1);
    if (this.done()) this.editedSince.set(true);
    this.prepared.set(null);
    this.copied.set("");
  }
  private value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  edit(
    field:
      | "name"
      | "origin"
      | "header"
      | "clientId"
      | "endpoint"
      | "scopes"
      | "authentication",
    event: Event,
  ) {
    if (field === "origin") this.touchedOrigin = true;
    this.draft = { ...this.draft, [field]: this.value(event) };
    this.clear(field);
    this.changed();
  }
  chooseAuth(event: Event) {
    this.draft = { ...this.draft, auth: this.value(event) as AuthKind };
    this.clear("auth");
    this.changed();
  }
  editSecret(slot: string, event: Event) {
    this.draft = {
      ...this.draft,
      secrets: { ...this.draft.secrets, [slot]: this.value(event) },
    };
    this.clear(`secret.${slot}`);
    this.changed();
  }
  addDestination() {
    this.draft = {
      ...this.draft,
      extraDestinations: [...this.draft.extraDestinations, ""],
    };
    this.changed();
    afterNextRender(
      () =>
        [
          ...this.host.nativeElement.querySelectorAll<HTMLInputElement>(
            ".destination input",
          ),
        ]
          .at(-1)
          ?.focus(),
      { injector: this.injector },
    );
  }
  editDestination(extra: number, event: Event) {
    const values = [...this.draft.extraDestinations];
    values[extra] = this.value(event);
    this.draft = { ...this.draft, extraDestinations: values };
    this.problems.update((all) =>
      all.filter((p) => !p.field.startsWith("destination")),
    );
    this.changed();
  }
  removeDestination(extra: number) {
    this.draft = {
      ...this.draft,
      extraDestinations: this.draft.extraDestinations.filter(
        (_, index) => index !== extra,
      ),
    };
    this.problems.update((all) =>
      all.filter((p) => !p.field.startsWith("destination")),
    );
    this.changed();
    afterNextRender(
      () =>
        this.host.nativeElement
          .querySelector<HTMLButtonElement>(".destinations ~ .row button")
          ?.focus(),
      { injector: this.injector },
    );
  }
  private clear(field: string) {
    this.problems.update((all) => all.filter((p) => p.field !== field));
  }
  /** Moves focus to the first field with a problem, or to the problem summary. */
  private focusProblem() {
    afterNextRender(
      () => {
        const root = this.host.nativeElement;
        (
          root.querySelector<HTMLElement>('form [aria-invalid="true"]') ??
          root.querySelector<HTMLElement>(".general-problems")
        )?.focus();
      },
      { injector: this.injector },
    );
  }
  private fail(problems: FieldProblem[], message = "", code = "") {
    this.problems.set(problems);
    this.failure.set(message);
    this.failureCode.set(code);
    this.focusProblem();
  }
  submit(event: Event) {
    event.preventDefault();
    if (this.phase() !== "idle") return;
    const problems = checkDraft(this.draft, { local: this.local });
    if (problems.length) {
      this.fail(problems);
      return;
    }
    if (this.handoff()) {
      this.fail([]);
      this.showHandoff(handoffRequest(this.draft, this.connectorVersionId()));
      return;
    }
    void this.create();
  }
  /** Shows the request and command for an administrator and moves focus to it. */
  private showHandoff(request: ConnectionRequestBody) {
    this.problems.set([]);
    this.prepared.set(request);
    afterNextRender(
      () =>
        this.host.nativeElement
          .querySelector<HTMLElement>(`#${this.id}-request`)
          ?.focus(),
      { injector: this.injector },
    );
  }
  private async create() {
    const versionId = this.connectorVersionId();
    if (!versionId) {
      this.fail([
        {
          field: "connector",
          message: this.resolving()
            ? `Studio is still checking whether ${httpConnector} is published in this project. Try again in a moment.`
            : `${httpConnector} isn't published in this project yet. The checklist names who publishes it.`,
        },
      ]);
      return;
    }
    const request = connectionRequest(this.draft, versionId);
    if (
      !this.attempt ||
      JSON.stringify(this.attempt.request) !== JSON.stringify(request)
    )
      this.attempt = { key: crypto.randomUUID(), request, before: undefined };
    const attempt = this.attempt;
    const api = this.api();
    this.fail([]);
    this.phase.set("creating");
    try {
      // Note the revisions that already carry this name, so a lost answer
      // can be told apart from an older identical revision.
      if (attempt.before === undefined)
        attempt.before = await this.revisionIds(request.name);
      const revision = await api.mutate<Record<string, unknown>>(
        `${api.environment}/connections`,
        "POST",
        request,
        undefined,
        attempt.key,
      );
      this.accept(revision);
    } catch (e) {
      const plain = describeError(e);
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 422 || status === 400 || status === 409) {
        // A conflicting key must not be reused, or "try again" fails forever.
        if (plain.code === "WV-IDEMPOTENCY-CONFLICT") this.attempt = null;
        const fields = connectionProblems(
          e instanceof ApiError ? e.detail : null,
        );
        this.fail(
          fields,
          connectionFailureCopy(status, plain.code, "create") || plain.message,
          plain.code,
        );
      } else if (status === 403) {
        // Refused: show the request for an administrator right away, as the
        // message says, instead of asking for another click.
        this.refused.set(true);
        this.failure.set(
          connectionFailureCopy(status, plain.code, "create") || plain.message,
        );
        this.failureCode.set(plain.code);
        this.showHandoff(request);
      } else if (status === 401) {
        this.fail(
          [],
          connectionFailureCopy(status, plain.code, "create") || plain.message,
          plain.code,
        );
      } else if (!lostAnswer(e)) {
        this.fail([], plain.message, plain.code);
      } else {
        // The answer was lost (timeout, network, server error): re-list by
        // name before any retry, so a retry can't create a second revision.
        const found = await this.findCreated(attempt);
        if (found) this.accept(found);
        else
          this.fail(
            [],
            `${plain.message} Studio checked and found no new connection named ${request.name}. Try again; a retry is recognized, so it won't create a second revision.`,
            plain.code,
          );
      }
    } finally {
      this.phase.set("idle");
      this.cdr.markForCheck();
    }
  }
  private accept(revision: Record<string, unknown>) {
    const created: CreatedConnection = {
      id: String(revision["id"] ?? ""),
      name: String(revision["name"] ?? this.draft.name),
      revision: Number(revision["revision"]) || 1,
    };
    this.attempt = null;
    this.done.set(created);
    this.editedSince.set(false);
    this.check.set({ state: "idle" });
    this.created.emit(created);
    afterNextRender(
      () =>
        this.host.nativeElement
          .querySelector<HTMLElement>(".created button")
          ?.focus(),
      { injector: this.injector },
    );
  }
  private async listNamed(name: string) {
    const items = (await pages(this.api(), "connections", true)) as Record<
      string,
      unknown
    >[];
    return items.filter(
      (item) => !item["unavailable"] && item["name"] === name,
    );
  }
  private async revisionIds(name: string): Promise<Set<string> | null> {
    try {
      return new Set(
        (await this.listNamed(name)).map((item) => String(item["id"])),
      );
    } catch {
      return null;
    }
  }
  private async findCreated(attempt: {
    request: ConnectionRequestBody;
    before: Set<string> | null | undefined;
  }): Promise<Record<string, unknown> | null> {
    try {
      const matches = (await this.listNamed(attempt.request.name)).filter(
        (item) =>
          sameRequest(item, attempt.request) &&
          !(attempt.before && attempt.before.has(String(item["id"]))),
      );
      return (
        matches.sort(
          (a, b) => Number(b["revision"]) - Number(a["revision"]),
        )[0] ?? null
      );
    } catch {
      return null;
    }
  }
  /** "Check configuration (no request is sent)": the platform's static check. */
  async checkConfiguration() {
    const revision = this.done();
    if (!revision || this.check().state === "running") return;
    const api = this.api();
    this.check.set({ state: "running" });
    // A newer revision may be created while the check runs; its answer then
    // belongs to the older revision and is dropped.
    const current = () => this.done()?.id === revision.id;
    try {
      const result = await api.request<{ ok?: unknown; code?: unknown }>(
        `${api.environment}/connections/${encodeURIComponent(revision.id)}/test`,
        "POST",
        {},
        {},
        connectionCheckTimeout,
      );
      if (!current()) return;
      this.check.set(
        result.ok === true
          ? { state: "ok" }
          : { state: "failed", message: checkCopy.failed },
      );
    } catch (e) {
      if (!current()) return;
      const plain = describeError(e);
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 422)
        this.problems.set(
          connectionProblems(e instanceof ApiError ? e.detail : null),
        );
      this.check.set({
        state: "failed",
        message:
          connectionFailureCopy(status, plain.code, "check") || plain.message,
        code: plain.code,
      });
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** The grant with this revision's real ID (one helper for every screen). */
  grantStep(revision: CreatedConnection) {
    return grantCommand(revision.id, this.local);
  }
  requestText(request: ConnectionRequestBody) {
    return JSON.stringify(request, null, 2) + "\n";
  }
  command(request: ConnectionRequestBody) {
    return handoffCommand(
      this.draft,
      request.connector_version_id === "CONNECTOR_VERSION_ID"
        ? ""
        : request.connector_version_id,
      this.api().session.profile,
    );
  }
  async copy(text: string, what: string) {
    if (await copyText(text)) this.copied.set(what);
    this.cdr.markForCheck();
  }
  download(request: ConnectionRequestBody) {
    exportFile(
      "connection.json",
      "application/json",
      this.requestText(request),
    );
  }
}
