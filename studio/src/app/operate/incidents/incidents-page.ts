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
// Operate › Incidents: active incidents first, filters kept in the address,
// a drawer with what happened, and the Resolve incident dialog. The server
// checks incident.resolve, the revision and the receipt on every decision.
// Reloads every 15 s while visible, except while a decision is being made.
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
import { OperateState } from "../operate-state";
import { viewFromPath, viewPath } from "../operate-routes";
import { Poller, browserEnvironment } from "../operate-store";
import { RefreshStatus } from "../refresh-status";
import { IncidentDrawer, type RecentEvents } from "./incident-drawer";
import {
  defaultIncidentFilters,
  filterIncidents,
  incidentCodes,
  incidentFiltersFromQuery,
  incidentFiltersToQuery,
  incidentStatusLabel,
  incidentTone,
  isUnavailableIncident,
  lastEvents,
  type IncidentFilters,
  type IncidentRecord,
  type IncidentView,
  type ResolutionRequest,
} from "./incidents-model";
import { ResolveIncidentDialog } from "./resolve-incident-dialog";

/** Polls reload at most this many pages of 50. */
const MAX_PAGES = 10;
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
      <div class="operate-filters" role="search" aria-label="Filter incidents">
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
          <select
            id="incident-code"
            (change)="setFilters({ code: value($event) })"
          >
            <option value="" [selected]="!filters.code">Any code</option>
            @for (code of codes(); track code) {
              <option [value]="code" [selected]="filters.code === code">
                {{ code }}
              </option>
            }
          </select>
        </div>
        <span class="operate-count" role="status">{{ countText() }}</span>
      </div>
      @if (error) {
        <weave-operate-state
          kind="partial"
          [message]="error.message"
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      }
      <div class="resource-layout" [class.has-detail]="!!selected">
        <div class="incident-list">
          @if (!visible().length) {
            @if (filters.status === "active" && !filters.code) {
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
                  ><span role="columnheader" data-wide>Step</span
                  ><span role="columnheader" data-wide>Run</span>
                </div>
              </div>
              <div role="rowgroup">
                @for (item of visible(); track item.id) {
                  @if (unavailable(item)) {
                    <div class="resource-row" role="row">
                      <span role="cell" class="row-text"
                        ><strong>Unavailable incident</strong
                        ><small>Studio can't show this incident.</small></span
                      ><span role="cell"></span
                      ><span role="cell" data-wide></span
                      ><span role="cell" data-wide></span>
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
                        @if (i.external_effects_may_continue) {
                          <span class="effects" title="Effects may continue"
                            ><weave-icon name="warning" [size]="16" /><span
                              class="sr-only"
                              >Effects may continue</span
                            ></span
                          >
                        }</span
                      ><span role="cell" data-wide>{{ stepOf(i) }}</span
                      ><span role="cell" data-wide
                        ><code [attr.title]="i.run_id">{{
                          short(i.run_id)
                        }}</code></span
                      >
                    </div>
                  }
                }
              </div>
            </div>
            @if (nextCursor) {
              <button type="button" class="load-more" (click)="loadMore()">
                Load more incidents
              </button>
            }
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
  readonly short = shortId;
  readonly defaults = defaultIncidentFilters;
  readonly statusOptions: [IncidentFilters["status"], string][] = [
    ["active", "Active"],
    ["resolved", "Resolved"],
    ["closed", "Closed"],
    ["all", "Any status"],
  ];
  // A reload never changes the incident under an open Resolve incident: the
  // revision the decision is sent with is the one the person looked at.
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
  private scopeKey = "";
  private generation = 0;
  private alive = true;

  get readable() {
    return this.host.canAnywhere("incident.read");
  }
  private get scope() {
    try {
      return this.host.profile ? this.host.api.environment : "";
    } catch {
      return "";
    }
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
    const key = JSON.stringify([
      this.scope,
      this.host.identity,
      this.host.signInEnded,
    ]);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.generation++;
    this.incidents = [];
    this.nextCursor = null;
    this.loaded = false;
    this.forbidden = false;
    this.error = null;
    this.selected = null;
    this.events = noEvents;
    this.resolving = false;
    this.failure = null;
    this.pending.clear();
    this.pages = 1;
    this.filters = incidentFiltersFromQuery(location.search);
    if (this.scope && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  @HostListener("window:popstate") locationChanged() {
    if (viewFromPath(location.pathname)?.view === "incidents")
      this.filters = incidentFiltersFromQuery(location.search);
  }
  /** The Incidents entry or a link moved the address: the filters follow it. */
  private addressChanged() {
    if (viewFromPath(location.pathname)?.view !== "incidents") return;
    const filters = incidentFiltersFromQuery(location.search);
    if (
      incidentFiltersToQuery(filters) !== incidentFiltersToQuery(this.filters)
    )
      this.filters = filters;
  }
  private query(cursor: string | null) {
    const query = new URLSearchParams({ limit: "50" });
    if (cursor) query.set("cursor", cursor);
    return query.toString();
  }
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return;
    const generation = this.generation;
    try {
      const items: IncidentRecord[] = [];
      let cursor: string | null = null;
      let pages = 0;
      do {
        const page: { items: IncidentRecord[]; next_cursor: string | null } =
          await this.host.api.request(
            `${scope}/incidents?${this.query(cursor)}`,
          );
        items.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor && ++pages < this.pages);
      if (generation !== this.generation) return;
      this.incidents = items;
      this.nextCursor = cursor;
      this.loaded = true;
      this.forbidden = false;
      this.error = null;
      const fresh = this.selected
        ? items.find((item) => item.id === this.selected!.id)
        : undefined;
      if (fresh && !isUnavailableIncident(fresh)) this.selected = fresh;
    } catch (error) {
      if (generation !== this.generation) return;
      this.error = describeError(error);
      this.forbidden = this.error.status === 403;
      throw error;
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  async loadMore() {
    const scope = this.scope;
    const cursor = this.nextCursor;
    if (!scope || !cursor) return;
    const generation = this.generation;
    try {
      const page: { items: IncidentRecord[]; next_cursor: string | null } =
        await this.host.api.request(`${scope}/incidents?${this.query(cursor)}`);
      if (generation !== this.generation) return;
      this.incidents = [...this.incidents, ...page.items];
      this.nextCursor = page.next_cursor;
      this.pages = Math.min(this.pages + 1, MAX_PAGES);
    } catch (error) {
      if (generation === this.generation) {
        this.error = describeError(error);
        this.forbidden = this.error.status === 403;
      }
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  visible() {
    return filterIncidents(this.incidents, this.filters);
  }
  codes() {
    return incidentCodes(this.incidents);
  }
  countText() {
    const count = this.visible().length;
    return `${count} ${count === 1 ? "incident" : "incidents"}`;
  }
  setFilters(change: Partial<IncidentFilters>) {
    this.filters = { ...this.filters, ...change } as IncidentFilters;
    const path = viewPath("incidents") + incidentFiltersToQuery(this.filters);
    if (location.pathname + location.search !== path)
      void this.router.navigateByUrl(path, { replaceUrl: true });
  }
  open(incident: IncidentView) {
    this.selected = incident;
    this.resolving = false;
    this.failure = null;
    void this.loadEvents(incident);
  }
  close() {
    this.selected = null;
    this.resolving = false;
    this.failure = null;
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
      this.incidents = this.incidents.map((item) =>
        item.id === resolved.id ? resolved : item,
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
   * Refresh incident: reads the incidents again so the dialog decides on the
   * current revision. A read that fails, or no longer finds the incident,
   * says so in the dialog instead of leaving the old revision to fail again.
   */
  async reloadIncident() {
    const incident = this.selected;
    if (!incident) return;
    this.failure = null;
    await this.poller.refresh();
    if (!this.alive || this.selected?.id !== incident.id || !this.resolving)
      return;
    if (this.error) this.failure = this.error;
    else if (!this.incidents.some((item) => item.id === incident.id))
      this.failure = {
        message:
          "Studio couldn't find this incident when it read the list again. Close this dialog and look for it in the list.",
        code: "",
        status: 404,
      };
    this.cdr.markForCheck();
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
  value(event: Event) {
    return (event.target as HTMLSelectElement).value as never;
  }
}
