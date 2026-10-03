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
// "Describe a request" tab: a form over one HTTPS request. Every change is
// checked locally for obvious mistakes, then (debounced) sent to the Studio
// host, whose Python builder is the only thing that produces the Action.
// Host diagnostics are mapped back to the field or row they concern.
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { ApiError, type StudioApi } from "../api";
import { describeError } from "../errors";
import { Icon } from "../icon";
import {
  HttpActionClient,
  type HttpActionBuildResult,
} from "./http-action-client";
import {
  METHODS,
  PARAMETER_LIMIT,
  blankDraft,
  buildRequest,
  connectionIssues,
  emptyStatusFor,
  explainDiagnostic,
  isRead,
  locateDiagnostic,
  methodEffect,
  parseApiAddress,
  pathPlaceholders,
  serviceSlug,
  syncPathRows,
  type AuthKind,
  type FieldKey,
  type HttpActionDraft,
  type Method,
  type ParamLocation,
  type ParamRow,
  type ParamType,
  type StatusRow,
  REQUIRED,
} from "./http-action-model";
import type { ActionCandidate } from "./http-action-review";
import { builderStyles } from "./http-action-styles";

export interface DescribePrefill {
  name?: string;
  method?: Method;
  address?: string;
  path?: string;
}
interface FieldNote {
  field: FieldKey;
  row?: string;
  severity: "error" | "warning" | "info";
  text: string;
  hint?: string;
  code?: string;
}

const AUTH_KINDS: { kind: AuthKind; label: string }[] = [
  { kind: "none", label: "No sign-in" },
  { kind: "api-key", label: "API key in a header" },
  { kind: "basic", label: "User name and password" },
  { kind: "bearer", label: "Bearer token" },
  { kind: "machine-token", label: "OAuth client credentials" },
];
const TYPES: { type: ParamType; label: string }[] = [
  { type: "string", label: "Text" },
  { type: "integer", label: "Whole number" },
  { type: "boolean", label: "Yes or no" },
];
const LOCATION_LABEL: Record<ParamLocation, string> = {
  path: "Path",
  query: "Query",
  header: "Header",
};
/** Builder notes that only restate what the form already shows. */
const QUIET_CODES = new Set(["WV-COMP-UNKNOWN_COMPATIBILITY"]);
const DEBOUNCE_MS = 350;
const MISSING: Partial<Record<FieldKey, string>> = {
  name: "a name",
  path: "a path",
  parameters: "a name for each parameter",
  body: "the body",
};

let sequence = 0;
let rowSequence = 0;
const newRowId = () => `hb-row-${++rowSequence}`;

@Component({
  selector: "weave-http-action-describe",
  standalone: true,
  imports: [Icon, NgTemplateOutlet],
  template: `<form
    class="hb-describe"
    novalidate
    (submit)="$event.preventDefault()"
  >
    <section class="hb-section" [attr.aria-labelledby]="prefix + '-action'">
      <h3 [id]="prefix + '-action'">Action</h3>
      <div class="hb-grid">
        <div class="hb-field">
          <label [for]="prefix + '-name'">Name</label>
          <input
            [id]="prefix + '-name'"
            autocomplete="off"
            spellcheck="false"
            maxlength="128"
            placeholder="pets.get-pet"
            [attr.data-initial-focus]="initialFocus() ? '' : null"
            [value]="draft().name"
            [attr.aria-invalid]="invalid('name')"
            [attr.aria-describedby]="prefix + '-name-notes'"
            (input)="setText('name', $event)"
            (blur)="touch('name')"
          />
          <div [id]="prefix + '-name-notes'">
            <ng-container
              *ngTemplateOutlet="
                notesTemplate;
                context: { $implicit: fieldNotes('name') }
              "
            />
          </div>
        </div>
        <div class="hb-field">
          <label [for]="prefix + '-version'">Version</label>
          <input
            class="w-version"
            [id]="prefix + '-version'"
            autocomplete="off"
            spellcheck="false"
            maxlength="64"
            [value]="draft().version"
            [attr.aria-invalid]="invalid('version')"
            [attr.aria-describedby]="prefix + '-version-notes'"
            (input)="setText('version', $event)"
          />
          <div [id]="prefix + '-version-notes'">
            <ng-container
              *ngTemplateOutlet="
                notesTemplate;
                context: { $implicit: fieldNotes('version') }
              "
            />
          </div>
        </div>
      </div>
      <div class="hb-field">
        <label [for]="prefix + '-description'">Description (optional)</label>
        <input
          [id]="prefix + '-description'"
          autocomplete="off"
          maxlength="1024"
          placeholder="What the action does, in a few words"
          [value]="draft().description"
          [attr.aria-describedby]="prefix + '-description-notes'"
          (input)="setText('description', $event)"
        />
        <div [id]="prefix + '-description-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('description') }
            "
          />
        </div>
      </div>
    </section>

    <section class="hb-section" [attr.aria-labelledby]="prefix + '-request'">
      <h3 [id]="prefix + '-request'">Request</h3>
      <div class="hb-grid">
        <div class="hb-field">
          <label [for]="prefix + '-method'">Method</label>
          <select
            [id]="prefix + '-method'"
            [attr.aria-describedby]="prefix + '-effect'"
            (change)="setMethod($event)"
          >
            @for (method of methods; track method) {
              <option [value]="method" [selected]="method === draft().method">
                {{ method }}
              </option>
            }
          </select>
        </div>
        <div class="hb-field">
          <label [for]="prefix + '-address'">API address</label>
          <input
            [id]="prefix + '-address'"
            type="url"
            inputmode="url"
            autocomplete="off"
            spellcheck="false"
            placeholder="https://api.example.com"
            [value]="draft().address"
            [attr.aria-invalid]="invalid('address')"
            [attr.aria-describedby]="
              prefix + '-address-help ' + prefix + '-address-notes'
            "
            (input)="setText('address', $event)"
          />
          <p class="hb-help" [id]="prefix + '-address-help'">
            HTTPS only. The connection keeps this address, not the action.
          </p>
          <div [id]="prefix + '-address-notes'">
            <ng-container
              *ngTemplateOutlet="
                notesTemplate;
                context: { $implicit: fieldNotes('address') }
              "
            />
            @if (basePath()) {
              <button type="button" class="hb-small" (click)="moveBasePath()">
                Move {{ basePath() }} into the path
              </button>
            }
          </div>
        </div>
      </div>
      <div
        class="hb-effect"
        [class.write]="methodInfo().action === 'write'"
        [id]="prefix + '-effect'"
        role="note"
      >
        <weave-icon
          [name]="methodInfo().action === 'write' ? 'warning' : 'lock'"
        />
        <span>
          <strong>{{ methodInfo().label }}.</strong>
          {{ methodInfo().detail }}
          The method decides this, so it can't be changed separately.
        </span>
      </div>
      <ng-container
        *ngTemplateOutlet="
          notesTemplate;
          context: { $implicit: fieldNotes('method') }
        "
      />
      <div class="hb-field">
        <label [for]="prefix + '-path'">Path</label>
        <input
          [id]="prefix + '-path'"
          autocomplete="off"
          spellcheck="false"
          placeholder="/v1/pets/{petId}"
          [value]="draft().path"
          [attr.aria-invalid]="invalid('path')"
          [attr.aria-describedby]="
            prefix + '-path-help ' + prefix + '-path-notes'
          "
          (input)="setPath($event)"
          (blur)="touch('path')"
        />
        <p class="hb-help" [id]="prefix + '-path-help'">
          Write values that change as {{ "{" }}name{{ "}" }}. Each one becomes a
          required path parameter.
        </p>
        <div [id]="prefix + '-path-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('path') }
            "
          />
        </div>
      </div>
      <fieldset
        class="hb-field"
        [attr.aria-describedby]="prefix + '-params-notes'"
      >
        <legend class="hb-legend">Parameters</legend>
        @if (!draft().parameters.length) {
          <p class="hb-help">
            No parameters yet. Path placeholders add theirs automatically.
          </p>
        }
        <ul class="hb-rows">
          @for (row of draft().parameters; track row.id; let i = $index) {
            <li
              class="hb-row"
              [class.invalid]="rowInvalid(row.id)"
              [attr.data-row]="row.location + ':' + row.name"
            >
              <span class="hb-row-tag">{{ locationLabel[row.location] }}</span>
              @if (row.location === "path") {
                <span class="grow monospace">{{ row.name }}</span>
              } @else {
                <input
                  class="grow"
                  [id]="row.id + '-name'"
                  autocomplete="off"
                  spellcheck="false"
                  maxlength="128"
                  [placeholder]="
                    row.location === 'header' ? 'X-Request-Id' : 'limit'
                  "
                  [attr.aria-label]="
                    locationLabel[row.location] + ' parameter name'
                  "
                  [attr.aria-invalid]="rowInvalid(row.id)"
                  [attr.aria-describedby]="row.id + '-notes'"
                  [value]="row.name"
                  (input)="updateRow(row.id, { name: value($event) })"
                  (blur)="touch(row.id)"
                />
              }
              <select
                [attr.aria-label]="
                  'Type of ' +
                  (row.name || locationLabel[row.location] + ' parameter')
                "
                (change)="updateRow(row.id, { type: typeValue($event) })"
              >
                @for (t of types; track t.type) {
                  <option [value]="t.type" [selected]="t.type === row.type">
                    {{ t.label }}
                  </option>
                }
              </select>
              @if (row.location === "query") {
                <label class="hb-check"
                  ><input
                    type="checkbox"
                    [checked]="row.list"
                    (change)="updateRow(row.id, { list: checked($event) })"
                  />List</label
                >
              }
              @if (row.location === "path") {
                <span class="hb-help">Always required</span>
              } @else {
                <label class="hb-check"
                  ><input
                    type="checkbox"
                    [checked]="row.required"
                    (change)="updateRow(row.id, { required: checked($event) })"
                  />Required</label
                >
                <button
                  type="button"
                  class="hb-remove"
                  [attr.aria-label]="
                    'Remove ' +
                    (row.name || locationLabel[row.location] + ' parameter')
                  "
                  (click)="removeRow(row)"
                >
                  <weave-icon name="trash" />
                </button>
              }
              <div class="hb-row-message" [id]="row.id + '-notes'">
                <ng-container
                  *ngTemplateOutlet="
                    notesTemplate;
                    context: { $implicit: rowNotes(row.id) }
                  "
                />
              </div>
            </li>
          }
        </ul>
        <div class="hb-actions">
          <button
            type="button"
            class="hb-small"
            [id]="prefix + '-add-query'"
            [disabled]="atParameterLimit()"
            (click)="addRow('query')"
          >
            <weave-icon name="plus" />Add query parameter
          </button>
          <button
            type="button"
            class="hb-small"
            [id]="prefix + '-add-header'"
            [disabled]="atParameterLimit()"
            (click)="addRow('header')"
          >
            <weave-icon name="plus" />Add header
          </button>
          @if (atParameterLimit()) {
            <span class="hb-help"
              >An action can have at most {{ parameterLimit }} parameters.</span
            >
          }
        </div>
        <div [id]="prefix + '-params-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('parameters') }
            "
          />
        </div>
      </fieldset>
      <div class="hb-field">
        <label [for]="prefix + '-auth'"
          >How the API checks who is calling</label
        >
        <select
          [id]="prefix + '-auth'"
          [attr.aria-describedby]="prefix + '-auth-help'"
          (change)="setAuth({ kind: authValue($event) })"
        >
          @for (a of authKinds; track a.kind) {
            <option [value]="a.kind" [selected]="a.kind === draft().auth.kind">
              {{ a.label }}
            </option>
          }
        </select>
        <p class="hb-help" [id]="prefix + '-auth-help'">
          The connection holds the secret. Studio never asks for secret values.
        </p>
      </div>
      @if (draft().auth.kind === "api-key") {
        <div class="hb-field">
          <label [for]="prefix + '-auth-header'"
            >Header that carries the key</label
          >
          <input
            [id]="prefix + '-auth-header'"
            autocomplete="off"
            spellcheck="false"
            placeholder="X-API-Key"
            [value]="draft().auth.header"
            [attr.aria-invalid]="invalid('auth')"
            [attr.aria-describedby]="prefix + '-auth-notes'"
            (input)="setAuth({ header: value($event) })"
          />
          <div [id]="prefix + '-auth-notes'">
            <ng-container
              *ngTemplateOutlet="
                notesTemplate;
                context: { $implicit: fieldNotes('auth') }
              "
            />
          </div>
        </div>
      }
      @if (read()) {
        <p class="hb-help">{{ draft().method }} requests don't send a body.</p>
      } @else {
        <div class="hb-field">
          <label class="hb-check"
            ><input
              type="checkbox"
              [id]="prefix + '-body-on'"
              [checked]="draft().body.enabled"
              (change)="setBody({ enabled: checked($event) })"
            />Send a JSON body</label
          >
        </div>
        @if (draft().body.enabled) {
          <fieldset class="hb-field">
            <legend class="hb-legend">Describe the body with</legend>
            <div class="hb-inline">
              <label class="hb-check"
                ><input
                  type="radio"
                  [name]="prefix + '-body-mode'"
                  [checked]="draft().body.mode === 'sample'"
                  (change)="setBody({ mode: 'sample' })"
                />An example</label
              >
              <label class="hb-check"
                ><input
                  type="radio"
                  [name]="prefix + '-body-mode'"
                  [checked]="draft().body.mode === 'schema'"
                  (change)="setBody({ mode: 'schema' })"
                />A JSON Schema</label
              >
            </div>
          </fieldset>
          <div class="hb-field">
            <label [for]="prefix + '-body'">{{
              draft().body.mode === "schema"
                ? "Body JSON Schema"
                : "Example body"
            }}</label>
            <textarea
              [id]="prefix + '-body'"
              spellcheck="false"
              [placeholder]="
                draft().body.mode === 'schema'
                  ? schemaPlaceholder
                  : bodyPlaceholder
              "
              [value]="draft().body.text"
              [attr.aria-invalid]="invalid('body')"
              [attr.aria-describedby]="
                prefix + '-body-help ' + prefix + '-body-notes'
              "
              (input)="setBody({ text: value($event) })"
              (blur)="touch('body')"
            ></textarea>
            <p class="hb-help" [id]="prefix + '-body-help'">
              JSON or YAML. From an example, only field names and types are
              kept; the values are never saved.
            </p>
            <div [id]="prefix + '-body-notes'">
              <ng-container
                *ngTemplateOutlet="
                  notesTemplate;
                  context: { $implicit: fieldNotes('body') }
                "
              />
            </div>
            <label class="hb-check"
              ><input
                type="checkbox"
                [checked]="draft().body.required"
                (change)="setBody({ required: checked($event) })"
              />The body is required</label
            >
          </div>
        }
      }
    </section>

    <section class="hb-section" [attr.aria-labelledby]="prefix + '-response'">
      <h3 [id]="prefix + '-response'">Response</h3>
      <fieldset
        class="hb-field"
        [attr.aria-describedby]="prefix + '-statuses-notes'"
      >
        <legend class="hb-legend">Success statuses</legend>
        <ul class="hb-rows">
          @for (status of draft().statuses; track status.id) {
            <li class="hb-row" [class.invalid]="rowInvalid(status.id)">
              <input
                type="number"
                inputmode="numeric"
                min="200"
                max="299"
                class="hb-status-code"
                [id]="status.id + '-code'"
                aria-label="Status code"
                [attr.aria-invalid]="rowInvalid(status.id)"
                [attr.aria-describedby]="status.id + '-notes'"
                [value]="status.code"
                (input)="updateStatus(status.id, { code: value($event) })"
              />
              @if (emptyFor(status); as e) {
                <label class="hb-check"
                  ><input
                    type="checkbox"
                    [checked]="e.empty"
                    [disabled]="e.forced"
                    (change)="
                      updateStatus(status.id, { empty: checked($event) })
                    "
                  />No response body</label
                >
                @if (e.forced) {
                  <span class="hb-help">{{
                    draft().method === "HEAD"
                      ? "HEAD responses never have a body"
                      : "This status never has a body"
                  }}</span>
                }
              }
              @if (draft().statuses.length > 1) {
                <button
                  type="button"
                  class="hb-remove"
                  [attr.aria-label]="'Remove status ' + status.code"
                  (click)="removeStatus(status.id)"
                >
                  <weave-icon name="trash" />
                </button>
              }
              <div class="hb-row-message" [id]="status.id + '-notes'">
                <ng-container
                  *ngTemplateOutlet="
                    notesTemplate;
                    context: { $implicit: rowNotes(status.id) }
                  "
                />
              </div>
            </li>
          }
        </ul>
        <div class="hb-actions">
          <button
            type="button"
            class="hb-small"
            [id]="prefix + '-add-status'"
            [disabled]="draft().statuses.length >= 20"
            (click)="addStatus()"
          >
            <weave-icon name="plus" />Add a status
          </button>
        </div>
        <div [id]="prefix + '-statuses-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('statuses') }
            "
          />
        </div>
      </fieldset>
      @if (draft().method === "HEAD") {
        <p class="hb-help">HEAD responses have no body to describe.</p>
      } @else {
        <fieldset class="hb-field">
          <legend class="hb-legend">Describe the response body with</legend>
          <div class="hb-inline">
            <label class="hb-check"
              ><input
                type="radio"
                [name]="prefix + '-response-mode'"
                [checked]="draft().response.mode === 'sample'"
                (change)="setResponse({ mode: 'sample' })"
              />An example</label
            >
            <label class="hb-check"
              ><input
                type="radio"
                [name]="prefix + '-response-mode'"
                [checked]="draft().response.mode === 'schema'"
                (change)="setResponse({ mode: 'schema' })"
              />A JSON Schema</label
            >
            <label class="hb-check"
              ><input
                type="radio"
                [name]="prefix + '-response-mode'"
                [checked]="draft().response.mode === 'none'"
                (change)="setResponse({ mode: 'none' })"
              />Leave it untyped</label
            >
          </div>
        </fieldset>
        @if (draft().response.mode !== "none") {
          <div class="hb-field">
            <label [for]="prefix + '-response-text'">{{
              draft().response.mode === "schema"
                ? "Response JSON Schema"
                : "Example response"
            }}</label>
            <textarea
              [id]="prefix + '-response-text'"
              spellcheck="false"
              [placeholder]="
                draft().response.mode === 'schema'
                  ? schemaPlaceholder
                  : responsePlaceholder
              "
              [value]="draft().response.text"
              [attr.aria-invalid]="invalid('response')"
              [attr.aria-describedby]="
                prefix + '-response-help ' + prefix + '-response-notes'
              "
              (input)="setResponse({ text: value($event) })"
            ></textarea>
            <p class="hb-help" [id]="prefix + '-response-help'">
              @if (draft().response.mode === "schema") {
                JSON or YAML. Workflows can read the fields it describes.
              } @else {
                Paste a response without real customer data. Studio infers the
                field names and types; the values are never saved.
              }
            </p>
          </div>
        }
        <div [id]="prefix + '-response-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('response') }
            "
          />
          @if (inferred(); as fields) {
            <p class="hb-info">Fields workflows can read: {{ fields }}</p>
          }
        </div>
      }
      <div class="hb-field">
        <label [for]="prefix + '-timeout'">Timeout in seconds (1 to 30)</label>
        <input
          [id]="prefix + '-timeout'"
          type="number"
          inputmode="numeric"
          min="1"
          max="30"
          class="hb-number"
          [value]="draft().timeout"
          [attr.aria-invalid]="invalid('timeout')"
          [attr.aria-describedby]="prefix + '-timeout-notes'"
          (input)="setText('timeout', $event)"
        />
        <div [id]="prefix + '-timeout-notes'">
          <ng-container
            *ngTemplateOutlet="
              notesTemplate;
              context: { $implicit: fieldNotes('timeout') }
            "
          />
        </div>
      </div>
      <details class="hb-field">
        <summary>Advanced</summary>
        <div class="hb-field">
          <label [for]="prefix + '-longest'"
            >Longest text value accepted in parameters</label
          >
          <input
            [id]="prefix + '-longest'"
            type="number"
            inputmode="numeric"
            min="1"
            max="4096"
            class="hb-number"
            [value]="draft().stringMaxLength"
            [attr.aria-invalid]="invalid('stringMaxLength')"
            [attr.aria-describedby]="prefix + '-longest-notes'"
            (input)="setText('stringMaxLength', $event)"
          />
          <div [id]="prefix + '-longest-notes'">
            <ng-container
              *ngTemplateOutlet="
                notesTemplate;
                context: { $implicit: fieldNotes('stringMaxLength') }
              "
            />
          </div>
        </div>
      </details>
    </section>

    @if (fieldNotes("general").length) {
      <section class="hb-section" aria-label="Other problems">
        <ng-container
          *ngTemplateOutlet="
            notesTemplate;
            context: { $implicit: fieldNotes('general') }
          "
        />
      </section>
    }
    <p
      class="hb-status"
      [class.ok]="statusLine().tone === 'ok'"
      [class.bad]="statusLine().tone === 'bad'"
      role="status"
    >
      @if (statusLine().tone === "busy") {
        <span class="loading-spinner small"></span>
      } @else if (statusLine().tone !== "idle") {
        <weave-icon [name]="statusLine().tone === 'ok' ? 'check' : 'warning'" />
      }
      {{ statusLine().text }}
      @if (hostError()) {
        <button type="button" class="hb-small" (click)="retryHost()">
          Try again
        </button>
      }
    </p>
    <ng-template #notesTemplate let-notes>
      @for (note of notes; track $index) {
        <p
          [class.hb-error]="note.severity === 'error'"
          [class.hb-warning]="note.severity === 'warning'"
          [class.hb-info]="note.severity === 'info'"
        >
          {{ note.text }}
          @if (note.hint) {
            <span class="hb-hint">{{ note.hint }}</span>
          }
        </p>
      }
    </ng-template>
  </form>`,
  styles: [
    builderStyles,
    `
      .hb-number {
        max-width: 140px;
      }
      .hb-status-code {
        width: 96px;
      }
      details summary {
        cursor: pointer;
        font-size: 12px;
        font-weight: 600;
        color: var(--jade);
      }
      .monospace {
        overflow-wrap: anywhere;
      }
    `,
  ],
})
export class HttpActionDescribe implements OnInit, OnDestroy {
  api = input.required<StudioApi>();
  prefill = input<DescribePrefill | null>(null);
  /** The name field takes focus when the dialog opens on this tab. */
  initialFocus = input(true);

  readonly methods = METHODS;
  readonly types = TYPES;
  readonly authKinds = AUTH_KINDS;
  readonly locationLabel = LOCATION_LABEL;
  readonly schemaPlaceholder = '{"type": "object", "properties": {}}';
  readonly bodyPlaceholder = '{"name": "Rex", "tag": "dog"}';
  readonly responsePlaceholder = '{"id": 7, "name": "Rex"}';
  readonly parameterLimit = PARAMETER_LIMIT;
  prefix = `http-describe-${++sequence}`;

  draft = signal<HttpActionDraft>(blankDraft(newRowId));
  /** Fields and rows the person has left at least once. */
  touched = signal<ReadonlySet<string>>(new Set());
  /** Latest host answer, keyed by the exact request it answers. */
  private hostResult = signal<{
    key: string;
    result: HttpActionBuildResult;
  } | null>(null);
  waiting = signal(false);
  hostError = signal("");

  private cdr = inject(ChangeDetectorRef);
  private host = inject(ElementRef<HTMLElement>);
  private client = computed(() => new HttpActionClient(this.api()));
  private timer: ReturnType<typeof setTimeout> | undefined;
  private generation = 0;

  built = computed(() => buildRequest(this.draft()));
  requestKey = computed(() => {
    const request = this.built().request;
    return request ? JSON.stringify(request) : "";
  });
  methodInfo = computed(() => methodEffect(this.draft().method));
  atParameterLimit = computed(
    () => this.draft().parameters.length >= PARAMETER_LIMIT,
  );
  /** Anything entered that closing the builder would lose. */
  hasInput = computed(() => {
    const d = this.draft();
    return (
      [
        d.name,
        d.description,
        d.address,
        d.path,
        d.body.text,
        d.response.text,
      ].some((text) => !!text.trim()) || d.parameters.length > 0
    );
  });
  read = computed(() => isRead(this.draft().method));
  basePath = computed(() => {
    const parsed = parseApiAddress(this.draft().address);
    return "basePath" in parsed ? parsed.basePath : "";
  });
  private current = computed(() => {
    const host = this.hostResult();
    return host && host.key === this.requestKey() ? host.result : null;
  });
  notes = computed((): FieldNote[] => {
    const draft = this.draft();
    const built = this.built();
    const touched = this.touched();
    const local: FieldNote[] = [...built.issues, ...connectionIssues(draft)]
      // An empty required field is explained once the person has left it.
      .filter(
        (issue) =>
          issue.code !== REQUIRED || touched.has(issue.row ?? issue.field),
      )
      .map((issue) => ({
        field: issue.field,
        severity: issue.severity,
        text: issue.message,
        ...(issue.row ? { row: issue.row } : {}),
        ...(issue.hint ? { hint: issue.hint } : {}),
      }));
    const result = this.current();
    const remote: FieldNote[] = (result?.diagnostics ?? [])
      .filter((d) => !QUIET_CODES.has(d.code))
      .map((d) => {
        const explained = explainDiagnostic(d);
        const at = locateDiagnostic(
          typeof d.path === "string" ? d.path : "",
          built.map,
          draft.parameters,
        );
        return {
          ...at,
          severity: explained.severity,
          text: explained.text,
          code: explained.code,
          ...(explained.hint ? { hint: explained.hint } : {}),
        };
      });
    return [...local, ...remote];
  });
  inferred = computed(() => {
    const action = this.current()?.action;
    if (!action || this.draft().response.mode !== "sample") return "";
    const outputs = (action["spec"] as Record<string, unknown>)?.[
      "outputSchema"
    ] as Record<string, unknown> | undefined;
    const branch = (
      Array.isArray(outputs?.["oneOf"]) ? outputs["oneOf"][0] : outputs
    ) as Record<string, unknown> | undefined;
    const body = (branch?.["properties"] as Record<string, unknown>)?.[
      "body"
    ] as Record<string, unknown> | undefined;
    const properties = body?.["properties"] as
      | Record<string, unknown>
      | undefined;
    const items = (body?.["items"] as Record<string, unknown>)?.[
      "properties"
    ] as Record<string, unknown> | undefined;
    const fields = properties ?? items;
    if (!fields) return "";
    const names = Object.entries(fields).map(([name, schema]) => {
      const type = (schema as Record<string, unknown>)?.["type"];
      return `${name} (${Array.isArray(type) ? type.join(" or ") : (type ?? "any")})`;
    });
    return `${items && !properties ? "each item has " : ""}${names.slice(0, 12).join(", ")}${names.length > 12 ? `, and ${names.length - 12} more` : ""}`;
  });
  statusLine = computed(
    (): { tone: "ok" | "bad" | "busy" | "idle"; text: string } => {
      const errors = this.notes().filter((n) => n.severity === "error").length;
      if (!this.built().request) {
        if (errors)
          return {
            tone: "bad",
            text: `Fix ${errors === 1 ? "the highlighted problem" : `the ${errors} highlighted problems`} to see the action.`,
          };
        const needed = [
          ...new Set(
            this.built()
              .issues.filter((i) => i.code === REQUIRED)
              .map((i) => MISSING[i.field] ?? "the required fields"),
          ),
        ];
        return {
          tone: "idle",
          text: `Enter ${needed.join(" and ")} to see the action.`,
        };
      }
      if (this.hostError())
        return {
          tone: "bad",
          text: `Studio couldn't check the action. ${this.hostError()}`,
        };
      if (this.waiting() || !this.current())
        return { tone: "busy", text: "Updating the preview…" };
      if (!this.current()!.ok)
        return {
          tone: "bad",
          text:
            errors === 1
              ? "1 problem to fix."
              : `${errors || "Some"} problems to fix.`,
        };
      const warnings = this.notes().filter(
        (n) => n.severity === "warning",
      ).length;
      return {
        tone: "ok",
        text: warnings
          ? `The action is ready, with ${warnings} ${warnings === 1 ? "warning" : "warnings"} to review.`
          : "The action is ready.",
      };
    },
  );
  /** What the review panel shows for this tab. */
  candidate = computed((): ActionCandidate => {
    const draft = this.draft();
    const parsed = parseApiAddress(draft.address);
    const origin = "origin" in parsed ? parsed.origin : "";
    const result = this.current();
    const document = result?.ok && result.action ? result.action : null;
    return {
      document,
      pending:
        !!this.built().request &&
        (this.waiting() || (!result && !this.hostError())),
      origin,
      auth:
        draft.auth.kind === "api-key"
          ? { kind: "api-key", header: draft.auth.header.trim() }
          : { kind: draft.auth.kind },
      slot: serviceSlug(origin, draft.name.trim()),
      ...(document ? {} : { note: this.statusLine().text }),
    };
  });

  constructor() {
    effect(() => {
      const key = this.requestKey();
      untracked(() => this.schedule(key));
    });
  }
  ngOnInit() {
    const prefill = this.prefill();
    if (!prefill) return;
    this.draft.update((d) => {
      const next = {
        ...d,
        ...(prefill.name ? { name: prefill.name } : {}),
        ...(prefill.method ? { method: prefill.method } : {}),
        ...(prefill.address ? { address: prefill.address } : {}),
        ...(prefill.path ? { path: prefill.path } : {}),
      };
      const names = pathPlaceholders(next.path);
      return "names" in names
        ? {
            ...next,
            parameters: syncPathRows(next.parameters, names.names, newRowId),
          }
        : next;
    });
  }
  ngOnDestroy() {
    clearTimeout(this.timer);
    this.generation++;
  }

  /** Accepts the version suggested after a version conflict; `focus` shows the changed field. */
  setVersion(version: string, focus = false) {
    this.draft.update((d) => ({ ...d, version }));
    if (focus) this.focus(`${this.prefix}-version`);
  }

  touch(key: string) {
    if (!this.touched().has(key))
      this.touched.update((keys) => new Set([...keys, key]));
  }
  fieldNotes(field: FieldKey) {
    return this.notes().filter((n) => n.field === field && !n.row);
  }
  rowNotes(id: string) {
    return this.notes().filter((n) => n.row === id);
  }
  invalid(field: FieldKey) {
    return this.notes().some(
      (n) => n.field === field && n.severity === "error",
    );
  }
  rowInvalid(id: string) {
    return this.notes().some((n) => n.row === id && n.severity === "error");
  }
  emptyFor(status: StatusRow) {
    return emptyStatusFor(
      this.draft().method,
      Number(status.code),
      status.empty,
    );
  }

  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  typeValue(event: Event) {
    return (event.target as HTMLSelectElement).value as ParamType;
  }
  authValue(event: Event) {
    return (event.target as HTMLSelectElement).value as AuthKind;
  }
  setText(
    field:
      | "name"
      | "version"
      | "description"
      | "address"
      | "timeout"
      | "stringMaxLength",
    event: Event,
  ) {
    const value = this.value(event);
    this.draft.update((d) => ({ ...d, [field]: value }));
  }
  setMethod(event: Event) {
    const method = (event.target as HTMLSelectElement).value as Method;
    this.draft.update((d) => ({ ...d, method }));
  }
  setPath(event: Event) {
    const path = this.value(event);
    this.draft.update((d) => {
      const names = pathPlaceholders(path.trim());
      return {
        ...d,
        path,
        parameters:
          "names" in names
            ? syncPathRows(d.parameters, names.names, newRowId)
            : d.parameters,
      };
    });
  }
  moveBasePath() {
    const parsed = parseApiAddress(this.draft().address);
    if (!("origin" in parsed) || !parsed.basePath) return;
    this.draft.update((d) => {
      const rest = d.path.trim();
      const path =
        parsed.basePath +
        (rest && rest !== "/"
          ? rest.startsWith("/")
            ? rest
            : `/${rest}`
          : "");
      const names = pathPlaceholders(path);
      return {
        ...d,
        address: parsed.origin,
        path,
        parameters:
          "names" in names
            ? syncPathRows(d.parameters, names.names, newRowId)
            : d.parameters,
      };
    });
    this.focus(`${this.prefix}-path`);
  }
  addRow(location: "query" | "header") {
    if (this.atParameterLimit()) return;
    const row: ParamRow = {
      id: newRowId(),
      location,
      name: "",
      type: "string",
      list: false,
      required: false,
    };
    this.draft.update((d) => ({ ...d, parameters: [...d.parameters, row] }));
    this.focus(`${row.id}-name`);
  }
  updateRow(id: string, patch: Partial<ParamRow>) {
    this.draft.update((d) => ({
      ...d,
      parameters: d.parameters.map((r) =>
        r.id === id ? { ...r, ...patch } : r,
      ),
    }));
  }
  removeRow(row: ParamRow) {
    this.draft.update((d) => ({
      ...d,
      parameters: d.parameters.filter((r) => r.id !== row.id),
    }));
    this.focus(
      `${this.prefix}-add-${row.location === "header" ? "header" : "query"}`,
    );
  }
  addStatus() {
    const used = new Set(this.draft().statuses.map((s) => Number(s.code)));
    const code =
      [201, 202, 204, 200, 203, 206].find((c) => !used.has(c)) ?? 200;
    const row: StatusRow = { id: newRowId(), code: String(code), empty: false };
    this.draft.update((d) => ({ ...d, statuses: [...d.statuses, row] }));
    this.focus(`${row.id}-code`);
  }
  updateStatus(id: string, patch: Partial<StatusRow>) {
    this.draft.update((d) => ({
      ...d,
      statuses: d.statuses.map((s) => (s.id === id ? { ...s, ...patch } : s)),
    }));
  }
  removeStatus(id: string) {
    this.draft.update((d) => ({
      ...d,
      statuses: d.statuses.filter((s) => s.id !== id),
    }));
    this.focus(`${this.prefix}-add-status`);
  }
  setBody(patch: Partial<HttpActionDraft["body"]>) {
    this.draft.update((d) => ({ ...d, body: { ...d.body, ...patch } }));
  }
  setResponse(patch: Partial<HttpActionDraft["response"]>) {
    this.draft.update((d) => ({ ...d, response: { ...d.response, ...patch } }));
  }
  setAuth(patch: Partial<HttpActionDraft["auth"]>) {
    this.draft.update((d) => ({ ...d, auth: { ...d.auth, ...patch } }));
  }
  retryHost() {
    const key = this.requestKey();
    this.hostError.set("");
    this.hostResult.set(null);
    this.schedule(key, 0);
  }

  private schedule(key: string, delay = DEBOUNCE_MS) {
    clearTimeout(this.timer);
    this.generation++;
    this.hostError.set("");
    if (!key || this.hostResult()?.key === key) {
      this.waiting.set(false);
      return;
    }
    this.waiting.set(true);
    this.timer = setTimeout(() => void this.run(key), delay);
  }
  private async run(key: string, attempt = 0) {
    const generation = this.generation;
    try {
      const result = await this.client().build(JSON.parse(key));
      if (generation !== this.generation) return;
      this.hostResult.set({ key, result });
      this.waiting.set(false);
    } catch (e) {
      if (generation !== this.generation) return;
      if (e instanceof ApiError && e.code === "WV-STUDIO-BUSY" && attempt < 2) {
        // The host analyzes at most two documents at once; wait and ask again.
        this.timer = setTimeout(
          () => void this.run(key, attempt + 1),
          800 * (attempt + 1),
        );
        return;
      }
      this.waiting.set(false);
      this.hostError.set(describeError(e).message);
    } finally {
      this.cdr.markForCheck();
    }
  }
  private focus(id: string) {
    setTimeout(() =>
      (this.host.nativeElement as HTMLElement)
        .querySelector<HTMLElement>(`#${CSS.escape(id)}`)
        ?.focus(),
    );
  }
}
