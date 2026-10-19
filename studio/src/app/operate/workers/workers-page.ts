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
// Operate › Workers: the fleet (every current worker with its presence, where
// it runs, its release, load and contact) and its releases. The platform
// applies the filters; the address keeps them, except a host prefix, which
// stays on the page. Selected workers drain, resume or are revoked one after
// another, at most 50, each with its own answer. Retire offline workers
// previews what it would revoke before it does. The fleet reloads every 10 s
// while visible; the server checks every command.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  HostListener,
  Input,
  OnDestroy,
  OnInit,
  inject,
} from "@angular/core";
import { NavigationEnd, Router } from "@angular/router";
import type { App } from "../../app";
import { Modal } from "../../dialog";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import type { Page, Target } from "../../operations/deployment-contracts";
import { toneAttribute } from "../../status-labels";
import { MAX_PAGES } from "../operate-limits";
import {
  ListReads,
  environmentOf,
  listFailure,
  scopeKeyOf,
} from "../operate-list";
import { OperateState } from "../operate-state";
import {
  releasesPath,
  viewFromPath,
  viewPath,
  workersRoute,
} from "../operate-routes";
import { Poller } from "../operate-store";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { RefreshStatus } from "../refresh-status";
import { ReleasesPage } from "./releases-page";
import { WorkerDetail } from "./worker-detail";
import {
  RETIRE_BATCH,
  filterChoices,
  filterCount,
  filtersFromQuery,
  filtersToQuery,
  isUnavailableWorker,
  noWorkerFilters,
  releaseTaskTypes,
  retireSummary,
  retireThresholds,
  shortDigest,
  workerBadges,
  workerLoad,
  workerQuery,
  workerVersion,
  workerWhere,
  type Presence,
  type WorkerFilters,
  type WorkerRecord,
  type WorkerRelease,
  type WorkerStatus,
} from "./workers-model";

/** The most workers one bulk action takes, one after another. */
const BULK_LIMIT = 50;
type BulkAction = "drain" | "resume" | "revoke";
interface BulkResult {
  id: string;
  ok: boolean;
  text: string;
}
interface RetireAnswer {
  matched: number;
  revoked: number;
  worker_ids: string[];
}
/** The Retire offline workers dialog while it is open. */
interface RetireState {
  seconds: number;
  /** The last preview (dry run) for these seconds. */
  preview: RetireAnswer | null;
  /** The last retirement's answer. */
  result: RetireAnswer | null;
  busy: boolean;
  problem: PlainError | null;
  /**
   * The key of a retirement whose answer was lost: sending it again reuses
   * the key, so the platform replays the first answer.
   */
  pendingKey: string | null;
}

@Component({
  selector: "weave-workers-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    Modal,
    ModalSheet,
    OperateState,
    RefreshStatus,
    ReleasesPage,
    WorkerDetail,
  ],
  styleUrls: ["../operate.css", "./workers.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1>Workers</h1>
        <p class="subtitle">
          {{
            tab === "releases"
              ? "Worker releases in this environment and what runs them."
              : "Programs that run your worker actions in this environment."
          }}
        </p>
      </div>
      @if (tab === "fleet" && readable && loaded) {
        <div class="actions">
          <weave-refresh-status
            [updatedAt]="poller.lastSuccessAt"
            [busy]="poller.busy"
            (refresh)="poller.refresh()"
          />
          @if (canRetire()) {
            <button type="button" (click)="openRetire()">
              <weave-icon name="revoke" [size]="16" />Retire offline workers…
            </button>
          }
        </div>
      }
    </div>
    @if (host.profile && !host.signInEnded) {
      <div class="operate-tabs" role="tablist" aria-label="Workers">
        <button
          type="button"
          role="tab"
          id="workers-tab-fleet"
          aria-controls="workers-panel"
          [attr.aria-selected]="tab === 'fleet'"
          [attr.tabindex]="tab === 'fleet' ? 0 : -1"
          (click)="showTab('fleet')"
          (keydown)="tabKey($event)"
        >
          Fleet
        </button>
        <button
          type="button"
          role="tab"
          id="workers-tab-releases"
          aria-controls="workers-panel"
          [attr.aria-selected]="tab === 'releases'"
          [attr.tabindex]="tab === 'releases' ? 0 : -1"
          (click)="showTab('releases')"
          (keydown)="tabKey($event)"
        >
          Releases
        </button>
      </div>
    }
    <div
      id="workers-panel"
      [attr.role]="host.profile && !host.signInEnded ? 'tabpanel' : null"
      [attr.aria-labelledby]="
        host.profile && !host.signInEnded
          ? tab === 'releases'
            ? 'workers-tab-releases'
            : 'workers-tab-fleet'
          : null
      "
    >
      @if (!host.profile) {
        <div class="empty-state">
          <h2>Workers appear here once you connect</h2>
          <p>Workers run your worker actions on a platform.</p>
          <button class="primary" (click)="host.openWizard('server')">
            <weave-icon name="cloud" [size]="16" />Connect to a platform
          </button>
        </div>
      } @else if (host.signInEnded) {
        <div class="empty-state">
          <h2>Sign in to see workers</h2>
          <p>Studio shows them as soon as you're signed in again.</p>
        </div>
      } @else if (tab === "releases") {
        <weave-releases-page [host]="host" />
      } @else if (!host.identity) {
        <weave-operate-state kind="loading" noun="workers" />
      } @else if (!readable || forbidden) {
        <weave-operate-state kind="access" capability="status.read" />
      } @else if (!loaded && error) {
        <weave-operate-state
          kind="error"
          [message]="error.message"
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      } @else if (!loaded) {
        <weave-operate-state kind="loading" noun="workers" />
      } @else {
        @let choices = choicesOf();
        <button
          type="button"
          class="filters-toggle"
          aria-controls="worker-filter-body"
          [attr.aria-expanded]="filtersOpen"
          (click)="filtersOpen = !filtersOpen"
        >
          <weave-icon name="search" [size]="16" />Filters ({{
            filterCount(filters)
          }})
        </button>
        <div
          class="operate-filters"
          id="worker-filter-body"
          role="search"
          aria-label="Filter workers"
          [class.open]="filtersOpen"
        >
          <div class="field">
            <label for="worker-presence">Status</label>
            <select
              id="worker-presence"
              (change)="setFilter('presence', value($event))"
            >
              @for (option of presenceOptions; track option[0]) {
                <option
                  [value]="option[0]"
                  [selected]="filters.presence === option[0]"
                >
                  {{ option[1] }}
                </option>
              }
            </select>
          </div>
          <div class="field">
            <label for="worker-draining">Draining</label>
            <select id="worker-draining" (change)="setDraining(value($event))">
              <option value="" [selected]="filters.draining === null">
                Draining or not
              </option>
              <option value="true" [selected]="filters.draining === true">
                Draining
              </option>
              <option value="false" [selected]="filters.draining === false">
                Not draining
              </option>
            </select>
          </div>
          <div class="field">
            <label for="worker-release">Release</label>
            <select
              id="worker-release"
              (change)="setFilter('release', value($event))"
            >
              <option value="" [selected]="!filters.release">
                Any release
              </option>
              @for (release of choices.releases; track release) {
                <option
                  [value]="release"
                  [selected]="filters.release === release"
                >
                  {{ releaseName(release) }}
                </option>
              }
            </select>
          </div>
          <div class="field">
            <label for="worker-task-type">Task type</label>
            <select
              id="worker-task-type"
              (change)="setFilter('taskType', value($event))"
            >
              <option value="" [selected]="!filters.taskType">
                Any task type
              </option>
              @for (type of choices.taskTypes; track type) {
                <option [value]="type" [selected]="filters.taskType === type">
                  {{ type }}
                </option>
              }
            </select>
          </div>
          @if (targets.length || filters.target) {
            <div class="field">
              <label for="worker-target">Target</label>
              <select
                id="worker-target"
                (change)="setFilter('target', value($event))"
              >
                <option value="" [selected]="!filters.target">
                  Any target
                </option>
                @for (target of targetChoices(); track target.id) {
                  <option
                    [value]="target.id"
                    [selected]="filters.target === target.id"
                  >
                    {{ target.name }}
                  </option>
                }
              </select>
            </div>
          }
          <div class="field host-field">
            <label for="worker-host">Host starts with</label>
            <input
              id="worker-host"
              autocomplete="off"
              spellcheck="false"
              [value]="filters.hostPrefix"
              (input)="setHost(value($event))"
            />
          </div>
          <div class="field">
            <label for="worker-order">Order</label>
            <select
              id="worker-order"
              (change)="setFilter('order', value($event))"
            >
              <option
                value="last_seen_desc"
                [selected]="filters.order === 'last_seen_desc'"
              >
                Last contact
              </option>
              <option
                value="registered_desc"
                [selected]="filters.order === 'registered_desc'"
              >
                Newest registered
              </option>
            </select>
          </div>
          <label class="checkbox-field"
            ><input
              type="checkbox"
              [checked]="filters.revoked"
              (change)="setFilter('revoked', checked($event))"
            />Show retired workers</label
          >
          <span class="operate-count" role="status">{{ countText() }}</span>
          @if (filterCount(filters)) {
            <button type="button" class="tertiary" (click)="clearFilters()">
              Clear filters ({{ filterCount(filters) }})
            </button>
          }
        </div>
        @if (error) {
          <weave-operate-state
            kind="partial"
            [message]="error.message"
            [code]="error.code"
            (retry)="poller.refresh()"
          />
        }
        @if (detailError; as failed) {
          @if (failed.error.status === 403) {
            <weave-operate-state
              kind="access"
              heading="You don't have access to this worker"
              capability="status.read"
            />
          } @else {
            <weave-operate-state
              kind="error"
              [message]="failed.error.message"
              [code]="failed.error.code"
              (retry)="retryDetail(failed.id)"
            />
          }
        }
        @if (selection.size) {
          <div class="bulk-bar" role="region" aria-label="Selected workers">
            <span class="bulk-count numeric" role="status"
              >{{ selection.size }} selected</span
            >
            @if (canDrainSelected()) {
              <button
                type="button"
                [disabled]="bulkRunning"
                (click)="bulkAction('drain')"
              >
                <weave-icon name="drain" [size]="16" />Drain
              </button>
              <button
                type="button"
                [disabled]="bulkRunning"
                (click)="bulkAction('resume')"
              >
                <weave-icon name="play" [size]="16" />Resume
              </button>
            }
            @if (canRetire()) {
              <button
                type="button"
                class="danger"
                [disabled]="bulkRunning"
                (click)="bulkAction('revoke')"
              >
                <weave-icon name="revoke" [size]="16" />Revoke
              </button>
            }
            <button
              type="button"
              class="tertiary"
              [disabled]="bulkRunning"
              (click)="selection = emptySelection()"
            >
              Clear selection
            </button>
          </div>
        }
        @if (bulk; as outcome) {
          <section
            class="bulk-results"
            aria-labelledby="worker-bulk-results-title"
          >
            <div class="bulk-results-head">
              <h2 id="worker-bulk-results-title">{{ outcome.title }}</h2>
              @if (!bulkRunning) {
                <button type="button" class="tertiary" (click)="bulk = null">
                  Dismiss
                </button>
              }
            </div>
            <ul role="list">
              @for (result of outcome.results; track result.id) {
                <li class="bulk-result" [attr.data-ok]="result.ok">
                  <weave-icon
                    [name]="result.ok ? 'ok' : 'degraded'"
                    [size]="16"
                  /><code>{{ short(result.id) }}</code
                  ><span>{{ result.text }}</span>
                </li>
              }
            </ul>
          </section>
        }
        @if (!workers.length) {
          @if (filterCount(filters)) {
            <weave-operate-state
              kind="empty"
              heading="No workers match these filters"
              text="Change or clear the filters to see more workers."
            >
              <button type="button" (click)="clearFilters()">
                Clear filters
              </button>
            </weave-operate-state>
          } @else {
            <weave-operate-state
              kind="empty"
              heading="No current workers"
              text="Workers appear when a worker release starts in this environment. Show retired workers to see revoked and long-offline ones."
            />
          }
        } @else {
          <div class="resource-layout" [class.has-detail]="!!selected">
            <div class="worker-list">
              <div
                class="resource-table worker-table"
                [class.selectable]="canSelect()"
                role="table"
                aria-label="Workers"
              >
                <div role="rowgroup">
                  <div class="table-head" role="row">
                    @if (canSelect()) {
                      <span role="columnheader" class="cell-select"
                        ><label class="select-box"
                          ><input
                            type="checkbox"
                            aria-label="Select every worker on this list, up to 50"
                            [checked]="allSelected()"
                            (change)="selectAll(checked($event))" /></label
                      ></span>
                    }
                    <span role="columnheader">Worker</span
                    ><span role="columnheader">Status</span
                    ><span role="columnheader" data-wide>Where</span
                    ><span role="columnheader" data-wide>Release</span
                    ><span role="columnheader" data-wider>Version</span
                    ><span role="columnheader" class="cell-load">Load</span
                    ><span role="columnheader" data-wide>Last contact</span
                    ><span role="columnheader" data-wider>Registered</span>
                  </div>
                </div>
                <div role="rowgroup">
                  @for (worker of workers; track worker.id) {
                    @if (unavailable(worker)) {
                      <div class="resource-row" role="row">
                        @if (canSelect()) {
                          <span role="cell" class="cell-select"></span>
                        }
                        <span role="cell" class="row-text"
                          ><strong>Unavailable worker</strong
                          ><small>Studio can't show this worker.</small></span
                        ><span role="cell"></span
                        ><span role="cell" data-wide></span
                        ><span role="cell" data-wide></span
                        ><span role="cell" data-wider></span
                        ><span role="cell" class="cell-load"></span
                        ><span role="cell" data-wide></span
                        ><span role="cell" data-wider></span>
                      </div>
                    } @else {
                      @let w = status(worker);
                      @let load = loadOf(w);
                      <div
                        class="resource-row"
                        role="row"
                        [class.selected]="selected?.id === w.id"
                      >
                        @if (canSelect()) {
                          <span role="cell" class="cell-select"
                            ><label class="select-box"
                              ><input
                                type="checkbox"
                                [attr.aria-label]="
                                  'Select worker ' + short(w.id)
                                "
                                [checked]="selection.has(w.id)"
                                [disabled]="
                                  !selection.has(w.id) &&
                                  selection.size >= bulkLimit
                                "
                                (change)="
                                  select(w.id, checked($event))
                                " /></label
                          ></span>
                        }
                        <span role="cell" class="row-text"
                          ><button
                            type="button"
                            class="row-open"
                            [attr.aria-current]="
                              selected?.id === w.id ? 'true' : null
                            "
                            (click)="open(w)"
                          >
                            Worker {{ short(w.id) }}</button
                          ><small [attr.title]="w.identity?.host ?? null">{{
                            w.identity?.host ?? "No host reported"
                          }}</small></span
                        ><span role="cell" class="badges">
                          @for (badge of badges(w); track badge.label) {
                            <span
                              class="status-pill"
                              [attr.data-tone]="tone(badge.tone)"
                              >{{ badge.label }}</span
                            >
                          }</span
                        ><span
                          role="cell"
                          data-wide
                          class="cell-text"
                          [attr.title]="where(w)"
                          >{{ where(w) }}</span
                        ><span role="cell" data-wide class="row-text"
                          ><code [attr.title]="w.release_id">{{
                            short(w.release_id)
                          }}</code
                          ><small>{{ typesText(w) }}</small></span
                        ><span
                          role="cell"
                          data-wider
                          class="cell-text"
                          [attr.title]="version(w) || null"
                          >{{ version(w) || "Unknown" }}</span
                        ><span role="cell" class="cell-load"
                          ><span
                            class="meter"
                            role="img"
                            [class.over]="load.over"
                            [attr.aria-label]="loadLabel(load.text)"
                            ><span class="meter-track" aria-hidden="true"
                              ><span
                                class="meter-fill"
                                [style.width.%]="load.fill * 100"
                              ></span></span
                            ><span class="meter-text" aria-hidden="true">{{
                              load.text
                            }}</span></span
                          ></span
                        ><span role="cell" data-wide>
                          @if (w.last_seen_at) {
                            <time
                              [attr.datetime]="iso(w.last_seen_at)"
                              [attr.title]="absolute(w.last_seen_at)"
                              >{{ relative(w.last_seen_at) }}</time
                            >
                          } @else {
                            Never
                          }</span
                        ><span role="cell" data-wider>
                          @if (w.registered_at) {
                            <time
                              [attr.datetime]="iso(w.registered_at)"
                              [attr.title]="absolute(w.registered_at)"
                              >{{ relative(w.registered_at) }}</time
                            >
                          } @else {
                            Unknown
                          }
                        </span>
                      </div>
                    }
                  }
                </div>
              </div>
              @if (canLoadMore) {
                <button
                  type="button"
                  class="load-more"
                  [disabled]="appending"
                  (click)="loadMore()"
                >
                  Load more workers
                </button>
              } @else if (nextCursor) {
                <p class="list-limit" role="status">
                  Showing the first 500 workers. Narrow the filters to see
                  others.
                </p>
              }
            </div>
            @if (selected) {
              <section
                class="record-detail"
                role="complementary"
                aria-labelledby="worker-detail-title"
                [weaveModalSheet]="true"
                [sheetWhen]="sheetWhen.detail"
                sheetInitialFocus="#worker-detail-title"
                (sheetDismiss)="close()"
              >
                <weave-worker-detail
                  [host]="host"
                  [worker]="selected"
                  (changed)="replace($event)"
                  (refresh)="openById(selected.id)"
                  (revoked)="poller.refresh()"
                  (closed)="close()"
                />
              </section>
            }
          </div>
        }
      }
    </div>
    @if (retire; as dialog) {
      <weave-modal
        heading="Retire offline workers"
        closeLabel="Close retire offline workers"
        describedBy="retire-description"
        (dismiss)="closeRetire()"
      >
        <p id="retire-description" class="dialog-message">
          Revokes workers that hold no tasks and haven't been in contact for as
          long as you choose, oldest first, up to {{ batch }} at a time. A
          revoked worker can't claim tasks again.
        </p>
        <div class="dialog-form">
          <div class="field">
            <label for="retire-threshold">Offline for at least</label>
            <select
              id="retire-threshold"
              [disabled]="dialog.busy"
              (change)="setRetireThreshold(value($event))"
            >
              @for (option of thresholds; track option.seconds) {
                <option
                  [value]="option.seconds"
                  [selected]="dialog.seconds === option.seconds"
                >
                  {{ option.label }}
                </option>
              }
            </select>
          </div>
          <p class="retire-status" role="status">{{ retireText(dialog) }}</p>
          @if (dialog.result?.worker_ids?.length) {
            <ul class="retired-ids" aria-label="Revoked workers">
              @for (id of dialog.result!.worker_ids; track id) {
                <li>
                  <code [attr.title]="id">{{ short(id) }}</code>
                </li>
              }
            </ul>
          }
          @if (dialog.problem; as failed) {
            <div class="notice error-notice" role="alert">
              <p>{{ failed.message }}</p>
              @if (failed.code) {
                <small class="support-code"
                  >Support code: {{ failed.code }}</small
                >
              }
            </div>
          }
          <div class="dialog-actions">
            <button type="button" (click)="closeRetire()">
              {{ dialog.result ? "Close" : "Cancel" }}
            </button>
            @if (retireLabel(dialog); as label) {
              <button
                type="button"
                class="danger-solid"
                [attr.aria-disabled]="dialog.busy ? 'true' : null"
                (click)="confirmRetire()"
              >
                {{ dialog.busy ? "Retiring…" : label }}
              </button>
            }
          </div>
        </div>
      </weave-modal>
    }
  `,
})
export class WorkersPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  private navigated = this.router.events.subscribe((event) => {
    if (event instanceof NavigationEnd) this.addressChanged();
  });
  readonly sheetWhen = sheetWhen;
  /** Narrow screens fold the filters behind "Filters (n)". */
  filtersOpen = false;
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly relative = timeRelative;
  readonly badges = workerBadges;
  readonly tone = toneAttribute;
  readonly filterCount = filterCount;
  readonly where = workerWhere;
  readonly version = workerVersion;
  readonly bulkLimit = BULK_LIMIT;
  readonly batch = RETIRE_BATCH;
  readonly thresholds = retireThresholds;
  readonly presenceOptions: [Presence | "", string][] = [
    ["", "Any status"],
    ["online", "Online"],
    ["offline", "Offline"],
    ["unknown", "Not seen yet"],
  ];
  /** The fleet reloads every 10 s while it shows; Releases reads its own. */
  readonly poller = new Poller(
    () => this.load(),
    10_000,
    undefined,
    () => this.tab !== "fleet" || this.selection.size > 0 || this.bulkRunning,
  );
  tab: "fleet" | "releases" = "fleet";
  workers: WorkerRecord[] = [];
  nextCursor: string | null = null;
  loaded = false;
  forbidden = false;
  error: PlainError | null = null;
  filters: WorkerFilters = noWorkerFilters;
  selected: WorkerStatus | null = null;
  /** Why a worker could not be read; it stays until another worker is opened. */
  detailError: { id: string; error: PlainError } | null = null;
  selection = new Set<string>();
  bulk: { title: string; results: BulkResult[] } | null = null;
  bulkRunning = false;
  retire: RetireState | null = null;
  /** Targets and releases this person may read, for the filters' choices. */
  targets: Pick<Target, "id" | "name">[] = [];
  releases: { id: string; task_types: string[]; digest: string }[] = [];
  /** The worker the latest read is for: an older answer never replaces it. */
  private opening = "";
  private pages = 1;
  private readonly listReads = new ListReads();
  private hostTimer: ReturnType<typeof setTimeout> | null = null;
  appending = false;

  get canLoadMore() {
    return !!this.nextCursor && this.pages < MAX_PAGES;
  }
  private scopeKey = "";
  private generation = 0;
  /** Choices are read once per platform, environment and person. */
  private choicesScope = "";
  /** Set while the page steps back to its list: that popstate is its own. */
  private closing = false;

  get readable() {
    return this.host.canAnywhere("status.read");
  }
  private get scope() {
    return environmentOf(this.host);
  }
  ngOnInit() {
    this.tab = workersRoute(location.pathname).tab;
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.navigated.unsubscribe();
    this.poller.stop();
    if (this.hostTimer) clearTimeout(this.hostTimer);
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.generation++;
    this.workers = [];
    this.nextCursor = null;
    this.loaded = false;
    this.forbidden = false;
    this.error = null;
    this.selected = null;
    this.detailError = null;
    this.opening = "";
    this.pages = 1;
    this.appending = false;
    this.selection = new Set();
    this.bulk = null;
    this.retire = null;
    this.targets = [];
    this.releases = [];
    // A host prefix typed for another platform, environment or person goes.
    if (this.hostTimer) clearTimeout(this.hostTimer);
    this.hostTimer = null;
    this.tab = workersRoute(location.pathname).tab;
    this.filters = filtersFromQuery(location.search);
    const id =
      this.tab === "fleet" ? (viewFromPath(location.pathname)?.id ?? "") : "";
    if (id) void this.openById(id);
    if (this.scope && this.host.identity && this.readable) {
      if (this.tab === "fleet") void this.poller.refresh();
      void this.readChoices();
    }
  }
  @HostListener("window:popstate") locationChanged() {
    const closing = this.closing;
    this.closing = false;
    if (viewFromPath(location.pathname)?.view !== "workers") return;
    // The page stepped back to its own list entry: the list is on screen
    // already, and why a worker could not be read stays with it.
    if (closing) this.followFilters(filtersFromQuery(location.search));
    else this.readLocation();
  }
  /** The Workers entry or a link moved the address: the page follows it. */
  private addressChanged() {
    const route = viewFromPath(location.pathname);
    if (route?.view !== "workers") return;
    const tab = workersRoute(location.pathname).tab;
    const same =
      tab === this.tab &&
      route.id === (this.selected?.id ?? "") &&
      filtersToQuery(filtersFromQuery(location.search)) ===
        filtersToQuery(this.filters);
    if (!same) this.readLocation();
  }
  /** The tab, the filters and a worker's own address, from the address. */
  private readLocation() {
    const tab = workersRoute(location.pathname).tab;
    if (tab !== this.tab) {
      this.tab = tab;
      if (tab === "fleet" && this.scope && this.host.identity && this.readable)
        void this.poller.refresh();
    }
    this.followFilters(filtersFromQuery(location.search));
    const id =
      tab === "fleet" ? (viewFromPath(location.pathname)?.id ?? "") : "";
    if (!id) {
      this.selected = null;
      this.detailError = null;
      this.opening = "";
    } else if (this.selected?.id !== id) void this.openById(id);
  }
  /** Filters from the address keep the host prefix typed on this page. */
  private followFilters(shared: WorkerFilters) {
    const next = { ...shared, hostPrefix: this.filters.hostPrefix };
    if (filtersToQuery(next) === filtersToQuery(this.filters)) return;
    this.filters = next;
    this.changed();
  }
  showTab(tab: "fleet" | "releases") {
    if (tab === this.tab) return;
    void this.router.navigateByUrl(
      tab === "releases"
        ? releasesPath()
        : viewPath("workers") + filtersToQuery(this.filters),
    );
  }
  /** Arrow keys move between the two tabs, as WAI-ARIA tabs do. */
  tabKey(event: KeyboardEvent) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === "Home"
        ? "fleet"
        : event.key === "End"
          ? "releases"
          : this.tab === "fleet"
            ? "releases"
            : "fleet";
    this.showTab(next);
    setTimeout(() =>
      document.querySelector<HTMLElement>(`#workers-tab-${next}`)?.focus(),
    );
  }

  // ---------------------------------------------------------------- list

  /** Reads as many pages as are on screen, with the filters' query. */
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return false;
    const generation = this.generation;
    const filters = this.filters;
    return this.listReads.run(async () => {
      if (generation !== this.generation) return false;
      try {
        const items: WorkerRecord[] = [];
        let cursor: string | null = null;
        let read = 0;
        do {
          const page: Page<WorkerRecord> = await this.host.api.request(
            `${scope}/workers?${workerQuery(filters, cursor)}`,
          );
          if (generation !== this.generation) return false;
          items.push(...page.items);
          cursor = page.next_cursor;
        } while (cursor && ++read < this.pages);
        this.workers = items;
        this.nextCursor = cursor;
        this.loaded = true;
        this.forbidden = false;
        this.error = null;
        const listed = new Set(items.map((item) => item.id));
        this.selection = new Set(
          [...this.selection].filter((id) => listed.has(id)),
        );
        const fresh = this.selected
          ? items.find((item) => item.id === this.selected!.id)
          : undefined;
        if (fresh && !isUnavailableWorker(fresh)) this.selected = fresh;
        return true;
      } catch (error) {
        if (generation !== this.generation) return false;
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
        throw error;
      } finally {
        this.cdr.markForCheck();
      }
    });
  }
  /** Continues the list with the same filters. */
  async loadMore() {
    const scope = this.scope;
    if (
      !scope ||
      !this.host.identity ||
      !this.readable ||
      !this.canLoadMore ||
      this.appending
    )
      return;
    const generation = this.generation;
    const filters = this.filters;
    this.appending = true;
    try {
      await this.listReads.run(async () => {
        if (generation !== this.generation || !this.canLoadMore) return;
        const page: Page<WorkerRecord> = await this.host.api.request(
          `${scope}/workers?${workerQuery(filters, this.nextCursor)}`,
        );
        if (generation !== this.generation) return;
        const seen = new Set(this.workers.map((item) => item.id));
        this.workers = [
          ...this.workers,
          ...page.items.filter((item) => !seen.has(item.id)),
        ];
        this.nextCursor = page.next_cursor;
        this.pages++;
        this.forbidden = false;
        this.error = null;
      });
    } catch (error) {
      if (generation === this.generation)
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
    } finally {
      if (generation === this.generation) this.appending = false;
      this.cdr.markForCheck();
    }
  }
  /**
   * The targets and releases this person may read, once per scope, to offer
   * as filter choices; a refusal or failure leaves the loaded ones.
   */
  private async readChoices() {
    const scope = this.scope;
    const key = this.scopeKey;
    if (!scope || this.choicesScope === key) return;
    this.choicesScope = key;
    const reads: Promise<void>[] = [];
    if (this.host.canAnywhere("deployment.read"))
      reads.push(
        this.host.api
          .request<Page<Target>>(`${scope}/deployment-targets?limit=100`)
          .then((page) => {
            if (key === this.scopeKey)
              this.targets = page.items.map(({ id, name }) => ({ id, name }));
          }),
      );
    if (this.host.canAnywhere("catalog.read"))
      reads.push(
        this.host.api
          .request<Page<WorkerRelease>>(`${scope}/worker-releases?limit=100`)
          .then((page) => {
            if (key === this.scopeKey)
              this.releases = page.items.map((release) => ({
                id: release.id,
                task_types: releaseTaskTypes(release),
                digest: release.image_digest,
              }));
          }),
      );
    await Promise.allSettled(reads);
    this.cdr.markForCheck();
  }
  choicesOf() {
    return filterChoices(this.workers, this.releases, this.filters);
  }
  targetChoices() {
    const targets = [...this.targets];
    if (
      this.filters.target &&
      !targets.some((target) => target.id === this.filters.target)
    )
      targets.push({
        id: this.filters.target,
        name: `Target ${shortId(this.filters.target)}`,
      });
    return targets;
  }
  releaseName(id: string) {
    const release = this.releases.find((item) => item.id === id);
    return release
      ? `${shortDigest(release.digest)} (${shortId(id)})`
      : shortId(id);
  }
  /** "3 workers", or "50 workers loaded" while more follow. */
  countText() {
    const count = this.workers.length;
    const noun = count === 1 ? "worker" : "workers";
    return this.nextCursor ? `${count} ${noun} loaded` : `${count} ${noun}`;
  }

  // ---------------------------------------------------------------- filters

  setFilter<K extends keyof WorkerFilters>(key: K, value: WorkerFilters[K]) {
    this.apply({ ...this.filters, [key]: value });
  }
  setDraining(value: string) {
    this.setFilter(
      "draining",
      value === "true" ? true : value === "false" ? false : null,
    );
  }
  /** The host prefix is sent after a pause in typing and never put in the address. */
  setHost(value: string) {
    this.filters = { ...this.filters, hostPrefix: value.trim() };
    if (this.hostTimer) clearTimeout(this.hostTimer);
    this.hostTimer = setTimeout(() => {
      this.hostTimer = null;
      this.changed();
    }, 400);
  }
  clearFilters() {
    if (this.hostTimer) clearTimeout(this.hostTimer);
    this.hostTimer = null;
    this.apply(noWorkerFilters);
  }
  private apply(next: WorkerFilters) {
    this.filters = next;
    void this.syncAddress(true);
    this.changed();
  }
  /**
   * New filters: the first page again; Load more pages, the selection and
   * an answer still out for the old filters go.
   */
  private changed() {
    this.generation++;
    this.pages = 1;
    this.nextCursor = null;
    this.appending = false;
    this.selection = new Set();
    this.bulk = null;
    if (this.scope && this.host.identity && this.readable)
      void this.poller.refresh();
  }

  // ---------------------------------------------------------------- detail

  async open(worker: WorkerStatus) {
    this.selected = worker;
    // From the list a worker's address is a new entry, marked as Studio's
    // own; from another worker's address it replaces that entry and keeps
    // its mark, so closing always steps back to the list.
    const onWorker = !!viewFromPath(location.pathname)?.id;
    await this.syncAddress(onWorker, {
      weaveWorkerPushed: onWorker ? !!history.state?.weaveWorkerPushed : true,
    });
    await this.openById(worker.id);
  }
  /**
   * Reads one worker for its detail (also its own address and Refresh
   * worker). A worker this person can't read, or that is gone, is explained
   * above the list and the address returns to the list (as Close does); any
   * other failure keeps the address so Try again can read it.
   */
  async openById(id: string) {
    const scope = this.scope;
    // Before the person's access is known there is nothing to ask for.
    if (!scope || !this.host.identity || !this.readable) return;
    const generation = this.scopeGeneration();
    this.opening = id;
    this.detailError = null;
    try {
      const worker = await this.host.api.request<WorkerStatus>(
        `${scope}/workers/${encodeURIComponent(id)}`,
      );
      if (generation !== this.scopeGeneration() || this.opening !== id) return;
      this.selected = worker;
    } catch (error) {
      if (generation !== this.scopeGeneration() || this.opening !== id) return;
      const plain = describeError(error);
      this.detailError = { id, error: plain };
      if (plain.status === 403 || plain.status === 404) {
        this.selected = null;
        await this.returnToList();
      }
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** A worker read belongs to the scope it was asked in, whatever the filters. */
  private scopeGeneration() {
    return this.scopeKey;
  }
  /** Try again on a worker that could not be read: its address comes back. */
  async retryDetail(id: string) {
    await this.openById(id);
    if (this.selected?.id === id) await this.syncAddress(false);
  }
  replace(worker: WorkerStatus) {
    // The answer to a command belongs to the worker it was for.
    if (this.selected?.id !== worker.id) return;
    this.selected = worker;
    this.workers = this.workers.map((item) =>
      item.id === worker.id ? worker : item,
    );
  }
  close() {
    // A second close before the step back lands must not step back again.
    if (this.closing) return;
    this.selected = null;
    this.detailError = null;
    this.opening = "";
    void this.returnToList();
  }
  /**
   * Puts the list's address back: one step back when Studio pushed the
   * worker's entry from the list, in place when the person arrived on it.
   */
  private async returnToList() {
    if (
      viewFromPath(location.pathname)?.id &&
      history.state?.weaveWorkerPushed
    ) {
      this.closing = true;
      history.back();
    } else await this.syncAddress(true);
  }
  /** Puts what the page shows in the address; settles once it is there. */
  private async syncAddress(replace: boolean, state?: Record<string, unknown>) {
    if (this.tab !== "fleet") return;
    const path =
      viewPath("workers", this.selected?.id ?? "") +
      filtersToQuery(this.filters);
    if (location.pathname + location.search !== path)
      await this.router.navigateByUrl(path, { replaceUrl: replace, state });
  }

  // ---------------------------------------------------------------- selection and bulk actions

  canRetire() {
    return this.host.can("release.retire");
  }
  /** Drain and Resume show when the person may drain some worker here. */
  canDrainSelected() {
    return this.host.canAnywhere("worker.drain");
  }
  canSelect() {
    return this.canDrainSelected() || this.canRetire();
  }
  emptySelection() {
    return new Set<string>();
  }
  select(id: string, on: boolean) {
    const next = new Set(this.selection);
    if (on && next.size < BULK_LIMIT) next.add(id);
    else next.delete(id);
    this.selection = next;
  }
  private selectable() {
    return this.workers
      .filter((item) => !isUnavailableWorker(item))
      .slice(0, BULK_LIMIT);
  }
  allSelected() {
    const rows = this.selectable();
    return rows.length > 0 && rows.every((item) => this.selection.has(item.id));
  }
  selectAll(on: boolean) {
    this.selection = on
      ? new Set(this.selectable().map((item) => item.id))
      : new Set();
  }
  /**
   * Drain, Resume or Revoke the selected workers, one after another, each
   * with its own revision (and, for Drain and Resume, its own key); each
   * worker reports its own answer.
   */
  async bulkAction(action: BulkAction) {
    const ids = [...this.selection].slice(0, BULK_LIMIT);
    if (!ids.length || this.bulkRunning) return;
    // The workspace and person the selection belongs to: a change while the
    // confirmation is open sends nothing.
    const scope = this.scope;
    const key = this.scopeKey;
    const count = ids.length;
    const workers = count === 1 ? "this worker" : `${count} workers`;
    const confirmed = await this.host.dialogs.confirm(
      action === "revoke"
        ? {
            title: `Revoke ${workers}?`,
            message:
              "A revoked worker can't claim tasks again. Tasks it is running finish or expire.",
            confirmLabel: count === 1 ? "Revoke worker" : "Revoke workers",
            cancelLabel: "Keep",
            danger: true,
          }
        : action === "drain"
          ? {
              title: `Drain ${workers}?`,
              message:
                "They finish their active tasks and take no new ones. You can resume them at any time.",
              confirmLabel: "Drain",
            }
          : {
              title: `Resume ${workers}?`,
              message: "They take new tasks again.",
              confirmLabel: "Resume",
            },
    );
    if (!confirmed || key !== scopeKeyOf(this.host)) return;
    const rows = new Map(
      this.workers
        .filter((item) => !isUnavailableWorker(item))
        .map((item) => [item.id, item as WorkerStatus]),
    );
    const title = { drain: "Drain", resume: "Resume", revoke: "Revoke" }[
      action
    ];
    const results: BulkResult[] = ids.map((id) => ({
      id,
      ok: false,
      text: "Waiting…",
    }));
    let shown = { title: `${title} workers`, results: [...results] };
    this.bulk = shown;
    this.bulkRunning = true;
    this.cdr.markForCheck();
    try {
      for (const [index, id] of ids.entries()) {
        const worker = rows.get(id);
        let result: { ok: boolean; text: string };
        // Who is signed in is read from the shell right before each send,
        // once a check of it still out has answered, and never from the
        // copy change detection keeps: another person may be signed in.
        if (this.host.checkingIdentity) await this.host.identitySettled();
        if (key !== scopeKeyOf(this.host))
          result = { ok: false, text: "Not sent: the workspace changed." };
        else if (!worker)
          result = { ok: false, text: "Not sent: it is no longer listed." };
        else result = await this.act(action, worker, scope);
        results[index] = { id, ...result };
        if (this.bulk === shown) {
          shown = { ...shown, results: [...results] };
          this.bulk = shown;
        }
        this.cdr.markForCheck();
      }
    } finally {
      this.bulkRunning = false;
      this.selection = new Set();
      this.cdr.markForCheck();
    }
    if (key === this.scopeKey) void this.poller.refresh();
  }
  private async act(
    action: BulkAction,
    worker: WorkerStatus,
    scope: string,
  ): Promise<{ ok: boolean; text: string }> {
    if (worker.revoked)
      return { ok: false, text: "Not sent: it is already revoked." };
    if (action === "drain" && worker.draining)
      return { ok: false, text: "Not sent: it is already draining." };
    if (action === "resume" && !worker.draining)
      return { ok: false, text: "Not sent: it is not draining." };
    const capability = action === "revoke" ? "release.retire" : "worker.drain";
    if (!this.host.can(capability, worker.id))
      return { ok: false, text: `Not sent: you need ${capability} for it.` };
    const path = `${scope}/workers/${encodeURIComponent(worker.id)}/${action}`;
    try {
      if (action === "revoke") await this.host.api.request(path, "POST");
      else
        await this.host.api.request(
          path,
          "POST",
          { expected_revision: worker.revision },
          { "Idempotency-Key": crypto.randomUUID() },
        );
      return {
        ok: true,
        text:
          action === "drain"
            ? "Draining."
            : action === "resume"
              ? "Takes new tasks again."
              : "Revoked.",
      };
    } catch (error) {
      const plain = describeError(error);
      return {
        ok: false,
        text:
          plain.code === "WV-WORKER-REVISION"
            ? "Not changed: it changed since the list was read. Refresh, then try again."
            : plain.status === 403
              ? `You need ${capability} for it. ${plain.message}`
              : plain.message + (plain.code ? ` (${plain.code})` : ""),
      };
    }
  }

  // ---------------------------------------------------------------- retire offline workers

  openRetire() {
    if (!this.canRetire()) return;
    this.retire = {
      seconds: retireThresholds[0].seconds,
      preview: null,
      result: null,
      busy: false,
      problem: null,
      pendingKey: null,
    };
    void this.previewRetire();
  }
  closeRetire() {
    this.retire = null;
  }
  setRetireThreshold(value: string) {
    const dialog = this.retire;
    if (!dialog || dialog.busy) return;
    this.retire = {
      ...dialog,
      seconds: Number(value),
      preview: null,
      result: null,
      problem: null,
      pendingKey: null,
    };
    void this.previewRetire();
  }
  /** What the dialog says: checking, the preview, or the retirement's result. */
  retireText(dialog: RetireState) {
    if (dialog.result)
      return retireSummary({ ...dialog.result, dry_run: false });
    if (dialog.preview)
      return retireSummary({ ...dialog.preview, dry_run: true });
    return dialog.problem ? "" : "Checking which workers match…";
  }
  /** The confirm button's words, or "" when there is nothing to retire. */
  retireLabel(dialog: RetireState) {
    if (dialog.pendingKey) return "Send again";
    if (dialog.result) {
      const left = dialog.result.matched - dialog.result.revoked;
      return left > 0 ? `Retire the next ${Math.min(left, RETIRE_BATCH)}` : "";
    }
    const matched = dialog.preview?.matched ?? 0;
    if (!matched) return "";
    const count = Math.min(matched, RETIRE_BATCH);
    return `Retire ${count} ${count === 1 ? "worker" : "workers"}`;
  }
  /** Asks what a retirement would revoke now, without revoking anything. */
  private async previewRetire() {
    const dialog = this.retire;
    if (!dialog) return;
    const seconds = dialog.seconds;
    dialog.busy = true;
    this.cdr.markForCheck();
    try {
      const answer = await this.sendRetire(seconds, true, crypto.randomUUID());
      if (this.retire !== dialog || dialog.seconds !== seconds) return;
      dialog.preview = answer;
    } catch (error) {
      if (this.retire === dialog) dialog.problem = describeError(error);
    } finally {
      dialog.busy = false;
      this.cdr.markForCheck();
    }
  }
  /**
   * Retires a batch with one key. An answer lost on the way keeps the key,
   * so Send again replays the first answer; the next batch takes a new key.
   */
  async confirmRetire() {
    const dialog = this.retire;
    if (!dialog || dialog.busy || !this.retireLabel(dialog)) return;
    const key = dialog.pendingKey ?? crypto.randomUUID();
    dialog.pendingKey = key;
    dialog.busy = true;
    dialog.problem = null;
    this.cdr.markForCheck();
    const scopeKey = this.scopeKey;
    try {
      const answer = await this.sendRetire(dialog.seconds, false, key);
      if (scopeKey === this.scopeKey) void this.poller.refresh();
      // The dialog closed meanwhile (or the workspace or person changed):
      // the person still learns what happened, without the worker IDs.
      if (this.retire !== dialog) {
        const count = answer.revoked;
        this.host.notify(
          `Retired ${count} offline ${count === 1 ? "worker" : "workers"}.`,
        );
        return;
      }
      dialog.pendingKey = null;
      dialog.result = answer;
      dialog.preview = null;
    } catch (error) {
      const plain = describeError(error);
      const unknown =
        plain.status === 0 || plain.status === 408 || plain.status >= 500;
      if (this.retire !== dialog) {
        // Send again belongs to the closed dialog; a new preview shows what
        // still matches.
        this.host.notify(
          unknown
            ? "Studio didn't get an answer about retiring offline workers: workers may have been revoked. Open Retire offline workers… to see what still matches."
            : `Offline workers were not retired. ${plain.message}`,
        );
        return;
      }
      if (!unknown) dialog.pendingKey = null;
      dialog.problem = unknown
        ? {
            ...plain,
            message:
              "Studio didn't get an answer, so workers may have been revoked. Send it again: the same request reports what happened instead of revoking more.",
          }
        : plain;
    } finally {
      dialog.busy = false;
      this.cdr.markForCheck();
    }
  }
  private sendRetire(seconds: number, dryRun: boolean, key: string) {
    return this.host.api.request<RetireAnswer>(
      `${this.scope}/workers/retire-stale`,
      "POST",
      { older_than_seconds: seconds, dry_run: dryRun },
      { "Idempotency-Key": key },
    );
  }

  // ---------------------------------------------------------------- cells

  unavailable(worker: WorkerRecord) {
    return isUnavailableWorker(worker);
  }
  status(worker: WorkerRecord) {
    return worker as WorkerStatus;
  }
  loadOf(worker: WorkerStatus) {
    return workerLoad(worker);
  }
  loadLabel(text: string) {
    return text === "Unknown" ? "Load unknown" : `${text} task slots in use`;
  }
  typesText(worker: WorkerStatus) {
    const count = worker.task_types.length;
    return `${count} task ${count === 1 ? "type" : "types"}`;
  }
  value(event: Event) {
    return (event.target as HTMLSelectElement).value as never;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked as never;
  }
}
