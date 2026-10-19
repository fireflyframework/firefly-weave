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
// Operate › Incidents: active incidents, newest opened first, filtered by the
// platform; the address keeps the filters and the open incident
// ("?incident=ID"), which opens from incidents.read even when no loaded page
// lists it. A drawer says what happened, and the Resolve incident dialog
// decides on the revision the person looked at. The server checks
// incident.resolve, the revision and the receipt on every decision. Reloads
// every 15 s while visible, except while a decision is being made.
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
import type { Page } from "../../operations/deployment-contracts";
import {
  ListReads,
  environmentOf,
  listFailure,
  scopeKeyOf,
} from "../operate-list";
import { OperateState } from "../operate-state";
import { viewFromPath, viewPath } from "../operate-routes";
import { Poller, browserEnvironment } from "../operate-store";
import { RefreshStatus } from "../refresh-status";
import { IncidentDrawer, type RecentEvents } from "./incident-drawer";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import {
  defaultIncidentFilters,
  incidentFilterCount,
  incidentFiltersFromQuery,
  incidentFiltersToQuery,
  incidentFromQuery,
  incidentQuery,
  incidentStatusLabel,
  incidentSuggestions,
  incidentTone,
  isUnavailableIncident,
  lastEvents,
  type IncidentFilters,
  type IncidentRecord,
  type IncidentView,
  type ResolutionRequest,
} from "./incidents-model";
import { ResolveIncidentDialog } from "./resolve-incident-dialog";

/** The longest opening range the platform accepts: 400 days. */
const MAX_RANGE_MS = 400 * 86_400_000;
/** A run's events are read 100 at a time, at most 1,000. */
const EVENT_PAGES = 10;
const noEvents: RecentEvents = {
  loading: false,
  lines: [],
  truncated: false,
  problem: null,
};

@Component({
  selector: "weave-incidents-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    ModalSheet,
    OperateState,
    RefreshStatus,
    IncidentDrawer,
    ResolveIncidentDialog,
  ],
  styleUrls: ["../operate.css", "./incidents.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1>Incidents</h1>
        <p class="subtitle">
          Runs that stopped on an uncertain step and wait for your decision.
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
        <h2>Incidents appear here once you connect</h2>
        <p>Incidents come from runs on a platform.</p>
        <button class="primary" (click)="host.openWizard('server')">
          <weave-icon name="cloud" [size]="16" />Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded) {
      <div class="empty-state">
        <h2>Sign in to see incidents</h2>
        <p>Studio shows them as soon as you're signed in again.</p>
      </div>
    } @else if (!host.identity) {
      <weave-operate-state kind="loading" noun="incidents" />
    } @else if (!readable || forbidden) {
      <weave-operate-state kind="access" capability="incident.read" />
    } @else if (!loaded && error) {
      <weave-operate-state
        kind="error"
        [message]="error.message"
        [code]="error.code"
        (retry)="poller.refresh()"
      />
    } @else if (!loaded) {
      <weave-operate-state kind="loading" noun="incidents" />
    } @else {
      @let hints = suggestions();
      <button
        type="button"
        class="filters-toggle"
        aria-controls="incident-filter-body"
        [attr.aria-expanded]="filtersOpen"
        (click)="filtersOpen = !filtersOpen"
      >
        <weave-icon name="search" [size]="16" />Filters ({{ filterCount() }})
      </button>
      <div
        class="operate-filters"
        id="incident-filter-body"
        role="search"
        aria-label="Filter incidents"
        [class.open]="filtersOpen"
      >
        <div class="field">
          <label for="incident-status">Status</label>
          <select
            id="incident-status"
            (change)="setFilters({ status: value($event) })"
          >
            @for (option of statusOptions; track option[0]) {
              <option
                [value]="option[0]"
                [selected]="filters.status === option[0]"
              >
                {{ option[1] }}
              </option>
            }
          </select>
        </div>
        <div class="field">
          <label for="incident-code">Code</label>
          <input
            id="incident-code"
            list="incident-code-options"
            autocomplete="off"
            spellcheck="false"
            [value]="filters.code"
            (change)="setFilters({ code: text($event) })"
          />
          <datalist id="incident-code-options">
            @for (code of hints.codes; track code) {
              <option [value]="code"></option>
            }
          </datalist>
        </div>
        <div class="field">
          <label for="incident-workflow">Workflow</label>
          <input
            id="incident-workflow"
            list="incident-workflow-options"
            autocomplete="off"
            spellcheck="false"
            [value]="filters.workflow"
            (change)="setFilters({ workflow: text($event) })"
          />
          <datalist id="incident-workflow-options">
            @for (name of hints.workflows; track name) {
              <option [value]="name"></option>
            }
          </datalist>
        </div>
        <div class="field">
          <label for="incident-step">Step</label>
          <input
            id="incident-step"
            list="incident-step-options"
            autocomplete="off"
            spellcheck="false"
            [value]="filters.nodeId"
            (change)="setFilters({ nodeId: text($event) })"
          />
          <datalist id="incident-step-options">
            @for (step of hints.steps; track step) {
              <option [value]="step"></option>
            }
          </datalist>
        </div>
        <div class="field">
          <label for="incident-opened-after">Opened after</label>
          <input
            id="incident-opened-after"
            type="datetime-local"
            [value]="localTime(filters.openedAfter)"
            [attr.aria-invalid]="rangeProblem ? 'true' : null"
            [attr.aria-describedby]="
              rangeProblem ? 'incident-range-problem' : null
            "
            (change)="setOpened('openedAfter', value($event))"
          />
        </div>
        <div class="field">
          <label for="incident-opened-before">Opened before</label>
          <input
            id="incident-opened-before"
            type="datetime-local"
            [value]="localTime(filters.openedBefore)"
            [attr.aria-invalid]="rangeProblem ? 'true' : null"
            [attr.aria-describedby]="
              rangeProblem ? 'incident-range-problem' : null
            "
            (change)="setOpened('openedBefore', value($event))"
          />
        </div>
        <div class="field">
          <label for="incident-order">Order</label>
          <select
            id="incident-order"
            (change)="setFilters({ order: value($event) })"
          >
            @for (option of orderOptions; track option[0]) {
              <option
                [value]="option[0]"
                [selected]="filters.order === option[0]"
              >
                {{ option[1] }}
              </option>
            }
          </select>
        </div>
        <label class="checkbox-field"
          ><input
            type="checkbox"
            [checked]="filters.includeTest"
            (change)="setFilters({ includeTest: checked($event) })"
          />Include test runs</label
        >
        <span class="operate-count" role="status">{{ countText() }}</span>
        @if (filterCount()) {
          <button type="button" class="tertiary" (click)="setFilters(defaults)">
            Clear filters ({{ filterCount() }})
          </button>
        }
      </div>
      @if (rangeProblem) {
        <p class="field-error" id="incident-range-problem" role="alert">
          {{ rangeProblem }}
        </p>
      }
      @if (error) {
        <weave-operate-state
          kind="partial"
          [message]="error.message"
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      }
      @if (detailProblem; as failed) {
        @if (failed.status === 403) {
          <weave-operate-state
            kind="access"
            heading="You don't have access to this incident"
            capability="incident.read"
          />
        } @else if (failed.status === 404) {
          <weave-operate-state
            kind="empty"
            heading="This incident can't be found"
            text="It may belong to another environment, or its run's data was deleted."
          />
        } @else {
          <weave-operate-state
            kind="error"
            [message]="failed.message"
            [code]="failed.code"
            (retry)="openFromAddress()"
          />
        }
      }
      <div class="resource-layout" [class.has-detail]="!!selected">
        <div class="incident-list">
          @if (!incidents.length) {
            @if (!filterCount()) {
              <weave-operate-state
                kind="empty"
                heading="No active incidents"
                text="Runs that stop on an uncertain step show here until someone decides what happens next."
              />
            } @else {
              <weave-operate-state
                kind="empty"
                heading="No incidents match these filters"
                text="Change the filters to see more incidents."
              >
                <button type="button" (click)="setFilters(defaults)">
                  Show active incidents
                </button>
              </weave-operate-state>
            }
          } @else {
            <div
              class="resource-table incident-table"
              role="table"
              aria-label="Incidents"
            >
              <div role="rowgroup">
                <div class="table-head" role="row">
                  <span role="columnheader">Incident</span
                  ><span role="columnheader">Status</span
                  ><span role="columnheader" data-wide>Workflow</span
                  ><span role="columnheader" data-wide>Step</span
                  ><span role="columnheader" data-wide>Opened</span
                  ><span role="columnheader" data-wider>Run</span>
                </div>
              </div>
              <div role="rowgroup">
                @for (item of incidents; track item.id) {
                  @if (unavailable(item)) {
                    <div class="resource-row" role="row">
                      <span role="cell" class="row-text"
                        ><strong>Unavailable incident</strong
                        ><small>Studio can't show this incident.</small></span
                      ><span role="cell"></span
                      ><span role="cell" data-wide></span
                      ><span role="cell" data-wide></span
                      ><span role="cell" data-wide></span
                      ><span role="cell" data-wider
                        ><code [attr.title]="item.id">{{
                          short(item.id)
                        }}</code></span
                      >
                    </div>
                  } @else {
                    @let i = view(item);
                    <div
                      class="resource-row"
                      role="row"
                      [class.selected]="selected?.id === i.id"
                    >
                      <span role="cell" class="row-text"
                        ><button
                          type="button"
                          class="row-open"
                          [attr.aria-current]="
                            selected?.id === i.id ? 'true' : null
                          "
                          [attr.aria-label]="
                            'Open incident ' + i.code + ' at ' + stepOf(i)
                          "
                          (click)="open(i)"
                        >
                          {{ i.code }}</button
                        ><small>
                          @if (i.origin_code !== i.code) {
                            First reported as {{ i.origin_code }}
                          } @else {
                            {{ stepOf(i) }}
                          }
                        </small></span
                      ><span role="cell" class="badges"
                        ><span class="status-pill" [attr.data-tone]="tone(i)">{{
                          label(i)
                        }}</span>
                        @if (i.test) {
                          <span class="tag">Test</span>
                        }
                        @if (i.external_effects_may_continue) {
                          <span class="effects" title="Effects may continue"
                            ><weave-icon name="warning" [size]="16" /><span
                              class="sr-only"
                              >Effects may continue</span
                            ></span
                          >
                        }</span
                      ><span
                        role="cell"
                        data-wide
                        class="row-text"
                        [attr.title]="workflowText(i)"
                        ><span
                          class="cell-line"
                          [attr.title]="i.workflow?.name ?? null"
                          >{{ i.workflow?.name ?? "Unknown" }}</span
                        >
                        @if (i.workflow) {
                          <small>{{ i.workflow.version }}</small>
                        }</span
                      ><span
                        role="cell"
                        data-wide
                        class="cell-line"
                        [attr.title]="stepOf(i)"
                        >{{ stepOf(i) }}</span
                      ><span role="cell" data-wide>
                        @if (i.opened_at) {
                          <time
                            [attr.datetime]="iso(i.opened_at)"
                            [attr.title]="absolute(i.opened_at)"
                            >{{ relative(i.opened_at) }}</time
                          >
                          @if (i.opened_at_estimated) {
                            <small class="estimated">estimated</small>
                          }
                        } @else {
                          Unknown
                        }</span
                      ><span role="cell" data-wider
                        ><code [attr.title]="i.run_id">{{
                          short(i.run_id)
                        }}</code></span
                      >
                    </div>
                  }
                }
              </div>
            </div>
          }
          @if (canLoadMore) {
            <button
              type="button"
              class="load-more"
              [disabled]="appending"
              (click)="loadMore()"
            >
              Load more incidents
            </button>
          } @else if (nextCursor) {
            <p class="list-limit" role="status">
              Showing the first 500 incidents. Narrow the filters to see others.
            </p>
          }
        </div>
        @if (selected) {
          <section
            class="record-detail"
            role="complementary"
            aria-labelledby="incident-detail-title"
            [weaveModalSheet]="true"
            [sheetWhen]="sheetWhen.detail"
            sheetInitialFocus="#incident-detail-title"
            (sheetDismiss)="close()"
          >
            <weave-incident-drawer
              [host]="host"
              [incident]="selected"
              [events]="events"
              (resolve)="startResolving()"
              (openRun)="openRun(selected)"
              (reloadEvents)="loadEvents(selected)"
              (closed)="close()"
            />
          </section>
        }
      </div>
    }
    @if (resolving && selected) {
      <weave-resolve-incident-dialog
        [incident]="selected"
        [busy]="sending.has(selected.id)"
        [failure]="failure"
        (decide)="resolve($event)"
        (cancel)="resolving = false"
        (reload)="reloadIncident()"
      />
    }
  `,
})
export class IncidentsPage implements DoCheck, OnInit, OnDestroy {
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
  readonly defaults = defaultIncidentFilters;
  readonly orderOptions: [IncidentFilters["order"], string][] = [
    ["opened_desc", "Newest opened"],
    ["opened_asc", "Oldest opened"],
    ["id", "Incident ID"],
  ];
  readonly statusOptions: [IncidentFilters["status"], string][] = [
    ["active", "Active"],
    ["resolved", "Resolved"],
    ["closed", "Closed"],
    ["all", "Any status"],
  ];
  // A reload never changes the incident under an open Resolve incident, not
  // even one already on its way when the dialog opened: the revision the
  // decision is sent with is the one the person looked at. Only Refresh
  // incident, in the dialog, reads it again.
  readonly poller = new Poller(
    () => this.load(),
    15_000,
    browserEnvironment,
    () => this.resolving,
  );
  incidents: IncidentRecord[] = [];
  nextCursor: string | null = null;
  loaded = false;
  forbidden = false;
  error: PlainError | null = null;
  filters: IncidentFilters = defaultIncidentFilters;
  selected: IncidentView | null = null;
  /** Why the incident the address names could not be opened. */
  detailProblem: PlainError | null = null;
  rangeProblem = "";
  events: RecentEvents = noEvents;
  resolving = false;
  /** The incidents whose decision is on its way; one never blocks another. */
  readonly sending = new Set<string>();
  /** The last refusal, shown in the dialog of the incident it was for. */
  failure: PlainError | null = null;
  /**
   * Per incident, the receipt of a decision whose answer was lost, with what
   * it was sent with (revision and decision). Sending the same again reuses
   * it, so the platform replays the first answer; anything else gets a new one.
   */
  private pending = new Map<string, { key: string; receipt: string }>();
  private pages = 1;
  private readonly listReads = new ListReads();
  appending = false;

  get canLoadMore() {
    return !!this.nextCursor && this.pages < MAX_PAGES;
  }
  private scopeKey = "";
  /** Bumped when the platform, environment or person changes. */
  private generation = 0;
  /** Bumped when the filters change: an answer for older filters is dropped. */
  private listGeneration = 0;
  /** The incident the latest direct read is for. */
  private opening = "";
  private alive = true;

  get readable() {
    return this.host.canAnywhere("incident.read");
  }
  private get scope() {
    return environmentOf(this.host);
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.alive = false;
    this.navigated.unsubscribe();
    this.poller.stop();
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.generation++;
    this.listGeneration++;
    this.incidents = [];
    this.nextCursor = null;
    this.loaded = false;
    this.forbidden = false;
    this.error = null;
    this.selected = null;
    this.detailProblem = null;
    this.opening = "";
    this.events = noEvents;
    this.resolving = false;
    this.failure = null;
    this.pending.clear();
    this.pages = 1;
    this.appending = false;
    this.rangeProblem = "";
    this.filters = incidentFiltersFromQuery(location.search);
    if (this.scope && this.host.identity && this.readable) {
      void this.poller.refresh();
      void this.openFromAddress();
    }
  }
  @HostListener("window:popstate") locationChanged() {
    this.addressChanged();
  }
  /** The address moved (a link, Back, Forward): filters and the open incident follow. */
  private addressChanged() {
    if (viewFromPath(location.pathname)?.view !== "incidents") return;
    const filters = incidentFiltersFromQuery(location.search);
    if (
      incidentFiltersToQuery(filters) !== incidentFiltersToQuery(this.filters)
    ) {
      this.filters = filters;
      this.changed();
    }
    const id = incidentFromQuery(location.search);
    if (!id) {
      if (this.selected && !this.resolving) this.close(false);
    } else if (this.selected?.id !== id) void this.openFromAddress();
  }
  /** Reads as many pages as are on screen, with the filters' query. */
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return false;
    const generation = this.generation;
    const listGeneration = this.listGeneration;
    const filters = this.filters;
    const current = () =>
      generation === this.generation && listGeneration === this.listGeneration;
    return this.listReads.run(async () => {
      if (!current()) return false;
      try {
        const items: IncidentRecord[] = [];
        let cursor: string | null = null;
        let read = 0;
        do {
          const page: Page<IncidentRecord> = await this.host.api.request(
            `${scope}/incidents?${incidentQuery(filters, cursor)}`,
          );
          if (!current()) return false;
          items.push(...page.items);
          cursor = page.next_cursor;
        } while (cursor && ++read < this.pages);
        this.incidents = items;
        this.nextCursor = cursor;
        this.loaded = true;
        this.forbidden = false;
        this.error = null;
        const fresh = this.selected
          ? items.find((item) => item.id === this.selected!.id)
          : undefined;
        if (fresh && !isUnavailableIncident(fresh) && !this.resolving)
          this.selected = fresh;
        return true;
      } catch (error) {
        if (!current()) return false;
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
        throw error;
      } finally {
        if (this.alive) this.cdr.markForCheck();
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
    const listGeneration = this.listGeneration;
    const filters = this.filters;
    const current = () =>
      generation === this.generation && listGeneration === this.listGeneration;
    this.appending = true;
    try {
      await this.listReads.run(async () => {
        if (!current() || !this.canLoadMore) return;
        const page: Page<IncidentRecord> = await this.host.api.request(
          `${scope}/incidents?${incidentQuery(filters, this.nextCursor)}`,
        );
        if (!current()) return;
        const seen = new Set(this.incidents.map((item) => item.id));
        this.incidents = [
          ...this.incidents,
          ...page.items.filter((item) => !seen.has(item.id)),
        ];
        this.nextCursor = page.next_cursor;
        this.pages++;
        this.forbidden = false;
        this.error = null;
      });
    } catch (error) {
      if (current())
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
    } finally {
      if (current()) this.appending = false;
      if (this.alive) this.cdr.markForCheck();
    }
  }
  suggestions() {
    return incidentSuggestions(this.incidents);
  }
  filterCount() {
    return incidentFilterCount(this.filters);
  }
  /** "3 incidents", or "50 incidents loaded" while more follow. */
  countText() {
    const count = this.incidents.length;
    const noun = count === 1 ? "incident" : "incidents";
    return this.nextCursor ? `${count} ${noun} loaded` : `${count} ${noun}`;
  }
  setFilters(change: Partial<IncidentFilters>) {
    const next = { ...this.filters, ...change } as IncidentFilters;
    next.code = next.code.trim();
    next.workflow = next.workflow.trim();
    next.nodeId = next.nodeId.trim();
    if (change === this.defaults) this.rangeProblem = "";
    if (incidentFiltersToQuery(next) === incidentFiltersToQuery(this.filters))
      return;
    this.filters = next;
    this.syncAddress();
    this.changed();
  }
  /** A bound of the opening range, from a datetime-local field. */
  setOpened(edge: "openedAfter" | "openedBefore", value: string) {
    const date = value ? new Date(value) : null;
    const instant =
      date && Number.isFinite(date.getTime()) ? date.toISOString() : "";
    const next = { ...this.filters, [edge]: instant };
    const after = next.openedAfter ? Date.parse(next.openedAfter) : null;
    const before = next.openedBefore ? Date.parse(next.openedBefore) : null;
    if (after !== null && before !== null && after >= before) {
      this.rangeProblem = "Choose a start before the end.";
      return;
    }
    if (after !== null && before !== null && before - after > MAX_RANGE_MS) {
      this.rangeProblem = "Choose a range of 400 days or less.";
      return;
    }
    this.rangeProblem = "";
    this.setFilters({ [edge]: instant });
  }
  /** "2026-10-07T12:30" in this computer's time zone, for a datetime-local field. */
  localTime(value: string) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return "";
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }
  /** New filters: the first page again; Load more pages and older answers go. */
  private changed() {
    this.listGeneration++;
    this.pages = 1;
    this.nextCursor = null;
    this.appending = false;
    if (this.scope && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  /** Puts the filters and the open incident in the address. */
  private syncAddress() {
    const path =
      viewPath("incidents") +
      incidentFiltersToQuery(this.filters, this.selected?.id ?? "");
    if (location.pathname + location.search !== path)
      void this.router.navigateByUrl(path, { replaceUrl: true });
  }
  open(incident: IncidentView) {
    this.selected = incident;
    this.detailProblem = null;
    this.opening = "";
    this.resolving = false;
    this.failure = null;
    this.syncAddress();
    void this.loadEvents(incident);
  }
  close(sync = true) {
    this.selected = null;
    this.opening = "";
    this.resolving = false;
    this.failure = null;
    if (sync) this.syncAddress();
  }
  /**
   * Opens the incident the address names from incidents.read, whether or not
   * a loaded page lists it. A refusal or a missing incident is explained
   * above the list.
   */
  async openFromAddress() {
    const scope = this.scope;
    const id = incidentFromQuery(location.search);
    if (!scope || !id || !this.host.identity || !this.readable) return;
    const generation = this.generation;
    this.opening = id;
    this.detailProblem = null;
    try {
      const incident = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(id)}`,
      );
      if (generation !== this.generation || this.opening !== id) return;
      this.opening = "";
      if (isUnavailableIncident(incident)) {
        this.detailProblem = {
          message:
            "Studio can't show this incident: its run can't be shown here.",
          code: "",
          status: 0,
        };
        return;
      }
      this.selected = incident;
      this.resolving = false;
      this.failure = null;
      void this.loadEvents(incident);
    } catch (error) {
      if (generation !== this.generation || this.opening !== id) return;
      this.opening = "";
      this.detailProblem = describeError(error);
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  openRun(incident: IncidentView) {
    void this.host.viewRun({ id: incident.run_id });
  }
  /** The run's last 20 engine events: types and sequence numbers only. */
  async loadEvents(incident: IncidentView) {
    const scope = this.scope;
    if (!scope) return;
    const generation = this.generation;
    this.events = { ...noEvents, loading: true };
    try {
      const events: { sequence: number; type: string }[] = [];
      let cursor: string | null = null;
      let pages = 0;
      do {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("cursor", cursor);
        const page: {
          events: { sequence: number; type: string }[];
          next_cursor: string | null;
        } = await this.host.api.request(
          `${scope}/runs/${encodeURIComponent(incident.run_id)}/history?${query}`,
        );
        events.push(...page.events);
        cursor = page.next_cursor;
      } while (cursor && ++pages < EVENT_PAGES);
      if (generation !== this.generation || this.selected?.id !== incident.id)
        return;
      this.events = {
        loading: false,
        lines: lastEvents(events),
        truncated: !!cursor,
        problem: null,
      };
    } catch (error) {
      if (generation === this.generation && this.selected?.id === incident.id)
        this.events = { ...noEvents, problem: describeError(error) };
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  startResolving() {
    this.resolving = true;
    this.failure = null;
  }
  /**
   * Sends a decision with the incident's revision as If-Match and a receipt.
   * The answer belongs to the incident it was for: when the person has moved
   * on (another incident, the drawer closed, another page), it never touches
   * what is on screen, and a short notice says what happened instead.
   */
  async resolve(request: ResolutionRequest) {
    const incident = this.selected;
    const scope = this.scope;
    if (!incident || !scope || this.sending.has(incident.id)) return;
    const generation = this.generation;
    const key = JSON.stringify([incident.revision, request]);
    const lost = this.pending.get(incident.id);
    const receipt = lost?.key === key ? lost.receipt : crypto.randomUUID();
    this.pending.set(incident.id, { key, receipt });
    this.sending.add(incident.id);
    this.failure = null;
    const where = `${incident.code} at ${this.stepOf(incident)}`;
    // Still this page, environment and person; and this incident still open.
    const samePage = () => this.alive && generation === this.generation;
    const stillOpen = () => samePage() && this.selected?.id === incident.id;
    try {
      const resolved = await this.host.api.request<IncidentView>(
        `${scope}/incidents/${encodeURIComponent(incident.id)}/resolve`,
        "POST",
        { receipt_id: receipt, ...request },
        { "If-Match": `"${incident.revision}"` },
      );
      if (this.pending.get(incident.id)?.receipt === receipt)
        this.pending.delete(incident.id);
      if (stillOpen()) {
        const dialogOpen = this.resolving;
        this.resolving = false;
        this.selected = resolved;
        this.host.notify("Incident resolved.");
        void this.loadEvents(resolved);
        // The dialog's button is gone: the incident's title takes the focus.
        if (dialogOpen)
          setTimeout(() =>
            document
              .querySelector<HTMLElement>("#incident-detail-title")
              ?.focus(),
          );
      } else {
        // The person has moved on: nothing on screen changes but the list.
        this.host.notify(`Incident ${where} is resolved.`);
        if (!samePage()) return;
      }
      // A decided incident the status filter no longer matches leaves the
      // list at once, as the platform's next page would leave it out.
      const listed =
        this.filters.status === "all" ||
        resolved.status === this.filters.status;
      this.incidents = this.incidents.flatMap((item) =>
        item.id !== resolved.id ? [item] : listed ? [resolved] : [],
      );
      // Not while the person decides on another incident: reading the list
      // would change the revision that decision is sent with.
      if (!this.resolving) void this.poller.refresh();
    } catch (error) {
      const plain = describeError(error);
      const unknown =
        plain.status === 0 || plain.status === 408 || plain.status >= 500;
      // A refusal is final for that receipt; a lost answer keeps it.
      if (!unknown && this.pending.get(incident.id)?.receipt === receipt)
        this.pending.delete(incident.id);
      const explained = unknown
        ? {
            ...plain,
            message:
              "Studio didn't get an answer, so the decision may have been recorded. Send it again unchanged: the same receipt makes that safe.",
          }
        : plain.code === "WV-INCIDENT-REVISION"
          ? {
              ...plain,
              message:
                "Someone changed this incident since you opened it. Refresh it, check it again, then decide.",
            }
          : plain;
      if (stillOpen() && this.resolving) this.failure = explained;
      else
        this.host.notify(
          unknown
            ? `Studio didn't get an answer about incident ${where}: the decision may have been recorded.`
            : `Incident ${where} was not resolved. ${plain.message}`,
        );
    } finally {
      this.sending.delete(incident.id);
      if (this.alive) this.cdr.markForCheck();
    }
  }
  /**
   * Refresh incident: reads the incident again (incidents.read) so the dialog
   * decides on its current revision, whether or not a loaded page lists it.
   * A read that fails, or finds it gone, says so in the dialog instead of
   * leaving the old revision to fail again.
   */
  async reloadIncident() {
    const incident = this.selected;
    const scope = this.scope;
    if (!incident || !scope) return;
    const generation = this.generation;
    this.failure = null;
    try {
      const fresh = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(incident.id)}`,
      );
      if (
        !this.alive ||
        generation !== this.generation ||
        this.selected?.id !== incident.id ||
        !this.resolving
      )
        return;
      if (isUnavailableIncident(fresh))
        this.failure = {
          message:
            "Studio can't show this incident any more. Close this dialog and look for it in the list.",
          code: "",
          status: 0,
        };
      else {
        this.selected = fresh;
        this.incidents = this.incidents.map((item) =>
          item.id === fresh.id ? fresh : item,
        );
      }
    } catch (error) {
      if (
        !this.alive ||
        generation !== this.generation ||
        this.selected?.id !== incident.id ||
        !this.resolving
      )
        return;
      const plain = describeError(error);
      this.failure =
        plain.status === 404
          ? {
              ...plain,
              message:
                "Studio couldn't find this incident when it read it again. Close this dialog and look for it in the list.",
            }
          : plain;
    } finally {
      this.cdr.markForCheck();
    }
  }
  unavailable(item: IncidentRecord) {
    return isUnavailableIncident(item);
  }
  view(item: IncidentRecord) {
    return item as IncidentView;
  }
  stepOf(incident: IncidentView) {
    return incident.node_id || "the run";
  }
  label(incident: IncidentView) {
    return incidentStatusLabel(incident.status);
  }
  tone(incident: IncidentView) {
    return toneAttribute(incidentTone(incident.status));
  }
  workflowText(incident: IncidentView) {
    return incident.workflow
      ? `${incident.workflow.name} ${incident.workflow.version}`
      : "Unknown workflow";
  }
  /** A relative time, or nothing for a value that isn't a date. */
  relative(value: string) {
    return timeRelative(value, "");
  }
  value(event: Event) {
    return (event.target as HTMLSelectElement).value as never;
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
}
