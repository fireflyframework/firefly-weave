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
// The builder's result panel: read-only YAML preview with Copy and Download,
// the full platform compile, publishing through definitions.publish with a
// retained idempotency key, and the "use" hand-off to the host editor.
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from "@angular/core";
import { stringify } from "yaml";
import { ApiError, type StudioApi } from "../api";
import { DialogService } from "../dialog";
import { describeError } from "../errors";
import { exportFile, exportNotice } from "../export-file";
import { Icon } from "../icon";
import {
  HttpActionClient,
  type JsonObject,
  type PublishedVersion,
} from "./http-action-client";
import {
  HTTP_CONNECTOR,
  IMMUTABLE_VERSION,
  classifyPublishError,
  explainDiagnostic,
  methodEffect,
  needsConnector,
  suggestVersion,
  versionTaken,
  type AuthKind,
  type DiagnosticLike,
  type Explained,
  type Method,
  type PublishFailure,
} from "./http-action-model";
import { builderStyles } from "./http-action-styles";

/** What a builder tab offers for review. */
export interface ActionCandidate {
  /** The Action from the local host; null while the description has problems. */
  document: JsonObject | null;
  /** True while the host is still analyzing the latest description. */
  pending: boolean;
  /** HTTPS origin for the connection, or "" when not given. */
  origin: string;
  auth: { kind: AuthKind; header?: string } | null;
  /** Connection slot to create or reuse in the workflow. */
  slot: string;
  /** Why there is no document yet, in plain words. */
  note?: string;
}

/** The hand-off to the editor. Never carries secret values: names only. */
export interface HttpActionUse {
  /** name@version for the step's `uses`. */
  uses: string;
  action: JsonObject;
  connector: typeof HTTP_CONNECTOR;
  /** Slot name for spec.connections; create it, or reuse it when it already uses the connector. */
  slot: string;
  /** Not published: offline, or the person cannot publish. */
  placeholder: boolean;
  /** Prefill for the connection form: no secret values, only names and the origin. */
  connection: {
    name: string;
    baseUrl: string | null;
    auth: { kind: AuthKind; header?: string } | null;
  };
}
export interface PublishedAction {
  id: string;
  name: string;
  version: string;
  uses: string;
}
export type UseContext = "step" | "workflow" | "none";

type CheckState =
  | { state: "idle" }
  | { state: "running" }
  | { state: "ok"; notes: Explained[] }
  | { state: "problems"; problems: Explained[]; notes: Explained[] }
  | { state: "connector" }
  | { state: "error"; message: string };
type ConnectorState =
  | "idle"
  | "checking"
  | "missing"
  | "unpublished"
  | "published"
  | "publishing"
  | "error";

const record = (value: unknown): JsonObject =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
const toYaml = (document: JsonObject) =>
  stringify(document, { lineWidth: 0, aliasDuplicateObjects: false });

let sequence = 0;

@Component({
  selector: "weave-http-action-review",
  standalone: true,
  imports: [Icon],
  template: `<section
    class="hb-review"
    [attr.aria-labelledby]="prefix + '-title'"
  >
    <div class="hb-review-head">
      <h3 [id]="prefix + '-title'" tabindex="-1">Action preview</h3>
      @if (summary(); as s) {
        <span class="hb-review-ref">{{ s.uses }}</span>
        <span class="hb-chip" [class.write]="s.write">{{ s.effect }}</span>
      }
    </div>
    @if (yaml(); as text) {
      <details class="hb-yaml-details">
        <summary>Show YAML</summary>
        <pre
          class="hb-yaml"
          tabindex="0"
          role="region"
          [attr.aria-label]="
            'Action YAML for ' + summary()!.uses + ' (read only)'
          "
          >{{ text }}</pre
        >
        <div class="hb-actions">
          <button type="button" class="hb-small" (click)="copy()">
            <weave-icon name="copy" />Copy YAML
          </button>
          <button type="button" class="hb-small" (click)="download()">
            <weave-icon name="download" />Download .action.yaml
          </button>
        </div>
      </details>
      <span class="hb-help" role="status">{{ fileMessage() }}</span>

      @if (!connected()) {
        <p class="hb-help">
          Connect to a platform to check and publish this action. You can
          download it now and publish it later.
        </p>
      } @else {
        <div class="hb-platform">
          @if (!canCompile()) {
            <p class="hb-help" [id]="prefix + '-nocompile'">
              Your account can't run platform checks in this workspace.
            </p>
          }
          @if (!canPublish()) {
            <p class="hb-help">
              Your account can't publish actions here. Download the file and ask
              a developer to publish it.
            </p>
          }
          <div aria-live="polite">
            @switch (checkState().state) {
              @case ("running") {
                <p class="hb-status">
                  <span class="loading-spinner small"></span>Checking with the
                  platform…
                </p>
              }
              @case ("ok") {
                <p class="hb-status ok">
                  <weave-icon name="check" />The platform accepts this action.
                </p>
              }
              @case ("problems") {
                <p class="hb-status bad">
                  <weave-icon name="warning" />The platform found
                  {{ checkProblems().length }}
                  {{ checkProblems().length === 1 ? "problem" : "problems" }}.
                </p>
                <ul class="hb-list">
                  @for (p of checkProblems(); track $index) {
                    <li>
                      {{ p.text }}
                      @if (p.hint) {
                        <span class="hb-hint">{{ p.hint }}</span>
                      }
                      @if (p.code) {
                        <span class="hb-code">{{ p.code }}</span>
                      }
                    </li>
                  }
                </ul>
              }
              @case ("error") {
                <p class="hb-status bad">
                  <weave-icon name="warning" />{{ checkMessage() }}
                </p>
              }
            }
          </div>
          @if (checkState().state === "connector") {
            <div class="notice hb-readiness" role="status">
              @switch (connectorState()) {
                @case ("checking") {
                  <p>
                    The project can't use the built-in HTTP connector yet.
                    Checking what is missing…
                  </p>
                }
                @case ("missing") {
                  <p>
                    This platform doesn't have the built-in HTTP connector
                    installed. Ask an operator to install it. Until then, you
                    can download the action file.
                  </p>
                }
                @case ("unpublished") {
                  <p>
                    The built-in HTTP connector is installed on the platform but
                    not published in this project yet. It is needed once per
                    project.
                  </p>
                  @if (canPublish()) {
                    <button
                      type="button"
                      class="hb-small"
                      [attr.aria-disabled]="busy() || null"
                      (click)="enableConnector()"
                    >
                      Publish the built-in HTTP connector
                    </button>
                  } @else {
                    <p>
                      Ask someone who can publish definitions in this project to
                      publish it.
                    </p>
                  }
                }
                @case ("publishing") {
                  <p>
                    <span class="loading-spinner small"></span>Publishing the
                    built-in HTTP connector…
                  </p>
                }
                @case ("published") {
                  <p>
                    The built-in HTTP connector is published. Check the action
                    again.
                  </p>
                }
                @default {
                  <p>
                    The project can't use the built-in HTTP connector yet, and
                    Studio couldn't find out why. Ask an operator to check the
                    connector.
                  </p>
                }
              }
            </div>
          }
          <div aria-live="polite">
            @if (published(); as p) {
              <p class="hb-status ok">
                <weave-icon name="check" />Published {{ p.name }}&#64;{{
                  p.version
                }}. Workflows in this project can use it now.
              </p>
            }
            @if (failure(); as f) {
              <div class="notice error-notice hb-failure" role="alert">
                <p>{{ f.message }}</p>
                @for (p of failureProblems(); track $index) {
                  <p>{{ p.text }}</p>
                }
                @if (f.kind === "version" && suggestion()) {
                  <button
                    type="button"
                    class="hb-small"
                    (click)="bumpVersion()"
                  >
                    Use version {{ suggestion() }} instead
                  </button>
                }
                @if (f.kind === "unknown") {
                  <button
                    type="button"
                    class="hb-small"
                    [attr.aria-disabled]="busy() || null"
                    (click)="checkAgain()"
                  >
                    Check again
                  </button>
                }
                @if (f.code) {
                  <span class="support-code">Support code: {{ f.code }}</span>
                }
              </div>
            }
          </div>
        </div>
      }

      @if (context() !== "none" && !readonly()) {
        <div class="hb-use">
          @if (!published() && !placeholderOnly()) {
            <p class="hb-help">
              Publish the action to use it in this workflow.
            </p>
          }
          @if (!published() && placeholderOnly()) {
            <p class="hb-help">
              The workflow will refer to {{ summary()!.uses }}. Publish the
              downloaded file before you run the workflow.
            </p>
          }
          @if (!candidate()?.origin) {
            <p class="hb-help">
              Add the API address to prefill the connection for this API.
            </p>
          }
        </div>
      }
    } @else {
      <p class="hb-help hb-review-empty" aria-live="polite">
        @if (candidate()?.pending) {
          <span class="loading-spinner small"></span>Updating the preview…
        } @else {
          {{
            candidate()?.note ??
              "Describe the request or import operations to see the action here."
          }}
        }
      </p>
    }
  </section>`,
  styles: [
    builderStyles,
    `
      .hb-review {
        border: 1px solid var(--line);
        border-radius: var(--radius-md);
        padding: 16px;
        background: var(--sunken);
        margin-top: 20px;
      }
      .hb-review-head {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
      }
      .hb-review-head h3 {
        margin: 0;
      }
      .hb-review-ref {
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      .hb-yaml-details {
        margin-top: 12px;
      }
      .hb-yaml-details > summary {
        cursor: pointer;
        width: fit-content;
        color: var(--link);
        font: var(--type-label);
        padding: 4px 0;
      }
      .hb-yaml {
        margin: 8px 0 0;
        max-height: 300px;
        overflow: auto;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 10px 12px;
        font-size: 12px;
        white-space: pre;
        overflow-wrap: normal;
      }
      .hb-platform {
        margin-top: 8px;
      }
      .hb-readiness,
      .hb-failure {
        margin-top: 12px;
      }
      .hb-readiness p,
      .hb-failure p {
        margin: 0 0 8px;
      }
      .hb-use {
        border-top: 1px solid var(--line);
        margin-top: 16px;
        padding-top: 12px;
      }
      .hb-review-empty {
        margin: 12px 0 0;
      }
      .hb-review h3:focus {
        outline: none;
      }
    `,
  ],
})
export class HttpActionReview {
  api = input.required<StudioApi>();
  candidate = input<ActionCandidate | null>(null);
  connected = input(false);
  canCompile = input(false);
  canPublish = input(false);
  context = input<UseContext>("workflow");
  readonly = input(false);
  /** Published name@version references, to suggest a free version. */
  publishedVersions = input<readonly string[]>([]);

  use = output<HttpActionUse>();
  publishedAction = output<PublishedAction>();
  /** The person accepted a new version after a version conflict. */
  bump = output<string>();

  prefix = `http-review-${++sequence}`;
  checkState = signal<CheckState>({ state: "idle" });
  connectorState = signal<ConnectorState>("idle");
  publishing = signal(false);
  published = signal<PublishedVersion | null>(null);
  failure = signal<PublishFailure | null>(null);
  suggestion = signal<string | null>(null);
  fileMessage = signal("");

  private dialogs = inject(DialogService);
  private cdr = inject(ChangeDetectorRef);
  private host = inject(ElementRef<HTMLElement>);
  private client = computed(() => new HttpActionClient(this.api()));
  /** Idempotency key for exactly this YAML; kept across unknown outcomes. */
  private key: { yaml: string; key: string } | null = null;
  /** Versions published from this dialog, by exact YAML, so switching back keeps them. */
  private publishedYaml = new Map<string, PublishedVersion>();
  private generation = 0;

  document = computed(() => this.candidate()?.document ?? null);
  yaml = computed(() => {
    const document = this.document();
    return document ? toYaml(document) : "";
  });
  summary = computed(() => {
    const document = this.document();
    if (!document) return null;
    const metadata = record(document["metadata"]);
    const spec = record(document["spec"]);
    const config = record(record(spec["implementation"])["config"]);
    const method = String(config["method"] ?? "GET") as Method;
    const effect = methodEffect(method);
    return {
      name: String(metadata["name"] ?? ""),
      version: String(metadata["version"] ?? ""),
      uses: `${metadata["name"]}@${metadata["version"]}`,
      effect: effect.label,
      write: effect.action === "write",
    };
  });
  busy = computed(
    () =>
      this.publishing() ||
      this.checkState().state === "running" ||
      this.connectorState() === "publishing" ||
      this.connectorState() === "checking",
  );
  checkProblems = computed(() => {
    const state = this.checkState();
    return state.state === "problems" ? state.problems : [];
  });
  checkMessage = computed(() => {
    const state = this.checkState();
    return state.state === "error" ? state.message : "";
  });
  failureProblems = computed(() =>
    (this.failure()?.diagnostics ?? [])
      .filter((d) => d.severity !== "info")
      .slice(0, 10)
      .map(explainDiagnostic),
  );
  /**
   * Publishing can't happen from here: offline, no permission, the platform
   * lacks the built-in connector, or it refused this account. The workflow
   * can still refer to the action, to be published later.
   */
  placeholderOnly = computed(() => {
    if (!this.connected() || !this.canPublish()) return true;
    const connector = this.connectorState();
    return (
      (this.checkState().state === "connector" &&
        (connector === "missing" || connector === "error")) ||
      this.failure()?.kind === "denied"
    );
  });

  constructor() {
    // A different document starts over: earlier checks and publishes no longer apply.
    effect(() => {
      this.yaml();
      untracked(() => {
        this.generation++;
        this.checkState.set({ state: "idle" });
        this.connectorState.set("idle");
        this.published.set(this.publishedYaml.get(this.yaml()) ?? null);
        this.failure.set(null);
        this.suggestion.set(null);
        this.fileMessage.set("");
        this.publishing.set(false);
      });
    });
  }

  /**
   * The footer's one primary command: insert (or use) the published action,
   * publish it, or use it as a placeholder where publishing can't happen.
   */
  primary = computed((): "use" | "publish" | "placeholder" | "" => {
    if (!this.yaml()) return "";
    const using = this.context() !== "none" && !this.readonly();
    if (this.published()) return using ? "use" : "";
    if (this.connected() && this.canPublish() && !this.placeholderOnly())
      return "publish";
    return using ? "placeholder" : "";
  });
  /**
   * Publishing comes next but there is no action yet: the footer shows
   * "Publish action" blocked, with `pendingReason()` when pressed.
   */
  publishPending = computed(
    () =>
      !this.yaml() &&
      !this.published() &&
      this.connected() &&
      this.canPublish() &&
      !this.placeholderOnly(),
  );
  pendingReason() {
    return (
      this.candidate()?.note ??
      "Describe the request or import operations to see the action here."
    );
  }
  primaryLabel = computed(() => {
    switch (this.primary()) {
      case "use":
        return this.context() === "step"
          ? "Use in this step"
          : "Insert into workflow";
      case "publish":
        return this.publishing() ? "Publishing…" : "Publish action";
      case "placeholder":
        return this.context() === "step"
          ? "Use as a placeholder in this step"
          : "Insert as placeholder";
      default:
        return "";
    }
  });
  /** Runs the footer's primary command. */
  runPrimary() {
    switch (this.primary()) {
      case "use":
        return this.useAction(false);
      case "publish":
        return void this.publish();
      case "placeholder":
        return this.useAction(true);
    }
  }
  /** Whether this exact Action was published from this dialog. */
  isPublished(document: JsonObject | null | undefined) {
    return !!document && this.publishedYaml.has(toYaml(document));
  }
  /**
   * Puts focus back in the panel when the control that had it went away (a
   * notice that closed, a button that was replaced, a confirmation dialog
   * that doesn't return focus inside another dialog), so keyboard users
   * never land on the page behind the builder.
   */
  restoreFocus(preferred?: Element | null) {
    setTimeout(() => {
      const active = document.activeElement;
      if (active && active !== document.body) return;
      // The commands sit in the dialog's footer, outside this panel.
      const root =
        ((this.host.nativeElement as HTMLElement).closest(
          ".modal-panel",
        ) as HTMLElement | null) ?? (this.host.nativeElement as HTMLElement);
      const target =
        (preferred instanceof HTMLElement &&
        preferred.isConnected &&
        root.contains(preferred)
          ? preferred
          : null) ??
        root.querySelector<HTMLElement>(".hb-footer .primary") ??
        root.querySelector<HTMLElement>(".hb-footer .hb-check") ??
        root.querySelector<HTMLElement>(`#${this.prefix}-title`);
      target?.focus();
    });
  }

  async copy() {
    const text = this.yaml();
    try {
      await navigator.clipboard.writeText(text);
      this.fileMessage.set("Copied the YAML.");
    } catch {
      const pre = (this.host.nativeElement as HTMLElement).querySelector(
        ".hb-yaml",
      );
      const selection = window.getSelection();
      if (pre && selection) {
        const range = document.createRange();
        range.selectNodeContents(pre);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      this.fileMessage.set(
        "Studio couldn't copy automatically. The YAML is selected: copy it with your keyboard.",
      );
    }
    this.cdr.markForCheck();
  }
  download() {
    const summary = this.summary();
    if (!summary) return;
    const name = `${summary.name}-${summary.version}.action.yaml`;
    this.fileMessage.set(
      exportFile(name, "application/yaml", this.yaml()) === "downloaded"
        ? exportNotice([name])
        : "Studio couldn't start the download. Copy the YAML instead.",
    );
  }

  async runCheck() {
    if (this.busy()) return;
    await this.check();
    this.restoreFocus();
  }

  /** Full platform compile; true when the action is accepted. */
  async check(): Promise<boolean> {
    const document = this.document();
    const summary = this.summary();
    if (!document || !summary || !this.connected() || !this.canCompile())
      return false;
    const generation = this.generation;
    this.checkState.set({ state: "running" });
    try {
      const result = await this.client().compile(document);
      if (generation !== this.generation) return false;
      const diagnostics = (result.diagnostics ?? []) as DiagnosticLike[];
      if (needsConnector(diagnostics)) {
        this.checkState.set({ state: "connector" });
        void this.inspectConnector(generation);
        return false;
      }
      const problems = diagnostics
        .filter((d) => d.severity === "error")
        .map(explainDiagnostic);
      const notes = diagnostics
        .filter((d) => d.severity !== "error")
        .map(explainDiagnostic);
      if (versionTaken(diagnostics)) {
        // The project already holds this name@version: offer the next free one.
        this.versionConflict(summary);
        const others = problems.filter((p) => p.code !== IMMUTABLE_VERSION);
        this.checkState.set(
          others.length
            ? { state: "problems", problems: others, notes }
            : { state: "idle" },
        );
        return false;
      }
      this.checkState.set(
        result.ok && !problems.length
          ? { state: "ok", notes }
          : { state: "problems", problems, notes },
      );
      return result.ok && !problems.length;
    } catch (e) {
      if (generation === this.generation)
        this.checkState.set({
          state: "error",
          message: `The platform check didn't finish. ${describeError(e).message}`,
        });
      return false;
    } finally {
      this.cdr.markForCheck();
    }
  }

  private async inspectConnector(generation: number) {
    this.connectorState.set("checking");
    try {
      const descriptor = await this.client().descriptor();
      if (generation !== this.generation) return;
      this.descriptor = descriptor;
      this.connectorState.set(
        descriptor.published_version_id ? "published" : "unpublished",
      );
    } catch (e) {
      if (generation !== this.generation) return;
      this.connectorState.set(
        e instanceof ApiError && e.status === 404 ? "missing" : "error",
      );
    } finally {
      this.cdr.markForCheck();
      this.restoreFocus();
    }
  }
  private descriptor: Awaited<
    ReturnType<HttpActionClient["descriptor"]>
  > | null = null;

  /** Publishes the installed built-in connector exactly as the platform reports it. */
  async enableConnector() {
    const descriptor = this.descriptor;
    if (!descriptor || !this.canPublish() || this.busy()) return;
    const opener = document.activeElement;
    const confirmed = await this.dialogs.confirm({
      title: "Publish the built-in HTTP connector?",
      message:
        "This publishes the connector exactly as the platform installed it, so actions in this project can use it. It is needed once per project.",
      confirmLabel: "Publish connector",
    });
    this.restoreFocus(opener);
    if (!confirmed) return;
    this.connectorState.set("publishing");
    this.cdr.markForCheck();
    try {
      await this.client().publishConnector(descriptor);
      this.connectorState.set("published");
      this.checkState.set({ state: "idle" });
      await this.check();
    } catch (e) {
      this.connectorState.set("unpublished");
      this.checkState.set({
        state: "error",
        message: `The connector wasn't published. ${describeError(e).message}`,
      });
    } finally {
      this.cdr.markForCheck();
      this.restoreFocus();
    }
  }

  async publish() {
    const summary = this.summary();
    if (
      !summary ||
      !this.connected() ||
      !this.canPublish() ||
      this.busy() ||
      this.published()
    )
      return;
    const opener = document.activeElement;
    const confirmed = await this.dialogs.confirm({
      title: `Publish ${summary.name} ${summary.version}?`,
      message:
        "A published version can't be changed. Workflows in this project can use it as soon as it is published.",
      confirmLabel: "Publish action",
    });
    this.restoreFocus(opener);
    if (!confirmed) return;
    // Without the compile grant, the publish itself is the platform check.
    const accepted = !this.canCompile() || (await this.check());
    if (accepted) await this.send();
    this.restoreFocus();
  }

  /** Sends the publish; a retry after an unknown outcome reuses the same key. */
  async send() {
    const summary = this.summary();
    if (!summary) return;
    const yaml = this.yaml();
    if (this.key?.yaml !== yaml) this.key = { yaml, key: crypto.randomUUID() };
    const generation = this.generation;
    this.publishing.set(true);
    this.failure.set(null);
    this.cdr.markForCheck();
    try {
      const version = await this.client().publishAction(yaml, this.key.key);
      if (generation !== this.generation) return;
      this.published.set(version);
      this.publishedYaml.set(yaml, version);
      this.publishedAction.emit({
        id: version.id,
        name: version.name,
        version: version.version,
        uses: `${version.name}@${version.version}`,
      });
    } catch (e) {
      if (generation !== this.generation) return;
      const failure = classifyPublishError(e);
      if (failure.kind === "key") this.key = null;
      if (
        failure.kind === "compile" &&
        needsConnector(failure.diagnostics ?? [])
      ) {
        // Without the compile grant the publish is the first check: explain readiness, not compiler text.
        this.checkState.set({ state: "connector" });
        void this.inspectConnector(generation);
        return;
      }
      if (failure.kind === "version") {
        this.versionConflict(summary, failure.code);
        return;
      }
      this.failure.set(failure);
    } finally {
      if (generation === this.generation) this.publishing.set(false);
      this.cdr.markForCheck();
    }
  }
  async checkAgain() {
    if (this.busy()) return;
    await this.send();
    this.restoreFocus();
  }
  private versionConflict(
    summary: { name: string; version: string },
    code?: string,
  ) {
    this.suggestion.set(
      suggestVersion(summary.name, summary.version, this.publishedVersions()),
    );
    this.failure.set({
      kind: "version",
      message: `Version ${summary.version} of ${summary.name} is already published with different content. Published versions can't change.`,
      ...(code ? { code } : {}),
    });
  }
  bumpVersion() {
    const next = this.suggestion();
    if (next) this.bump.emit(next);
  }
  useAction(placeholder: boolean) {
    const candidate = this.candidate();
    const document = this.document();
    const summary = this.summary();
    if (!candidate || !document || !summary) return;
    this.use.emit({
      uses: summary.uses,
      action: document,
      connector: HTTP_CONNECTOR,
      slot: candidate.slot,
      placeholder,
      connection: {
        name: candidate.slot,
        baseUrl: candidate.origin || null,
        auth: candidate.auth,
      },
    });
  }
}
