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
// Operate › Workers: every worker in the environment with its presence,
// claims, load and last contact, filters kept in the address, and a detail
// panel with Drain and Resume. Reloads every 10 s while visible.
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
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import { toneAttribute } from "../../status-labels";
import { MAX_PAGES } from "../operate-limits";
import {
  ListReads,
  environmentOf,
  listFailure,
  readPages,
  scopeKeyOf,
} from "../operate-list";
import { OperateState } from "../operate-state";
import { viewFromPath, viewPath } from "../operate-routes";
import { Poller } from "../operate-store";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { RefreshStatus } from "../refresh-status";
import { WorkerDetail } from "./worker-detail";
import {
  filterChoices,
  filterCount,
  filterWorkers,
  filtersFromQuery,
  filtersToQuery,
  isUnavailableWorker,
  noWorkerFilters,
  workerBadges,
  workerLoad,
  type Presence,
  type WorkerFilters,
  type WorkerRecord,
  type WorkerStatus,
} from "./workers-model";

@Component({
  selector: "weave-workers-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, ModalSheet, OperateState, RefreshStatus, WorkerDetail],
  styleUrls: ["../operate.css", "./workers.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1>Workers</h1>
        <p class="subtitle">
          Programs that run your worker actions in this environment.
        </p>
      </div>
      @if (readable && loaded) {
        <div class="actions">
          <weave-refresh-status
            [updatedAt]="poller.lastSuccessAt"
            [busy]="poller.busy"
            (refresh)="poller.refresh()"
          />
        </div>
      }
    </div>
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
      <div class="operate-filters" role="search" aria-label="Filter workers">
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
          <label for="worker-release">Release</label>
          <select
            id="worker-release"
            (change)="setFilter('release', value($event))"
          >
            <option value="" [selected]="!filters.release">Any release</option>
            @for (release of choices.releases; track release) {
              <option
                [value]="release"
                [selected]="filters.release === release"
              >
                {{ short(release) }}
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
        <label class="checkbox-field"
          ><input
            type="checkbox"
            [checked]="filters.draining"
            (change)="setFilter('draining', checked($event))"
          />Draining only</label
        >
        <label class="checkbox-field"
          ><input
            type="checkbox"
            [checked]="filters.revoked"
            (change)="setFilter('revoked', checked($event))"
          />Show revoked workers</label
        >
        <span class="operate-count" role="status">{{ countText() }}</span>
        @if (filterCount(filters)) {
          <button type="button" class="tertiary" (click)="clearFilters()">
            Clear filters
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
      @if (!visible().length) {
        @if (workers.length) {
          <weave-operate-state
            kind="empty"
            [heading]="
              nextCursor
                ? 'No loaded workers match these filters'
                : 'No workers match these filters'
            "
            [text]="
              nextCursor
                ? canLoadMore
                  ? 'More workers may match on later pages. Load more or change the filters.'
                  : 'More workers may match beyond the page limit. Change the filters to check the loaded records.'
                : 'Change or clear the filters to see more workers.'
            "
          >
            <button type="button" (click)="clearFilters()">
              Clear filters
            </button>
          </weave-operate-state>
        } @else {
          <weave-operate-state
            kind="empty"
            [heading]="nextCursor ? 'No loaded workers' : 'No workers yet'"
            [text]="
              nextCursor
                ? 'More workers may exist on later pages.'
                : 'Workers appear when a worker release starts in this environment.'
            "
          />
        }
      } @else {
        <div class="resource-layout" [class.has-detail]="!!selected">
          <div class="worker-list">
            <div
              class="resource-table worker-table"
              role="table"
              aria-label="Workers"
            >
              <div role="rowgroup">
                <div class="table-head" role="row">
                  <span role="columnheader">Worker</span
                  ><span role="columnheader">Status</span
                  ><span role="columnheader" data-wide>Release</span
                  ><span role="columnheader">Load</span
                  ><span role="columnheader" data-wide>Last contact</span>
                </div>
              </div>
              <div role="rowgroup">
                @for (worker of visible(); track worker.id) {
                  @if (unavailable(worker)) {
                    <div class="resource-row" role="row">
                      <span role="cell" class="row-text"
                        ><strong>Unavailable worker</strong
                        ><small>Studio can't show this worker.</small></span
                      ><span role="cell"></span
                      ><span role="cell" data-wide></span
                      ><span role="cell"></span
                      ><span role="cell" data-wide></span>
                    </div>
                  } @else {
                    @let w = status(worker);
                    @let load = loadOf(w);
                    <div
                      class="resource-row"
                      role="row"
                      [class.selected]="selected?.id === w.id"
                    >
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
                        ><small>{{ typesText(w) }}</small></span
                      ><span role="cell" class="badges">
                        @for (badge of badges(w); track badge.label) {
                          <span
                            class="status-pill"
                            [attr.data-tone]="tone(badge.tone)"
                            >{{ badge.label }}</span
                          >
                        }</span
                      ><span role="cell" data-wide
                        ><code [attr.title]="w.release_id">{{
                          short(w.release_id)
                        }}</code></span
                      ><span role="cell"
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
                        }
                      </span>
                    </div>
                  }
                }
              </div>
            </div>
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
                (closed)="close()"
              />
            </section>
          }
        </div>
      }
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
        <p role="status">Showing up to 500 workers. More workers may exist.</p>
      }
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
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly relative = timeRelative;
  readonly badges = workerBadges;
  readonly tone = toneAttribute;
  readonly filterCount = filterCount;
  readonly presenceOptions: [Presence | "", string][] = [
    ["", "Any status"],
    ["online", "Online"],
    ["offline", "Offline"],
    ["unknown", "Not seen yet"],
  ];
  readonly poller = new Poller(() => this.load(), 10_000);
  workers: WorkerRecord[] = [];
  nextCursor: string | null = null;
  loaded = false;
  forbidden = false;
  error: PlainError | null = null;
  filters: WorkerFilters = noWorkerFilters;
  selected: WorkerStatus | null = null;
  /** Why a worker could not be read; it stays until another worker is opened. */
  detailError: { id: string; error: PlainError } | null = null;
  /** The worker the latest read is for: an older answer never replaces it. */
  private opening = "";
  private pages = 1;
  private readonly listReads = new ListReads();
  appending = false;

  get canLoadMore() {
    return !!this.nextCursor && this.pages < MAX_PAGES;
  }
  private scopeKey = "";
  private generation = 0;
  /** Set while the page steps back to its list: that popstate is its own. */
  private closing = false;

  get readable() {
    return this.host.canAnywhere("status.read");
  }
  private get scope() {
    return environmentOf(this.host);
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.navigated.unsubscribe();
    this.poller.stop();
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
    this.readLocation();
    if (this.scope && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  @HostListener("window:popstate") locationChanged() {
    const closing = this.closing;
    this.closing = false;
    if (viewFromPath(location.pathname)?.view !== "workers") return;
    // The page stepped back to its own list entry: the list is on screen
    // already, and why a worker could not be read stays with it.
    if (closing) this.filters = filtersFromQuery(location.search);
    else this.readLocation();
  }
  /** The Workers entry or a link moved the address: the page follows it. */
  private addressChanged() {
    const route = viewFromPath(location.pathname);
    if (route?.view !== "workers") return;
    const same =
      route.id === (this.selected?.id ?? "") &&
      filtersToQuery(filtersFromQuery(location.search)) ===
        filtersToQuery(this.filters);
    if (!same) this.readLocation();
  }
  /** Filters from the query; a worker's own address opens its detail. */
  private readLocation() {
    this.filters = filtersFromQuery(location.search);
    const id = viewFromPath(location.pathname)?.id ?? "";
    if (!id) {
      this.selected = null;
      this.detailError = null;
      this.opening = "";
    } else if (this.selected?.id !== id) void this.openById(id);
  }
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return false;
    const generation = this.generation;
    return this.listReads.run(async () => {
      if (generation !== this.generation) return false;
      try {
        const { items, cursor } = await readPages<WorkerRecord>((query) => {
          if (generation !== this.generation)
            throw Error("List scope changed.");
          return this.host.api.request(`${scope}/workers?${query}`);
        }, this.pages);
        if (generation !== this.generation) return false;
        this.workers = items;
        this.nextCursor = cursor;
        this.loaded = true;
        this.forbidden = false;
        this.error = null;
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
    this.appending = true;
    try {
      await this.listReads.run(async () => {
        if (generation !== this.generation || !this.canLoadMore) return;
        const page = await readPages<WorkerRecord>(
          (query) => this.host.api.request(`${scope}/workers?${query}`),
          1,
          this.nextCursor,
        );
        if (generation !== this.generation) return;
        this.workers = [...this.workers, ...page.items];
        this.nextCursor = page.cursor;
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
  visible() {
    return filterWorkers(this.workers, this.filters);
  }
  choicesOf() {
    return filterChoices(this.workers);
  }
  /** "3 workers", or "1 of 3 workers" while filters narrow the list. */
  countText() {
    const shown = this.visible().length;
    const total = filterWorkers(this.workers, {
      ...noWorkerFilters,
      revoked: this.filters.revoked,
    }).length;
    const noun = (count: number) => (count === 1 ? "worker" : "workers");
    return shown === total
      ? `${total} ${noun(total)}`
      : `${shown} of ${total} ${noun(total)}`;
  }
  setFilter<K extends keyof WorkerFilters>(key: K, value: WorkerFilters[K]) {
    this.filters = { ...this.filters, [key]: value };
    void this.syncAddress(true);
  }
  clearFilters() {
    this.filters = noWorkerFilters;
    void this.syncAddress(true);
  }
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
    const generation = this.generation;
    this.opening = id;
    this.detailError = null;
    try {
      const worker = await this.host.api.request<WorkerStatus>(
        `${scope}/workers/${encodeURIComponent(id)}`,
      );
      if (generation !== this.generation || this.opening !== id) return;
      this.selected = worker;
    } catch (error) {
      if (generation !== this.generation || this.opening !== id) return;
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
    const path =
      viewPath("workers", this.selected?.id ?? "") +
      filtersToQuery(this.filters);
    if (location.pathname + location.search !== path)
      await this.router.navigateByUrl(path, { replaceUrl: replace, state });
  }
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
