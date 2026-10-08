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
// "Import OpenAPI" tab. The document is uploaded from this computer or
// pasted; there is deliberately no URL field, so Studio never fetches
// anything. The local host lists every operation with a verdict, the person
// picks operations and explicit relaxations, and the host emits Actions on
// the built-in HTTP connector (never a code package).
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import { stringify } from "yaml";
import { ApiError, type StudioApi } from "../api";
import { describeError } from "../errors";
import { exportFile, exportNotice } from "../export-file";
import { Icon } from "../icon";
import {
  HttpActionClient,
  type InventoryOperation,
  type JsonObject,
  type OpenApiImportResult,
  type OpenApiInventory,
} from "./http-action-client";
import {
  HOST_BODY_LIMIT,
  RELAXATIONS,
  RELAXATION_KEYS,
  detectFormat,
  explainDiagnostic,
  filterOperations,
  fitsHostRequest,
  methodEffect,
  operationForPointer,
  relaxationPayload,
  serviceSlug,
  suggestedRelaxations,
  validName,
  type AuthKind,
  type Explained,
  type Method,
  type RelaxationKey,
  type Relaxations,
} from "./http-action-model";
import type { ActionCandidate } from "./http-action-review";
import { builderStyles } from "./http-action-styles";

const SELECTION_LIMIT = 100;
const TOO_LARGE =
  "This document is too large for Studio to analyze here (the limit is about 2 MB once sent). Import it with the command line instead: weave connector import-openapi.";
const RENDER_LIMIT = 200;

interface RelaxState {
  stringLimit: boolean;
  stringLimitValue: string;
  numericFormats: boolean;
  ignoreResponseHeaders: boolean;
  jsonMediaOnly: boolean;
  upgradeOpenapi30: boolean;
}
interface OperationView {
  op: InventoryOperation;
  id: string;
  reasons: Explained[];
  suggestions: RelaxationKey[];
}
interface ImportedView {
  name: string;
  version: string;
  method: string;
  path: string;
  write: boolean;
  notes: Explained[];
}

const record = (value: unknown): JsonObject =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
const methodOf = (document: JsonObject) =>
  String(
    record(record(record(document["spec"])["implementation"])["config"])[
      "method"
    ] ?? "",
  );

let sequence = 0;

@Component({
  selector: "weave-http-action-openapi",
  standalone: true,
  imports: [Icon],
  template: `<div class="hb-openapi">
    <section class="hb-section" [attr.aria-labelledby]="prefix + '-source'">
      <h3 [id]="prefix + '-source'">OpenAPI document</h3>
      <p class="hb-help">
        Choose a JSON or YAML file from this computer, or paste the document.
        Studio never downloads documents from the internet.
      </p>
      <div class="hb-actions">
        <!-- A real button, so the file chooser is reached and shown focused
             from the keyboard; the file input itself stays hidden. -->
        <button type="button" class="hb-small" (click)="filePicker.click()">
          <weave-icon name="upload" />Choose a file
        </button>
        <input
          #filePicker
          type="file"
          hidden
          accept=".json,.yaml,.yml,application/json,application/yaml,text/yaml"
          aria-label="OpenAPI document file"
          [id]="prefix + '-file'"
          (change)="chooseFile($event)"
        />
        @if (fileName()) {
          <span class="hb-help">{{ fileName() }}</span>
        }
        @if (source()) {
          <button type="button" class="hb-small" (click)="clear(true)">
            Clear
          </button>
        }
      </div>
      <div class="hb-field">
        <label [for]="prefix + '-text'">Or paste it here</label>
        <textarea
          [id]="prefix + '-text'"
          spellcheck="false"
          placeholder="openapi: 3.1.0"
          [value]="source()"
          [attr.aria-invalid]="!!sourceError()"
          [attr.aria-describedby]="prefix + '-source-notes'"
          (input)="paste($event)"
        ></textarea>
        <div [id]="prefix + '-source-notes'">
          @if (sourceError()) {
            <p class="hb-error">{{ sourceError() }}</p>
          }
        </div>
      </div>
      <div class="hb-field hb-format">
        <label [for]="prefix + '-format'">Format</label>
        <select [id]="prefix + '-format'" (change)="setFormat($event)">
          <option value="auto" [selected]="formatChoice() === 'auto'">
            Detect automatically ({{ detected().toUpperCase() }})
          </option>
          <option value="json" [selected]="formatChoice() === 'json'">
            JSON
          </option>
          <option value="yaml" [selected]="formatChoice() === 'yaml'">
            YAML
          </option>
        </select>
      </div>
      <details class="hb-field" [open]="anyRelaxation()">
        <summary>
          Import options for documents Weave can't read as they are
        </summary>
        <fieldset>
          <legend class="sr-only">Import options</legend>
          <p class="hb-help">
            Each option you turn on is listed as a warning on the actions it
            changes.
          </p>
          @for (key of relaxationKeys; track key) {
            <div class="hb-relax">
              <label class="hb-check"
                ><input
                  type="checkbox"
                  [id]="prefix + '-relaxation-' + key"
                  [attr.data-relaxation]="key"
                  [checked]="relaxOn(key)"
                  [attr.aria-describedby]="prefix + '-relax-' + key"
                  (change)="setRelaxation(key, checked($event))"
                />{{ relaxations[key].label }}</label
              >
              <p class="hb-help" [id]="prefix + '-relax-' + key">
                {{ relaxations[key].description }}
              </p>
              @if (key === "defaultStringMaxLength" && relax().stringLimit) {
                <label class="hb-inline-number"
                  >Limit in characters
                  <input
                    type="number"
                    min="1"
                    max="4096"
                    [value]="relax().stringLimitValue"
                    (input)="setStringLimit($event)"
                /></label>
              }
            </div>
          }
        </fieldset>
      </details>
      <div class="hb-actions">
        <button
          type="button"
          class="primary"
          [id]="prefix + '-list'"
          [disabled]="!source().trim() || !!sourceError()"
          [attr.aria-disabled]="listing() || null"
          (click)="list()"
        >
          {{ listing() ? "Listing operations…" : "List operations" }}
        </button>
        @if (staleInventory()) {
          <span class="hb-warning" role="status"
            >The document changed. List the operations again.</span
          >
        }
      </div>
      @if (listError()) {
        <div class="notice error-notice hb-block" role="alert">
          <p>{{ listError() }}</p>
        </div>
      }
    </section>

    @if (inventory(); as inv) {
      <section class="hb-section" [attr.aria-labelledby]="prefix + '-ops'">
        <h3 [id]="prefix + '-ops'" tabindex="-1">
          {{ inv.title || "Operations" }}
          @if (inv.openapi) {
            <span class="hb-help">OpenAPI {{ inv.openapi }}</span>
          }
        </h3>
        @for (d of documentNotes(); track $index) {
          <p
            [class.hb-error]="d.severity === 'error'"
            [class.hb-warning]="d.severity !== 'error'"
          >
            {{ d.text }}
            @if (d.hint) {
              <span class="hb-hint">{{ d.hint }}</span>
            }
          </p>
        }
        @for (key of documentSuggestions(); track key) {
          @if (!relaxOn(key)) {
            <button
              type="button"
              class="hb-small"
              (click)="enableRelaxation(key)"
            >
              Turn on “{{ relaxations[key].label }}”
            </button>
          }
        }
        @if (inv.truncated) {
          <p class="hb-warning">
            Studio checked only part of this large document. Operations you
            don't select are not checked in full.
          </p>
        }
        @if (!inv.operations.length) {
          <p class="hb-help" role="status">There are no operations to list.</p>
        } @else {
          <p class="hb-help" role="status">
            {{ readyCount() }} of {{ inv.operations.length }}
            {{ inv.operations.length === 1 ? "operation" : "operations" }} can
            be imported. {{ selected().length }} selected.
          </p>
          <div class="search-field hb-search">
            <weave-icon name="search" />
            <input
              type="search"
              autocomplete="off"
              aria-label="Search operations"
              placeholder="Search by method, path, summary or tag"
              [value]="search()"
              (input)="search.set(value($event))"
            />
          </div>
          <div class="hb-actions">
            <button
              type="button"
              class="hb-small"
              [id]="prefix + '-select-ready'"
              [disabled]="!visibleReady().length"
              (click)="selectVisible()"
            >
              Select all ready{{ search() ? " matches" : "" }}
            </button>
            <button
              type="button"
              class="hb-small"
              [disabled]="!selected().length"
              (click)="clearSelection()"
            >
              Clear selection
            </button>
          </div>
          @if (selected().length >= selectionLimit) {
            <p class="hb-warning">
              You can import up to {{ selectionLimit }} operations at a time.
            </p>
          }
          <ul
            class="hb-ops"
            [attr.aria-label]="'Operations in ' + (inv.title || 'the document')"
          >
            @for (view of visible(); track view.op.key) {
              <li class="hb-op" [class.unsupported]="!view.op.supported">
                <label class="hb-op-main">
                  <input
                    type="checkbox"
                    [checked]="isSelected(view.op.key)"
                    [disabled]="
                      !view.op.supported ||
                      (!isSelected(view.op.key) &&
                        selected().length >= selectionLimit)
                    "
                    [attr.aria-describedby]="
                      view.reasons.length ? view.id : null
                    "
                    (change)="toggle(view.op.key, checked($event))"
                  />
                  <span class="hb-method">{{ view.op.method }}</span>
                  <span class="hb-op-path">{{ view.op.path }}</span>
                  @if (view.op.summary) {
                    <span class="hb-op-summary">{{ view.op.summary }}</span>
                  }
                </label>
                <span class="hb-op-chips">
                  <span
                    class="hb-chip"
                    [class.write]="view.op.sideEffect !== 'read_only'"
                    >{{ effectLabel(view.op) }}</span
                  >
                  <span class="hb-chip" [class.bad]="!view.op.supported">{{
                    view.op.supported ? "Ready" : "Not supported"
                  }}</span>
                </span>
                @if (view.reasons.length) {
                  <ul class="hb-list" [id]="view.id">
                    @for (r of view.reasons; track $index) {
                      <li
                        [class.hb-error]="r.severity === 'error'"
                        [class.hb-warning]="r.severity === 'warning'"
                        [class.hb-info]="r.severity === 'info'"
                      >
                        {{ r.text }}
                        @if (r.hint) {
                          <span class="hb-hint">{{ r.hint }}</span>
                        }
                      </li>
                    }
                  </ul>
                  @for (key of view.suggestions; track key) {
                    @if (!relaxOn(key)) {
                      <button
                        type="button"
                        class="hb-small hb-suggest"
                        (click)="enableRelaxation(key)"
                      >
                        Turn on “{{ relaxations[key].label }}”
                      </button>
                    }
                  }
                }
              </li>
            }
          </ul>
          @if (hiddenCount()) {
            <p class="hb-help">
              {{ hiddenCount() }} more operations. Search to find them.
            </p>
          }
          @if (!visible().length) {
            <p class="hb-help">No operation matches “{{ search() }}”.</p>
          }
          <div class="hb-field">
            <label [for]="prefix + '-name'">Name for this API (optional)</label>
            <input
              [id]="prefix + '-name'"
              autocomplete="off"
              spellcheck="false"
              maxlength="64"
              [placeholder]="defaultName()"
              [value]="apiName()"
              [attr.aria-invalid]="!!nameError()"
              [attr.aria-describedby]="prefix + '-name-help'"
              (input)="apiName.set(value($event))"
            />
            <p class="hb-help" [id]="prefix + '-name-help'">
              Used for the connection. Action names come from each operation.
            </p>
            @if (nameError()) {
              <p class="hb-error">{{ nameError() }}</p>
            }
          </div>
          <div class="hb-actions">
            <button
              type="button"
              class="primary"
              [id]="prefix + '-create'"
              [disabled]="!canCreate()"
              [attr.aria-disabled]="importing() || listing() || null"
              (click)="importSelected()"
            >
              {{
                importing()
                  ? "Creating actions…"
                  : selected().length === 1
                    ? "Create 1 action"
                    : selected().length
                      ? "Create " + selected().length + " actions"
                      : "Create actions"
              }}
            </button>
          </div>
          @if (importError()) {
            <div class="notice error-notice hb-block" role="alert">
              <p>{{ importError() }}</p>
            </div>
          }
        }
      </section>
    }

    @if (importedResult(); as result) {
      <section class="hb-section" [attr.aria-labelledby]="prefix + '-created'">
        <h3 [id]="prefix + '-created'" tabindex="-1">
          {{
            result.ok
              ? importedViews().length === 1
                ? "1 action created"
                : importedViews().length + " actions created"
              : "The import found problems"
          }}
        </h3>
        @for (d of importGeneral(); track $index) {
          <p
            [class.hb-error]="d.severity === 'error'"
            [class.hb-warning]="d.severity !== 'error'"
          >
            {{ d.text }}
            @if (d.hint) {
              <span class="hb-hint">{{ d.hint }}</span>
            }
          </p>
        }
        @if (importedViews().length) {
          <p class="hb-help">
            Review each action below, then publish or use it.
          </p>
          <ul class="hb-imported">
            @for (a of importedViews(); track a.name; let i = $index) {
              <li>
                <button
                  type="button"
                  class="hb-pick"
                  [class.current]="i === chosenIndex()"
                  [attr.aria-pressed]="i === chosenIndex()"
                  (click)="choose(i)"
                >
                  <span class="hb-pick-name"
                    >{{ a.name }}&#64;{{ a.version }}</span
                  >
                  <span class="hb-method">{{ a.method }}</span>
                  <span class="hb-op-path">{{ a.path }}</span>
                  <span class="hb-chip" [class.write]="a.write">{{
                    a.write ? "Changes data" : "Reads data"
                  }}</span>
                </button>
                @for (n of a.notes; track $index) {
                  <p
                    [class.hb-warning]="n.severity !== 'error'"
                    [class.hb-error]="n.severity === 'error'"
                  >
                    {{ n.text }}
                  </p>
                }
              </li>
            }
          </ul>
          <div class="hb-actions">
            <button type="button" class="hb-small" (click)="downloadAll()">
              <weave-icon name="download" />Download all actions
            </button>
            @if (result.policy) {
              <button type="button" class="hb-small" (click)="downloadPolicy()">
                <weave-icon name="download" />Download the import policy
              </button>
            }
            <span class="hb-help" role="status">{{ fileMessage() }}</span>
          </div>
        }
      </section>
    }
  </div>`,
  styles: [
    builderStyles,
    `
      .hb-format select {
        max-width: 280px;
      }
      details summary {
        cursor: pointer;
        font-size: 12px;
        font-weight: 600;
        color: var(--link);
      }
      .hb-relax {
        margin-top: 10px;
      }
      .hb-relax .hb-help {
        margin-left: 22px;
      }
      .hb-inline-number {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 6px 0 0 22px;
        font-weight: 500;
      }
      .hb-inline-number input {
        width: 100px;
        margin: 0;
      }
      .hb-block {
        margin-top: 12px;
      }
      .hb-search {
        width: 100%;
        margin-top: 8px;
      }
      .hb-search input {
        flex: 1;
        min-width: 0;
      }
      .hb-ops,
      .hb-imported {
        list-style: none;
        margin: 12px 0 0;
        padding: 0;
        display: grid;
        gap: 6px;
        max-height: 420px;
        overflow: auto;
        overscroll-behavior: contain;
      }
      .hb-op {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px 10px;
        padding: 8px 10px;
        border: 1px solid var(--line);
        border-radius: 6px;
        min-width: 0;
      }
      .hb-op.unsupported {
        background: var(--sunken);
      }
      .hb-op-main {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 6px 10px;
        flex: 1 1 240px;
        min-width: 0;
        font-weight: 500;
      }
      .hb-op-main input {
        width: 16px;
        height: 16px;
        margin: 0;
        accent-color: var(--accent);
      }
      .hb-op-path {
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      .hb-op-summary {
        color: var(--muted);
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      .hb-op-chips {
        display: inline-flex;
        gap: 6px;
        flex-wrap: wrap;
      }
      .hb-op .hb-list {
        flex-basis: 100%;
        margin: 2px 0 0;
      }
      .hb-suggest {
        margin-left: 18px;
      }
      .hb-pick {
        width: 100%;
        justify-content: flex-start;
        flex-wrap: wrap;
        white-space: normal;
        text-align: left;
        gap: 6px 10px;
        min-width: 0;
      }
      .hb-pick.current {
        border-color: var(--accent);
        background: var(--selected);
      }
      .hb-pick-name {
        font-weight: 600;
        overflow-wrap: anywhere;
      }
      h3 .hb-help {
        font-weight: 400;
        margin-left: 6px;
      }
      h3:focus {
        outline: none;
      }
    `,
  ],
})
export class HttpActionOpenApi implements OnDestroy {
  api = input.required<StudioApi>();

  readonly relaxations = RELAXATIONS;
  readonly relaxationKeys = RELAXATION_KEYS;
  readonly selectionLimit = SELECTION_LIMIT;
  prefix = `http-openapi-${++sequence}`;

  source = signal("");
  fileName = signal("");
  formatChoice = signal<"auto" | "json" | "yaml">("auto");
  relax = signal<RelaxState>({
    stringLimit: false,
    stringLimitValue: "256",
    numericFormats: false,
    ignoreResponseHeaders: false,
    jsonMediaOnly: false,
    upgradeOpenapi30: false,
  });
  sourceError = signal("");
  listing = signal(false);
  listError = signal("");
  search = signal("");
  selected = signal<string[]>([]);
  apiName = signal("");
  importing = signal(false);
  importError = signal("");
  chosenIndex = signal(0);
  fileMessage = signal("");
  /** Version chosen after a conflict, per imported action name. */
  private versions = signal<Record<string, string>>({});
  private sourceVersion = signal(0);
  private listed = signal<{ key: string; value: OpenApiInventory } | null>(
    null,
  );
  private imported = signal<{ key: string; value: OpenApiImportResult } | null>(
    null,
  );

  private cdr = inject(ChangeDetectorRef);
  private host = inject(ElementRef<HTMLElement>);
  private client = computed(() => new HttpActionClient(this.api()));
  private relistTimer: ReturnType<typeof setTimeout> | undefined;
  /**
   * Separate counters: a newer listing supersedes an older listing, and a
   * newer import an older import, but a relisting never swallows an import's
   * answer (which would leave "Creating actions…" on screen for good).
   */
  private listGeneration = 0;
  private importGeneration = 0;

  detected = computed(() => detectFormat(this.fileName(), this.source()));
  format = computed((): "json" | "yaml" => {
    const choice = this.formatChoice();
    return choice === "auto" ? this.detected() : choice;
  });
  relaxationPayload = computed((): Relaxations | undefined => {
    const r = this.relax();
    const limit = Number(r.stringLimitValue);
    return relaxationPayload({
      ...(r.stringLimit &&
      Number.isInteger(limit) &&
      limit >= 1 &&
      limit <= 4096
        ? { defaultStringMaxLength: limit }
        : {}),
      numericFormats: r.numericFormats,
      ignoreResponseHeaders: r.ignoreResponseHeaders,
      jsonMediaOnly: r.jsonMediaOnly,
      upgradeOpenapi30: r.upgradeOpenapi30,
    });
  });
  anyRelaxation = computed(() => !!this.relaxationPayload());
  private documentKey = computed(
    () => `${this.sourceVersion()}|${this.format()}`,
  );
  private listKey = computed(
    () =>
      `${this.documentKey()}|${JSON.stringify(this.relaxationPayload() ?? {})}`,
  );
  private importKey = computed(
    () =>
      `${this.listKey()}|${this.selected().join("\n")}|${this.apiName().trim()}`,
  );
  inventory = computed(() => this.listed()?.value ?? null);
  staleInventory = computed(() => {
    const listed = this.listed();
    return !!listed && !listed.key.startsWith(`${this.documentKey()}|`);
  });
  /** The options changed since the list was made; it is listed again shortly. */
  private relistDue = computed(() => {
    const listed = this.listed();
    return !!listed && listed.key !== this.listKey();
  });
  views = computed((): OperationView[] => {
    const inventory = this.inventory();
    // A problem with the whole document is shown once above, not on every operation.
    const shared = new Set(
      (inventory?.diagnostics ?? []).map((d) => `${d.code}|${d.message}`),
    );
    return (inventory?.operations ?? []).map((op, index) => {
      const own = op.reasons.filter(
        (r) => !shared.has(`${r.code}|${r.message}`),
      );
      const reasons = own.map(explainDiagnostic);
      if (!op.supported && !own.length)
        reasons.push({
          severity: "error",
          text: "The problem with the whole document, above, applies here too.",
          code: "",
          technical: "",
        });
      return {
        op,
        id: `${this.prefix}-op-${index}`,
        reasons,
        suggestions: op.supported ? [] : suggestedRelaxations(own),
      };
    });
  });
  private matching = computed(() => {
    const matches = new Set(
      filterOperations(
        this.views().map((v) => v.op),
        this.search(),
      ),
    );
    return this.views().filter((v) => matches.has(v.op));
  });
  visible = computed(() => this.matching().slice(0, RENDER_LIMIT));
  hiddenCount = computed(() =>
    Math.max(0, this.matching().length - RENDER_LIMIT),
  );
  visibleReady = computed(() => this.matching().filter((v) => v.op.supported));
  readyCount = computed(
    () =>
      (this.inventory()?.operations ?? []).filter((o) => o.supported).length,
  );
  documentNotes = computed(() =>
    (this.inventory()?.diagnostics ?? []).map(explainDiagnostic),
  );
  documentSuggestions = computed(() =>
    suggestedRelaxations(this.inventory()?.diagnostics ?? []),
  );
  defaultName = computed(
    () => serviceSlug("", this.inventory()?.title ?? "") || "api",
  );
  nameError = computed(() => {
    const name = this.apiName().trim();
    return name && !validName(name)
      ? "Use letters, digits, dots, hyphens or underscores, starting with a letter or digit."
      : "";
  });
  /** Something to create; the button stays focusable while a request runs. */
  canCreate = computed(
    () =>
      !!this.selected().length &&
      !this.staleInventory() &&
      !this.relistDue() &&
      !this.nameError(),
  );
  canImport = computed(
    () => this.canCreate() && !this.importing() && !this.listing(),
  );
  importedResult = computed(() => {
    const imported = this.imported();
    return imported && imported.key === this.importKey()
      ? imported.value
      : null;
  });
  /** The created Actions, with any version chosen after a conflict. */
  documents = computed(() =>
    (this.importedResult()?.actions ?? []).map((document) => {
      const metadata = record(document["metadata"]);
      const name = String(metadata["name"] ?? "");
      const version = this.versions()[name];
      return version
        ? { ...document, metadata: { ...metadata, version } }
        : document;
    }),
  );
  /** Import diagnostics per OpenAPI operation ("GET /pets"). */
  private importNotes = computed(() => {
    const byOperation = new Map<string, Explained[]>();
    const general: Explained[] = [];
    for (const d of this.importedResult()?.diagnostics ?? []) {
      const operation = operationForPointer(String(d.path ?? ""));
      const explained = explainDiagnostic(d);
      if (operation)
        byOperation.set(operation, [
          ...(byOperation.get(operation) ?? []),
          explained,
        ]);
      else general.push(explained);
    }
    return { byOperation, general };
  });
  importGeneral = computed(() => {
    const result = this.importedResult();
    const notes = this.importNotes();
    if (!result || result.ok) return notes.general;
    // A failed import has no actions: show every problem with its operation.
    return [
      ...notes.general,
      ...[...notes.byOperation.entries()].flatMap(([operation, list]) =>
        list.map((n) => ({ ...n, text: `${operation}: ${n.text}` })),
      ),
    ];
  });
  importedViews = computed((): ImportedView[] => {
    const provenance = record(
      record(this.importedResult()?.provenance)["operations"],
    );
    return this.documents().map((document) => {
      const metadata = record(document["metadata"]);
      const name = String(metadata["name"] ?? "");
      const config = record(
        record(record(document["spec"])["implementation"])["config"],
      );
      const method = methodOf(document);
      const operation = operationForPointer(String(provenance[name] ?? ""));
      return {
        name,
        version: String(metadata["version"] ?? ""),
        method,
        path: String(config["path"] ?? ""),
        write: methodEffect((method || "GET") as Method).action === "write",
        notes: operation
          ? (this.importNotes().byOperation.get(operation) ?? [])
          : [],
      };
    });
  });
  /** What the review panel shows for this tab. */
  candidate = computed((): ActionCandidate => {
    const result = this.importedResult();
    if (!result)
      return {
        document: null,
        pending: this.importing(),
        origin: "",
        auth: null,
        slot: "",
        note: this.imported()
          ? "The document, the options or the selection changed. Create the actions again."
          : "Choose operations and create the actions to review them here.",
      };
    const actions = this.documents();
    const document =
      actions[Math.min(this.chosenIndex(), actions.length - 1)] ?? null;
    const example = record(result.connectionExample);
    const config = record(example["config"]);
    const origin =
      typeof config["baseUrl"] === "string" ? config["baseUrl"] : "";
    const auth = record(config["auth"]);
    const kind =
      typeof auth["kind"] === "string" ? (auth["kind"] as AuthKind) : null;
    return {
      document,
      pending: false,
      origin,
      auth: kind
        ? {
            kind,
            ...(typeof auth["header"] === "string"
              ? { header: auth["header"] }
              : {}),
          }
        : null,
      slot:
        typeof example["name"] === "string"
          ? example["name"]
          : serviceSlug(origin, this.apiName() || this.defaultName()),
      ...(document
        ? {}
        : {
            note: "The import found problems. Fix them and create the actions again.",
          }),
    };
  });

  constructor() {
    // New options relist a document that was already listed, keeping the selection.
    effect(() => {
      const key = this.listKey();
      untracked(() => {
        const listed = this.listed();
        if (!listed || listed.key === key || this.staleInventory()) return;
        clearTimeout(this.relistTimer);
        this.relistTimer = setTimeout(() => void this.list(false), 300);
      });
    });
  }
  ngOnDestroy() {
    clearTimeout(this.relistTimer);
    this.listGeneration++;
    this.importGeneration++;
  }

  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  relaxOn(key: RelaxationKey) {
    const r = this.relax();
    return key === "defaultStringMaxLength" ? r.stringLimit : r[key];
  }
  setRelaxation(key: RelaxationKey, on: boolean) {
    this.relax.update((r) =>
      key === "defaultStringMaxLength"
        ? { ...r, stringLimit: on }
        : { ...r, [key]: on },
    );
  }
  setStringLimit(event: Event) {
    const value = this.value(event);
    this.relax.update((r) => ({ ...r, stringLimitValue: value }));
  }
  setFormat(event: Event) {
    this.formatChoice.set(
      (event.target as HTMLSelectElement).value as "auto" | "json" | "yaml",
    );
  }
  effectLabel(op: InventoryOperation) {
    return op.sideEffect === "read_only" ? "Reads data" : "Changes data";
  }
  isSelected(key: string) {
    return this.selected().includes(key);
  }
  toggle(key: string, on: boolean) {
    const order = (this.inventory()?.operations ?? []).map((o) => o.key);
    this.selected.update((keys) => {
      const next = new Set(keys);
      if (on && next.size < SELECTION_LIMIT) next.add(key);
      else next.delete(key);
      return order.filter((k) => next.has(k));
    });
  }
  selectVisible() {
    const order = (this.inventory()?.operations ?? []).map((o) => o.key);
    this.selected.update((keys) => {
      const next = new Set(keys);
      for (const view of this.visibleReady()) {
        if (next.size >= SELECTION_LIMIT) break;
        next.add(view.op.key);
      }
      return order.filter((k) => next.has(k));
    });
  }
  async chooseFile(event: Event) {
    const element = event.target as HTMLInputElement;
    const file = element.files?.[0];
    element.value = "";
    if (!file) return;
    if (file.size > HOST_BODY_LIMIT) {
      this.sourceError.set(TOO_LARGE);
      this.cdr.markForCheck();
      return;
    }
    try {
      this.setSource(await file.text(), file.name);
    } catch {
      this.sourceError.set("Studio couldn't read that file. Choose it again.");
    }
    this.cdr.markForCheck();
  }
  paste(event: Event) {
    this.setSource(this.value(event), "");
  }
  clear(focus = false) {
    this.setSource("", "");
    this.listed.set(null);
    this.imported.set(null);
    this.selected.set([]);
    if (focus) this.focus(`${this.prefix}-text`);
  }
  /** Turns on the option a reason points to and shows it among the import options. */
  enableRelaxation(key: RelaxationKey) {
    this.setRelaxation(key, true);
    this.focus(`${this.prefix}-relaxation-${key}`);
  }
  clearSelection() {
    this.selected.set([]);
    this.focus(`${this.prefix}-select-ready`);
  }
  setVersion(version: string) {
    const document = this.candidate().document;
    const name = String(record(document?.["metadata"])["name"] ?? "");
    if (name) this.versions.update((v) => ({ ...v, [name]: version }));
  }
  choose(index: number) {
    this.chosenIndex.set(index);
  }

  async list(moveFocus = true) {
    if (!this.source().trim() || this.sourceError()) return;
    // A click while listing waits for the answer already on its way.
    if (moveFocus && this.listing()) return;
    const key = this.listKey();
    const generation = ++this.listGeneration;
    this.listing.set(true);
    this.listError.set("");
    try {
      const value = await this.client().inventory({
        source: this.source(),
        format: this.format(),
        relaxations: this.relaxationPayload(),
      });
      if (generation !== this.listGeneration) return;
      this.listed.set({ key, value });
      const supported = new Set(
        value.operations.filter((o) => o.supported).map((o) => o.key),
      );
      this.selected.update((keys) => keys.filter((k) => supported.has(k)));
      if (moveFocus) this.focus(`${this.prefix}-ops`);
    } catch (e) {
      if (generation !== this.listGeneration) return;
      this.listError.set(this.explainFailure(e));
    } finally {
      if (generation === this.listGeneration) this.listing.set(false);
      this.cdr.markForCheck();
    }
  }
  async importSelected() {
    if (!this.canImport()) return;
    const key = this.importKey();
    const generation = ++this.importGeneration;
    const name = this.apiName().trim();
    this.importing.set(true);
    this.importError.set("");
    try {
      const value = await this.client().importOpenApi({
        source: this.source(),
        format: this.format(),
        relaxations: this.relaxationPayload(),
        selection: this.selected(),
        ...(name ? { name } : {}),
      });
      if (generation !== this.importGeneration) return;
      this.imported.set({ key, value });
      this.versions.set({});
      this.chosenIndex.set(0);
      this.focus(`${this.prefix}-created`);
    } catch (e) {
      if (generation !== this.importGeneration) return;
      this.importError.set(this.explainFailure(e));
    } finally {
      if (generation === this.importGeneration) this.importing.set(false);
      this.cdr.markForCheck();
    }
  }
  downloadAll() {
    const names: string[] = [];
    for (const document of this.documents()) {
      const metadata = record(document["metadata"]);
      const name = `${metadata["name"]}-${metadata["version"]}.action.yaml`;
      if (
        exportFile(
          name,
          "application/yaml",
          stringify(document, { lineWidth: 0, aliasDuplicateObjects: false }),
        ) === "downloaded"
      )
        names.push(name);
    }
    this.fileMessage.set(
      names.length
        ? exportNotice(names)
        : "Studio couldn't start the downloads. Copy each action from the preview instead.",
    );
  }
  downloadPolicy() {
    const policy = this.importedResult()?.policy;
    if (!policy) return;
    const name = `${String(policy["name"] ?? "openapi")}.import-policy.json`;
    this.fileMessage.set(
      exportFile(
        name,
        "application/json",
        `${JSON.stringify(policy, null, 2)}\n`,
      ) === "downloaded"
        ? exportNotice([name])
        : "Studio couldn't start the download.",
    );
  }

  private setSource(text: string, fileName: string) {
    this.source.set(text);
    this.fileName.set(fileName);
    this.sourceVersion.update((v) => v + 1);
    this.sourceError.set(fitsHostRequest(text) ? "" : TOO_LARGE);
  }
  private explainFailure(error: unknown) {
    if (error instanceof ApiError && error.status === 422)
      return "Studio couldn't read this as an OpenAPI document in JSON or YAML. Check the format and the text.";
    if (error instanceof ApiError && error.code === "WV-STUDIO-BUSY")
      return "Studio is still analyzing another document, or this one took too long. Wait a moment, or select fewer operations, then try again.";
    return describeError(error).message;
  }
  private focus(id: string) {
    setTimeout(() =>
      (this.host.nativeElement as HTMLElement)
        .querySelector<HTMLElement>(`#${CSS.escape(id)}`)
        ?.focus(),
    );
  }
}
