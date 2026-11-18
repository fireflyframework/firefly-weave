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
// Operate › Logs: log entries across the runs of this environment, newest
// first, from the last hour and Warning up, without test runs, until the
// person changes the filters. Every filter but the message lives in the
// address; the message stays in this page and is sent only for 6 hours or
// less. Follow reads the newest entries every 10 s while the page is
// visible; Load older entries continues with the first page's own query.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  ElementRef,
  Injector,
  Input,
  OnDestroy,
  afterNextRender,
  computed,
  inject,
  signal,
  viewChild,
} from "@angular/core";
import { Router } from "@angular/router";
import type { App } from "../../app";
import { describeError } from "../../errors";
import { Icon } from "../../icon";
import { environmentOf, scopeKeyOf } from "../operate-list";
import {
  isLogCode,
  isStepName,
  logsPath,
  logsRoute,
  type LogsLevel,
  type LogsRange,
  type LogsRoute,
} from "../operate-routes";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import { RefreshStatus } from "../refresh-status";
import { asRunViewsApi } from "../run-contracts";
import { personLearned } from "../runs/person-scope";
import { LogEntries } from "./log-entries";
import {
  FOLLOW_MS,
  countText,
  executionLogQuery,
  filterSummary,
  levelChoices,
  rangeChoices,
  searchProblem,
  stopsReads,
} from "./logs-model";
import { LogSearchStore } from "./logs-store";

/** How long typing in the message filter pauses before it is sent. */
const TYPING_MS = 400;
const NAME_PROBLEM =
  "Use letters, digits, dots, dashes and underscores, starting with a letter or digit.";

@Component({
  selector: "weave-logs-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, LogEntries, OperateState, RefreshStatus],
  styleUrls: ["../operate.css", "./logs.css"],
  host: { class: "logs-page" },
  template: `@let s = store.state();
    <div class="operate-heading">
      <div>
        <h1>Logs</h1>
        <p class="subtitle">Log entries from every run in this environment.</p>
      </div>
      @if (searchable()) {
        <div class="actions">
          @if (following()) {
            <span class="logs-follow"
              ><span class="live-dot" aria-hidden="true"></span>Following</span
            >
          }
          <weave-refresh-status
            [updatedAt]="store.updatedAt()"
            [busy]="poller.busy"
            (refresh)="refreshNow()"
          />
        </div>
      }
    </div>
    @if (!host.profile) {
      <div class="empty-state">
        <h2>Logs appear here once you connect</h2>
        <p>Log entries are written by a platform's workers as steps run.</p>
        <button class="primary" (click)="host.openWizard('server')">
          Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded || host.platformSignedOut) {
      <div class="empty-state">
        <h2>Sign in to see logs</h2>
        <p>
          {{
            host.signInEnded
              ? "Studio shows them as soon as you're signed in again."
              : "You're signed out of " + host.profile.name + "."
          }}
        </p>
        <button
          type="button"
          class="primary"
          (click)="host.signInPlatform(host.profile.name, false)"
        >
          Sign in
        </button>
      </div>
    } @else if (!host.identity) {
      <weave-operate-state kind="loading" noun="log entries" [rows]="6" />
    } @else if (!readable || s.kind === "refused") {
      <p class="logs-access">
        <weave-icon name="lock" [size]="16" /><span
          >You need <code>run.read</code> in {{ environmentName() }}. Ask an
          administrator in Settings › People and access.</span
        >
      </p>
    } @else if (s.kind === "unserved") {
      <div class="logs-empty">
        <span class="badge"><weave-icon name="info" [size]="20" /></span>
        <h2 class="empty-title">This platform can't show logs yet.</h2>
      </div>
    } @else {
      <div class="logs-search" [class.open]="filtersOpen()">
        <div class="filter-summary">
          <p class="summary-text"><b>Filters:</b> {{ summary() }}</p>
          <button
            type="button"
            aria-controls="logs-filters"
            [attr.aria-expanded]="filtersOpen()"
            (click)="toggleFilters()"
          >
            Change filters
          </button>
        </div>
        <div
          class="logs-filters"
          id="logs-filters"
          role="search"
          aria-label="Filter log entries"
        >
          <div class="filters">
            <div class="field">
              <label for="logs-time">Time</label>
              <select
                #time
                id="logs-time"
                (change)="setRange($any(value($event)))"
              >
                @for (choice of ranges; track choice.value) {
                  <option
                    [value]="choice.value"
                    [selected]="route().range === choice.value"
                  >
                    {{ choice.label }}
                  </option>
                }
              </select>
            </div>
            <div class="field">
              <label for="logs-level">Level</label>
              <select id="logs-level" (change)="setLevel($any(value($event)))">
                @for (choice of levels; track choice.value) {
                  <option
                    [value]="choice.value"
                    [selected]="route().level === choice.value"
                  >
                    {{ choice.label }}
                  </option>
                }
              </select>
            </div>
            <div class="field">
              <label for="logs-workflow">Workflow</label>
              <select id="logs-workflow" (change)="setWorkflow(value($event))">
                <option value="" [selected]="!route().workflow">
                  Any workflow
                </option>
                @for (name of workflowNames(); track name) {
                  <option [value]="name" [selected]="route().workflow === name">
                    {{ name }}
                  </option>
                }
              </select>
            </div>
            <div class="field">
              <label for="logs-step">Step</label>
              <input
                #step
                id="logs-step"
                placeholder="Any step"
                autocomplete="off"
                spellcheck="false"
                [value]="route().step"
                [attr.aria-invalid]="stepProblem() ? 'true' : null"
                [attr.aria-describedby]="
                  stepProblem() ? 'logs-step-problem' : null
                "
                (change)="setStep(value($event))"
              />
              @if (stepProblem()) {
                <p class="field-error" id="logs-step-problem" role="alert">
                  <weave-icon name="failCircle" [size]="16" />{{
                    stepProblem()
                  }}
                </p>
              }
            </div>
            <div class="field">
              <label for="logs-code">Code</label>
              <input
                #code
                id="logs-code"
                placeholder="Any code"
                autocomplete="off"
                spellcheck="false"
                [value]="route().code"
                [attr.aria-invalid]="codeProblem() ? 'true' : null"
                [attr.aria-describedby]="
                  codeProblem() ? 'logs-code-problem' : null
                "
                (change)="setCode(value($event))"
              />
              @if (codeProblem()) {
                <p class="field-error" id="logs-code-problem" role="alert">
                  <weave-icon name="failCircle" [size]="16" />{{
                    codeProblem()
                  }}
                </p>
              }
            </div>
            <div class="field wide">
              <label for="logs-message">Message contains</label>
              <span class="search-input"
                ><weave-icon name="search" [size]="16" /><input
                  id="logs-message"
                  type="search"
                  placeholder="Text in the message"
                  autocomplete="off"
                  spellcheck="false"
                  [value]="draft()"
                  [attr.aria-invalid]="messageProblem() ? 'true' : null"
                  [attr.aria-describedby]="
                    messageProblem() ? 'logs-message-problem' : null
                  "
                  (input)="setMessage(value($event))"
              /></span>
              @if (messageProblem()) {
                <p class="field-error" id="logs-message-problem" role="alert">
                  <weave-icon name="failCircle" [size]="16" />{{
                    messageProblem()
                  }}
                </p>
              }
            </div>
          </div>
          <div class="filterbar">
            <label class="checkbox-field"
              ><input
                type="checkbox"
                [checked]="route().test"
                (change)="setTest(checked($event))"
              />Include test runs</label
            >
            <label class="checkbox-field"
              ><input
                type="checkbox"
                role="switch"
                [checked]="following()"
                (change)="setFollow(checked($event))"
              />Follow</label
            >
            <span class="hint"
              >Follow asks every 10 seconds while this page is open.</span
            >
          </div>
        </div>
      </div>
      @switch (s.kind) {
        @case ("loading") {
          <div class="entries loading with-run" role="status">
            <span class="sr-only">Loading log entries…</span>
            @for (row of [0, 1, 2, 3]; track row) {
              <div class="entry" aria-hidden="true">
                <span class="bar"></span><span class="bar"></span
                ><span class="bar mid"></span><span class="bar mid"></span
                ><span class="bar mid"></span><span class="bar wide msg"></span>
              </div>
            }
          </div>
        }
        @case ("slow") {
          <div class="notice logs-notice" role="alert">
            <p><weave-icon name="stale" [size]="16" />{{ problem() }}</p>
            <button type="button" class="sm" (click)="refreshNow()">
              Try again
            </button>
          </div>
        }
        @case ("withheld") {
          <weave-operate-state
            kind="error"
            message="This run's details are withheld."
            (retry)="refreshNow()"
          />
        }
        @case ("error") {
          @if (!store.rows().length) {
            <weave-operate-state
              kind="error"
              [message]="'Studio could not load the logs. ' + problem()"
              [code]="problemCode()"
              (retry)="refreshNow()"
            />
          } @else {
            <div class="notice logs-notice" data-tone="danger" role="alert">
              <p>Studio could not refresh the logs. {{ problem() }}</p>
              @if (problemCode()) {
                <small class="support-code"
                  >Support code: {{ problemCode() }}</small
                >
              }
              <button type="button" class="sm" (click)="refreshNow()">
                Try again
              </button>
            </div>
          }
        }
      }
      @if (store.rows().length) {
        <weave-log-entries
          [rows]="store.rows()"
          [withRun]="true"
          (openRun)="openRun($event)"
        />
        <div class="logs-footer">
          @if (store.more()) {
            <button
              type="button"
              [attr.aria-disabled]="store.appending() ? 'true' : null"
              (click)="store.loadOlder()"
            >
              Load older entries
            </button>
          }
          @if (store.limited()) {
            <p class="hint" role="status">
              Showing the newest {{ store.rows().length }} entries. Narrow the
              filters to see others.
            </p>
          } @else {
            <p class="hint">{{ count() }}</p>
          }
        </div>
      } @else if (s.kind === "ready") {
        <div class="logs-empty">
          <span class="badge"><weave-icon name="logs" [size]="20" /></span>
          <h2 class="empty-title">No log entries match.</h2>
          <p>
            Workers write entries while steps run; entries are kept for 7 days.
          </p>
        </div>
      }
    }`,
  styles: `
    .logs-search {
      margin-bottom: var(--group-gap);
    }
    /* Follow is on: the live dot of the editor's Runs tab, never animated. */
    .logs-follow {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font: var(--type-caption);
      color: var(--text);
    }
    .live-dot {
      flex: none;
      width: 8px;
      height: 8px;
      border-radius: var(--radius-pill);
      background: var(--live);
      box-shadow: 0 0 0 4px var(--live-glow);
    }
    .filters {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(168px, 100%), 1fr));
      gap: var(--group-gap);
      align-items: start;
      margin-bottom: var(--group-gap);
    }
    .filters .wide {
      grid-column: span 2;
    }
    .filters :is(select, input) {
      width: 100%;
      min-height: var(--target);
    }
    .filters input[aria-invalid="true"] {
      border-color: var(--danger);
    }
    .search-input {
      position: relative;
      display: block;
    }
    .search-input weave-icon {
      position: absolute;
      top: 50%;
      left: var(--space-3);
      transform: translateY(-50%);
      color: var(--subtle);
      pointer-events: none;
    }
    .search-input input {
      padding-left: calc(var(--space-3) + var(--space-6));
    }
    .filterbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-4);
    }
    .filterbar .checkbox-field {
      min-height: var(--target);
      font: var(--type-small);
      color: var(--text);
    }
    .filter-summary {
      display: none;
    }
    .summary-text {
      margin: 0;
      font: var(--type-small);
      color: var(--text);
    }
    .summary-text b {
      font-weight: 600;
    }
    .logs-empty h2.empty-title {
      font: var(--type-title-sm);
    }
    weave-operate-state,
    .logs-notice {
      display: block;
      margin-bottom: var(--group-gap);
    }
    .logs-notice {
      display: flex;
    }
    .logs-footer {
      margin-top: var(--group-gap);
    }
    @media (max-width: 767px) {
      .filter-summary {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: var(--space-2);
        margin-bottom: var(--group-gap);
      }
      .filter-summary button {
        min-height: var(--control-touch);
      }
      .logs-search:not(.open) .logs-filters {
        display: none;
      }
      .filters {
        grid-template-columns: minmax(0, 1fr);
      }
      .filters .wide {
        grid-column: auto;
      }
    }
  `,
})
export class LogsPage implements DoCheck, OnDestroy {
  @Input({ required: true }) host!: App;
  private router = inject(Router);
  private injector = inject(Injector);
  private time = viewChild<ElementRef<HTMLSelectElement>>("time");
  private stepField = viewChild<ElementRef<HTMLInputElement>>("step");
  private codeField = viewChild<ElementRef<HTMLInputElement>>("code");
  readonly levels = levelChoices;
  readonly ranges = rangeChoices;
  readonly store = new LogSearchStore(
    {
      api: () => asRunViewsApi(this.host.api),
      scope: () => scopeKeyOf(this.host),
    },
    undefined,
    (error) => this.signInGone(error),
  );
  /** Follow: the newest entries every 10 s while the page is visible. */
  readonly poller = new Poller(() => this.followRead(), FOLLOW_MS);
  readonly route = signal<LogsRoute>(logsRoute(location.search));
  /** What the message field holds, and what was last sent. */
  readonly draft = signal("");
  private readonly contains = signal("");
  readonly following = signal(false);
  readonly filtersOpen = signal(false);
  readonly stepProblem = signal("");
  readonly codeProblem = signal("");
  readonly messageProblem = computed(() =>
    searchProblem(this.route().range, this.draft()),
  );
  readonly summary = computed(() =>
    filterSummary(this.route(), this.draft(), this.following()),
  );
  readonly count = computed(() =>
    countText(this.store.rows().length, this.route().range, this.store.more()),
  );
  private scopeKey = "";
  private search = location.search;
  private typing: ReturnType<typeof setTimeout> | null = null;

  /** With the person known, only with run.read anywhere in the workspace. */
  get readable() {
    return this.host.canAnywhere("run.read");
  }
  /** The filters and the entries show: connected, signed in, allowed, served. */
  searchable() {
    const kind = this.store.state().kind;
    return (
      !!this.host.profile &&
      !this.host.signInEnded &&
      !this.host.platformSignedOut &&
      !!this.host.identity &&
      this.readable &&
      kind !== "refused" &&
      kind !== "unserved"
    );
  }
  ngDoCheck() {
    // Back and Forward change the filters without this page.
    if (
      location.pathname === "/operate/logs" &&
      location.search !== this.search
    ) {
      this.search = location.search;
      const route = logsRoute(location.search);
      if (logsPath(route) !== logsPath(this.route())) {
        this.route.set(route);
        this.stepProblem.set("");
        this.codeProblem.set("");
        // A text field may still hold what was typed (a refused name, or a
        // change not drawn yet); its binding would see no change to draw.
        this.showText(this.stepField(), route.step);
        this.showText(this.codeField(), route.code);
        void this.read();
      }
    }
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    // Another platform, environment or person: nothing read before stays,
    // and nothing typed before is sent, unless only the same session's
    // person became known.
    const learned = personLearned(this.scopeKey, key);
    this.scopeKey = key;
    if (!learned) this.forgetTyped();
    this.store.forget();
    if (environmentOf(this.host) && this.host.identity && this.readable)
      void this.read();
  }
  /** What was typed for the person before: the message and refused names. */
  private forgetTyped() {
    if (this.typing) clearTimeout(this.typing);
    this.typing = null;
    this.draft.set("");
    this.contains.set("");
    this.stepProblem.set("");
    this.codeProblem.set("");
    this.showText(this.stepField(), this.route().step);
    this.showText(this.codeField(), this.route().code);
  }
  private showText(
    field: ElementRef<HTMLInputElement> | undefined,
    text: string,
  ) {
    if (field && field.nativeElement.value !== text)
      field.nativeElement.value = text;
  }
  ngOnDestroy() {
    this.poller.stop();
    if (this.typing) clearTimeout(this.typing);
    this.store.forget();
  }
  /** New filters: a new first page, unless the message can't be sent as it is. */
  private read() {
    if (searchProblem(this.route().range, this.contains())) {
      this.store.forget();
      return Promise.resolve("dropped");
    }
    return this.store
      .search(executionLogQuery(this.route(), this.contains(), new Date()))
      .then((result) => {
        this.stopIfBlocked();
        return result;
      });
  }
  /** Follow stops once no later read can change what the page shows. */
  private stopIfBlocked() {
    if (!stopsReads(this.store.state().kind)) return;
    this.poller.stop();
    this.following.set(false);
  }
  /** Follow, Refresh and Try again: the newest page of the same filters. */
  private async followRead(): Promise<boolean> {
    if (!environmentOf(this.host) || !this.host.identity || !this.readable)
      return false;
    if (searchProblem(this.route().range, this.contains())) return false;
    const result = await this.store.follow(
      executionLogQuery(this.route(), this.contains(), new Date()),
    );
    this.stopIfBlocked();
    if (result === "failed") throw new Error("The logs could not be read.");
    return result === "applied";
  }
  refreshNow() {
    void this.poller.refresh();
  }
  /** An ended sign-in is the shell's to explain, with its own Sign in again. */
  private signInGone(error: unknown) {
    const plain = describeError(error);
    if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
      this.host.fail(error);
  }

  // ---------------------------------------------------------------- filters

  private go(route: LogsRoute) {
    this.route.set(route);
    const path = logsPath(route);
    this.search = path.includes("?") ? path.slice(path.indexOf("?")) : "";
    if (location.pathname + location.search !== path)
      void this.router.navigateByUrl(path);
    void this.read();
  }
  setRange(range: LogsRange) {
    if (range !== this.route().range) this.go({ ...this.route(), range });
  }
  setLevel(level: LogsLevel) {
    if (level !== this.route().level) this.go({ ...this.route(), level });
  }
  setWorkflow(workflow: string) {
    if (workflow !== this.route().workflow)
      this.go({ ...this.route(), workflow });
  }
  setStep(text: string) {
    const step = text.trim();
    this.stepProblem.set(step && !isStepName(step) ? NAME_PROBLEM : "");
    if (!this.stepProblem() && step !== this.route().step)
      this.go({ ...this.route(), step });
  }
  setCode(text: string) {
    const code = text.trim();
    this.codeProblem.set(code && !isLogCode(code) ? NAME_PROBLEM : "");
    if (!this.codeProblem() && code !== this.route().code)
      this.go({ ...this.route(), code });
  }
  setTest(test: boolean) {
    if (test !== this.route().test) this.go({ ...this.route(), test });
  }
  /** The message is sent after a pause in typing, and never kept elsewhere. */
  setMessage(text: string) {
    this.draft.set(text);
    if (this.typing) clearTimeout(this.typing);
    this.typing = setTimeout(() => {
      this.typing = null;
      if (this.draft().trim() === this.contains().trim()) return;
      this.contains.set(this.draft());
      void this.read();
    }, TYPING_MS);
  }
  setFollow(on: boolean) {
    this.following.set(on);
    if (on) this.poller.start(true);
    else this.poller.stop();
  }
  /** On a phone the filters fold; opening them moves focus to Time. */
  toggleFilters() {
    const open = !this.filtersOpen();
    this.filtersOpen.set(open);
    if (open)
      afterNextRender(() => this.time()?.nativeElement.focus(), {
        injector: this.injector,
      });
  }
  /** Workflow names this person has seen: published versions and listed entries. */
  workflowNames() {
    const names = new Set<string>();
    for (const version of this.host.workflowVersions.values())
      if (version.name) names.add(version.name);
    for (const row of this.store.rows())
      if (row.kind === "entry" && row.run) names.add(row.run.workflow);
    if (this.route().workflow) names.add(this.route().workflow);
    return [...names].sort();
  }
  openRun(id: string) {
    void this.host.viewRun({ id });
  }
  environmentName() {
    return this.host.workspaceNames?.environment || "this environment";
  }
  problem() {
    const state = this.store.state();
    return state.kind === "slow" || state.kind === "error"
      ? state.error.message
      : "";
  }
  problemCode() {
    const state = this.store.state();
    return state.kind === "error" ? state.error.code : "";
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
}
