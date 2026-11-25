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
// lists it. Transactions in doubt come first, in their own band, each case of
// them one group; a code or status filter shows one list. A drawer says what
// happened; Resolve incident, or a decision on a transaction in doubt, is made
// on the revision the person looked at. The server checks the capabilities,
// the revision and the receipt on every decision. Reloads every 15 s while
// visible, except while a decision is being made.
import { NgTemplateOutlet } from "@angular/common";
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  ElementRef,
  HostListener,
  Injector,
  Input,
  OnDestroy,
  OnInit,
  afterEveryRender,
  afterNextRender,
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
import {
  IncidentDrawer,
  type OfferRead,
  type RecentEvents,
} from "./incident-drawer";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { retryDelay } from "../transactions/transaction-record";
import {
  caseLabel,
  inboxRows,
  linkPlace,
  missingText,
  rowGroups,
  type RowGroup,
} from "./incident-cases";
import {
  bandsView,
  defaultIncidentFilters,
  ignoresPriority,
  inDoubt,
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
  mayRefusePriority,
  stepOfIncident,
  type IncidentFilters,
  type IncidentLink,
  type IncidentRecord,
  type IncidentView,
  type ResolutionRequest,
} from "./incidents-model";
import { ResolveIncidentDialog } from "./resolve-incident-dialog";
import type { PartnerRead } from "./transaction-panel";
import { TransactionResolveDialog } from "./transaction-resolve-dialog";
import {
  explainDecision,
  transactionActions,
  type TransactionAction,
} from "./transaction-resolutions";

/** The longest opening range the platform accepts: 400 days. */
const MAX_RANGE_MS = 400 * 86_400_000;
/** What explains why the incident the address names couldn't be opened. */
const UNOPENED = ".incident-unopened";
/** Where the keyboard stops in the list as its rows change: each incident's control and the bands' headings. */
const STOPS = "[data-incident], .band-heading h2";
/** A run's events are read 100 at a time, at most 1,000. */
const EVENT_PAGES = 10;
/** Why an incident can't be read from the drawer. */
const NOT_SHOWN =
  "Studio can't show this incident: its run can't be shown here.";
const READ_REFUSED =
  "You need incident.read in this environment. Ask an administrator in Settings › People and access.";
const NOT_FOUND =
  "This incident can't be found. It may belong to another environment, or its run's data was deleted.";
/** What never changes of an incident, which the platform's answer to a decision leaves out. */
const unchanging = ({
  opened_at,
  opened_at_estimated,
  workflow,
  test,
}: IncidentView) => ({ opened_at, opened_at_estimated, workflow, test });
const noEvents: RecentEvents = {
  loading: false,
  lines: [],
  truncated: false,
  problem: null,
};

/** What a list's pages answered, and the cursor after them. */
interface ListAnswer {
  items: IncidentRecord[];
  next: string | null;
}
/** One list of incidents: what was read, the cursor after it and how many pages that is. */
interface IncidentList extends ListAnswer {
  pages: number;
  appending: boolean;
}
const emptyList = (): IncidentList => ({
  items: [],
  next: null,
  pages: 1,
  appending: false,
});
/** A band on screen: its heading, its count and its rows. */
interface Band {
  id: "high" | "other";
  title: string;
  count: string;
  groups: RowGroup[];
  list: IncidentList;
}

@Component({
  selector: "weave-incidents-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    NgTemplateOutlet,
    Icon,
    ModalSheet,
    OperateState,
    RefreshStatus,
    IncidentDrawer,
    ResolveIncidentDialog,
    TransactionResolveDialog,
  ],
  styleUrls: ["../operate.css", "./incidents.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1 tabindex="-1">Incidents</h1>
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
            class="incident-unopened"
            kind="access"
            heading="You don't have access to this incident"
            capability="incident.read"
          >
            <button type="button" tryAgain (click)="openFromAddress(true)">
              Try again
            </button>
          </weave-operate-state>
        } @else if (failed.status === 404) {
          <weave-operate-state
            class="incident-unopened"
            kind="empty"
            heading="This incident can't be found"
            text="It may belong to another environment, or its run's data was deleted."
          >
            <button type="button" (click)="openFromAddress(true)">
              Try again
            </button>
          </weave-operate-state>
        } @else {
          <weave-operate-state
            class="incident-unopened"
            kind="error"
            [message]="failed.message"
            [code]="failed.code"
            (retry)="openFromAddress(true)"
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
          } @else if (bands(); as shown) {
            @for (band of shown; track band.id) {
              <section
                class="incident-band"
                [class.high]="band.id === 'high'"
                [attr.aria-labelledby]="'incident-band-' + band.id"
              >
                <div class="band-heading">
                  <h2 [id]="'incident-band-' + band.id" tabindex="-1">
                    @if (band.id === "high") {
                      <weave-icon name="critical" [size]="16" />
                    }
                    {{ band.title }}
                  </h2>
                  <span class="band-count">{{ band.count }}</span>
                </div>
                @if (band.id === "high") {
                  <p class="band-note">
                    Transactions in doubt. Other systems may not match Weave
                    until someone resolves them.
                  </p>
                }
                <div
                  class="resource-table incident-table"
                  role="table"
                  [attr.aria-labelledby]="'incident-band-' + band.id"
                >
                  <ng-container [ngTemplateOutlet]="tableHead" />
                  @for (group of band.groups; track group.key) {
                    @if (group.case; as value) {
                      <div
                        class="case"
                        role="rowgroup"
                        [attr.aria-labelledby]="'incident-case-' + value.key"
                      >
                        <div class="case-head" role="row">
                          <span role="cell" [attr.aria-colspan]="6"
                            ><weave-icon name="callWorkflow" [size]="16" /><span
                              class="case-words"
                              ><strong [id]="'incident-case-' + value.key">{{
                                caseLabel(value)
                              }}</strong>
                              @if (value.resolveFirst; as first) {
                                <span
                                  >Resolve first: <code>{{ first.code }}</code>
                                  in run
                                  <code>{{ short(first.run_id) }}</code></span
                                >
                              }
                            </span></span
                          >
                        </div>
                        @for (member of value.members; track member.id) {
                          <ng-container
                            [ngTemplateOutlet]="incidentRow"
                            [ngTemplateOutletContext]="{
                              $implicit: member,
                              banded: true,
                              first: value.resolveFirst?.id === member.id,
                            }"
                          />
                        }
                        @for (link of value.missing; track link.id) {
                          <div class="resource-row not-listed" role="row">
                            <span role="cell" class="row-text"
                              ><strong>{{ link.code }}</strong
                              ><small>{{ missingText(link) }}</small></span
                            ><span role="cell" class="badges"
                              ><span
                                class="status-pill"
                                [attr.data-tone]="tone(link)"
                                >{{ label(link) }}</span
                              >
                              @if (value.resolveFirst?.id === link.id) {
                                <span class="tag">Resolve first</span>
                              }</span
                            ><span role="cell" data-wide class="row-text"
                              ><span
                                class="cell-line"
                                [attr.title]="link.workflow?.name ?? null"
                                >{{ link.workflow?.name ?? "Unknown" }}</span
                              ></span
                            ><span
                              role="cell"
                              data-wide
                              class="cell-line"
                              [attr.title]="linkStep(link)"
                              >{{ linkStep(link) }}</span
                            ><span role="cell" class="open-cell"
                              ><button
                                type="button"
                                class="sm"
                                [attr.data-incident]="link.id"
                                [attr.aria-label]="
                                  'Open incident ' +
                                  link.code +
                                  ', ' +
                                  linkPlace(link)
                                "
                                (click)="openedFrom = link.id; openLink(link)"
                              >
                                Open
                              </button></span
                            ><span role="cell" data-wider
                              ><code [attr.title]="link.run_id">{{
                                short(link.run_id)
                              }}</code></span
                            >
                          </div>
                        }
                      </div>
                    } @else {
                      <div role="rowgroup">
                        @for (item of group.items; track item.id) {
                          <ng-container
                            [ngTemplateOutlet]="incidentRow"
                            [ngTemplateOutletContext]="{
                              $implicit: item,
                              banded: true,
                              first: false,
                            }"
                          />
                        }
                      </div>
                    }
                  }
                </div>
                <ng-container
                  [ngTemplateOutlet]="more"
                  [ngTemplateOutletContext]="{
                    $implicit: band.list,
                    of: 'incident-band-' + band.id,
                  }"
                />
              </section>
            }
          } @else {
            <div
              class="resource-table incident-table"
              role="table"
              aria-label="Incidents"
            >
              <ng-container [ngTemplateOutlet]="tableHead" />
              <div role="rowgroup">
                @for (item of rest.items; track item.id) {
                  <ng-container
                    [ngTemplateOutlet]="incidentRow"
                    [ngTemplateOutletContext]="{
                      $implicit: item,
                      banded: false,
                      first: false,
                    }"
                  />
                }
              </div>
            </div>
            <ng-container
              [ngTemplateOutlet]="more"
              [ngTemplateOutletContext]="{ $implicit: rest, of: null }"
            />
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
            [sheetReturnFocus]="drawerOpener"
            (sheetDismiss)="close()"
          >
            <weave-incident-drawer
              [host]="host"
              [incident]="selected"
              [events]="events"
              (resolve)="startResolving()"
              [offerRead]="offerRead"
              (decide)="startDeciding($event)"
              [partner]="partner"
              (openLink)="openPartner($event)"
              (tryPartner)="readPartner()"
              (openRun)="openRun($event)"
              (reloadEvents)="loadEvents(selected)"
              (closed)="close()"
            />
          </section>
        }
      </div>
    }
    @if (resolving && selected) {
      @if (deciding; as action) {
        <weave-transaction-resolve-dialog
          [incident]="selected"
          [action]="action"
          [busy]="sending.has(selected.id)"
          [failure]="failure"
          [refreshing]="refreshing"
          (decide)="resolve($event)"
          (dismiss)="stopDeciding(action)"
          (refresh)="reloadIncident()"
        />
      } @else {
        <weave-resolve-incident-dialog
          [incident]="selected"
          [busy]="sending.has(selected.id)"
          [failure]="failure"
          (decide)="resolve($event)"
          (cancel)="resolving = false"
          (reload)="reloadIncident()"
        />
      }
    }
    <ng-template #tableHead>
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
    </ng-template>
    <ng-template #incidentRow let-item let-banded="banded" let-first="first">
      @if (unavailable(item)) {
        <div class="resource-row" role="row">
          <span role="cell" class="row-text"
            ><strong>Unavailable incident</strong
            ><small>Studio can't show this incident.</small></span
          ><span role="cell"></span><span role="cell" data-wide></span
          ><span role="cell" data-wide></span><span role="cell" data-wide></span
          ><span role="cell" data-wider
            ><code [attr.title]="item.id">{{ short(item.id) }}</code></span
          >
        </div>
      } @else {
        @let i = view(item);
        @let key = i.transaction?.business_key;
        <div
          class="resource-row"
          role="row"
          [class.selected]="selected?.id === i.id"
        >
          <span role="cell" class="row-text"
            ><button
              type="button"
              class="row-open"
              [attr.data-incident]="i.id"
              [attr.aria-current]="selected?.id === i.id ? 'true' : null"
              [attr.aria-label]="'Open incident ' + i.code + ' at ' + stepOf(i)"
              (click)="openedFrom = i.id; open(i)"
            >
              {{ i.code }}</button
            ><small>
              @if (inDoubt(i) && (key || !banded)) {
                {{ doubtText(banded, !!key) }}
                @if (key) {
                  <span class="business-key">{{ key }}</span>
                }
              } @else if (i.origin_code !== i.code && !inDoubt(i)) {
                First reported as {{ i.origin_code }}
              } @else {
                {{ stepText(i) }}
              }
            </small></span
          ><span role="cell" class="badges"
            ><span class="status-pill" [attr.data-tone]="tone(i)">{{
              label(i)
            }}</span>
            @if (inDoubt(i) && !banded) {
              <span class="status-pill" data-tone="danger">High priority</span>
            }
            @if (first) {
              <span class="tag">Resolve first</span>
            }
            @if (i.test) {
              <span class="tag">Test</span>
            }
            @if (i.external_effects_may_continue) {
              <span class="effects" title="Effects may continue"
                ><weave-icon name="warning" [size]="16" /><span class="sr-only"
                  >Effects may continue</span
                ></span
              >
            }</span
          ><span
            role="cell"
            data-wide
            class="row-text"
            [attr.title]="workflowText(i)"
            ><span class="cell-line" [attr.title]="i.workflow?.name ?? null">{{
              i.workflow?.name ?? "Unknown"
            }}</span>
            @if (i.workflow) {
              <small>{{ i.workflow.version }}</small>
            }</span
          ><span
            role="cell"
            data-wide
            class="cell-line"
            [attr.title]="stepText(i)"
            >{{ stepText(i) }}</span
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
            ><code [attr.title]="i.run_id">{{ short(i.run_id) }}</code></span
          >
        </div>
      }
    </ng-template>
    <ng-template #more let-list let-of="of">
      @if (canLoadMore(list)) {
        <button
          type="button"
          class="load-more"
          [attr.aria-describedby]="of"
          [disabled]="list.appending"
          (click)="loadMore(list)"
        >
          Load more incidents
        </button>
      } @else if (list.next) {
        <p class="list-limit" role="status">
          Showing the first 500 incidents. Narrow the filters to see others.
        </p>
      }
    </ng-template>
  `,
})
export class IncidentsPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);
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
  // A reload never changes the incident under an open decision, not even one
  // already on its way when its dialog opened: the revision the
  // decision is sent with is the one the person looked at. Only Refresh
  // incident, in the dialog, reads it again.
  readonly poller = new Poller(
    () => this.load(),
    15_000,
    browserEnvironment,
    () => this.resolving,
  );
  /** The default view's transactions in doubt; empty while one list shows. */
  high = emptyList();
  /** Every other incident, or the one list a code or status filter shows. */
  rest = emptyList();
  /** What was read is the two bands, not one list. */
  private banded = false;
  /** False once the platform refused to list by priority: one list, until the scope changes. */
  private knowsPriority = true;
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
  /** The decision being made on a transaction in doubt; null in Resolve incident, or with no dialog. */
  deciding: TransactionAction | null = null;
  /** The decision whose incident is read before its dialog opens, or why it couldn't be. */
  offerRead: OfferRead | null = null;
  /** The incident under a decision on a transaction in doubt is being read again. */
  refreshing = false;
  /** Bumped as a dialog opens or closes: an answer for an older one isn't its own. */
  private dialogs = 0;
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
  private readonly listReads = new ListReads();
  /** The related incident being read for the drawer, or why it isn't open. */
  partner: PartnerRead | null = null;
  /** What that read is for: the incident asked for, and the one it was asked in. */
  private asked: {
    id: string;
    from: string;
    failures: number;
    timer: ReturnType<typeof setTimeout> | null;
  } | null = null;

  /** Every incident loaded, transactions in doubt first. */
  get incidents(): IncidentRecord[] {
    return this.high.items.length
      ? [...this.high.items, ...this.rest.items]
      : this.rest.items;
  }
  canLoadMore(list: IncidentList) {
    return !!list.next && list.pages < MAX_PAGES;
  }
  private scopeKey = "";
  /** Bumped when the platform, environment or person changes. */
  private generation = 0;
  /** Bumped when the filters change: an answer for older filters is dropped. */
  private listGeneration = 0;
  /** The incident the latest direct read is for. */
  private opening = "";
  /** The listed incident whose control opened the drawer last. */
  openedFrom = "";
  /** Where that control is, or was while the list drew it. */
  private opener: string[] = [];
  /** Where the control of the list that holds the focus is, as the page is drawn again. */
  private focused: string[] = [];
  private alive = true;

  get readable() {
    return this.host.canAnywhere("incident.read");
  }
  private get scope() {
    return environmentOf(this.host);
  }
  constructor() {
    afterEveryRender({ write: () => this.restoreFocus() });
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.alive = false;
    this.forgetPartner();
    this.navigated.unsubscribe();
    this.poller.stop();
    this.generation++;
  }
  ngDoCheck() {
    this.holdFocus();
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.generation++;
    this.listGeneration++;
    this.high = emptyList();
    this.rest = emptyList();
    this.banded = false;
    this.knowsPriority = true;
    this.loaded = false;
    this.forbidden = false;
    this.error = null;
    this.forgetPartner();
    this.selected = null;
    this.detailProblem = null;
    this.opening = "";
    this.events = noEvents;
    this.leaveDialog();
    this.pending.clear();
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
    // It no longer names the incident that couldn't be opened, or is on its way.
    if (!id || this.selected?.id === id) {
      this.detailProblem = null;
      this.opening = "";
    }
    if (!id) {
      if (this.selected && !this.resolving) this.close(false);
    } else if (this.selected?.id !== id) void this.openFromAddress();
  }
  /**
   * Reads as many pages as are on screen, with the filters' query: the band in
   * doubt and then the rest, or one list. A platform of an earlier release
   * refuses the priority, or ignores it: one list too.
   */
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return false;
    const generation = this.generation;
    const listGeneration = this.listGeneration;
    const filters = this.filters;
    const current = () =>
      generation === this.generation && listGeneration === this.listGeneration;
    /** The cursor of the page being read; null for a first page. */
    let cursor: string | null = null;
    /** A list's pages again; null once the answer is no longer wanted. */
    const read = async (list: IncidentList, priority?: "high" | "normal") => {
      const items: IncidentRecord[] = [];
      let pages = 0;
      cursor = null;
      do {
        const page: Page<IncidentRecord> = await this.host.api.request(
          `${scope}/incidents?${incidentQuery(filters, cursor, priority)}`,
        );
        if (!current()) return null;
        items.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor && ++pages < list.pages);
      return { items, next: cursor };
    };
    return this.listReads.run(async () => {
      if (!current()) return false;
      try {
        const none: ListAnswer = { items: [], next: null };
        let banded = this.knowsPriority && bandsView(filters);
        let high: ListAnswer | null = none;
        if (banded)
          try {
            high = await read(this.high, "high");
          } catch (error) {
            // A cursor it refuses is one it gave: only a first page says it
            // may not know the priority.
            if (
              cursor ||
              !current() ||
              !mayRefusePriority(describeError(error))
            )
              throw error;
            banded = false;
          }
        if (!high) return false;
        const rest = await read(this.rest, banded ? "normal" : undefined);
        if (!rest) return false;
        // Answered without it: the platform refused the priority, not a filter.
        if (!banded && bandsView(filters)) this.knowsPriority = false;
        if (ignoresPriority(high.items, rest.items)) {
          banded = false;
          high = none;
        }
        Object.assign(this.high, high);
        Object.assign(this.rest, rest);
        this.banded = banded;
        this.loaded = true;
        this.forbidden = false;
        this.error = null;
        const fresh = this.selected
          ? this.incidents.find((item) => item.id === this.selected!.id)
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
  /** Continues one list with the same filters; the other's cursor stays. */
  async loadMore(list: IncidentList) {
    const scope = this.scope;
    if (
      !scope ||
      !this.host.identity ||
      !this.readable ||
      !this.canLoadMore(list) ||
      list.appending
    )
      return;
    const generation = this.generation;
    const listGeneration = this.listGeneration;
    const filters = this.filters;
    const priority = !this.banded
      ? undefined
      : list === this.high
        ? "high"
        : "normal";
    const current = () =>
      generation === this.generation && listGeneration === this.listGeneration;
    list.appending = true;
    try {
      await this.listReads.run(async () => {
        if (!current() || !this.canLoadMore(list)) return;
        const page: Page<IncidentRecord> = await this.host.api.request(
          `${scope}/incidents?${incidentQuery(filters, list.next, priority)}`,
        );
        if (!current()) return;
        const seen = new Set(list.items.map((item) => item.id));
        list.items = [
          ...list.items,
          ...page.items.filter((item) => !seen.has(item.id)),
        ];
        list.next = page.next_cursor;
        list.pages++;
        this.forbidden = false;
        this.error = null;
      });
    } catch (error) {
      if (current())
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
    } finally {
      if (current()) list.appending = false;
      if (this.alive) this.cdr.markForCheck();
    }
  }
  /** The rows last drawn, kept while the incidents they come from are the same. */
  private drawn: {
    high: IncidentRecord[];
    rest: IncidentRecord[];
    groups: { high: RowGroup[]; rest: RowGroup[] };
  } | null = null;
  private groups() {
    const high = this.high.items;
    const rest = this.rest.items;
    if (this.drawn?.high !== high || this.drawn.rest !== rest) {
      const rows = inboxRows(high, rest);
      this.drawn = {
        high,
        rest,
        groups: { high: rowGroups(rows.high), rest: rowGroups(rows.normal) },
      };
    }
    return this.drawn.groups;
  }
  /** The bands on screen; null while nothing is in doubt, or one list shows. */
  bands(): Band[] | null {
    const groups = this.groups();
    if (!groups.high.length) return null;
    const band = (
      id: Band["id"],
      title: string,
      list: IncidentList,
      rows: RowGroup[],
    ): Band => ({
      id,
      title,
      count: this.bandCount(rows, list),
      groups: rows,
      list,
    });
    return [
      band("high", "High priority", this.high, groups.high),
      ...(groups.rest.length || this.rest.next
        ? [band("other", "Other incidents", this.rest, groups.rest)]
        : []),
    ];
  }
  /** "4 incidents", or "50 shown" while more follow. */
  private bandCount(groups: RowGroup[], list: IncidentList) {
    const count = groups.reduce(
      (total, group) =>
        total + (group.case ? group.case.members : group.items).length,
      0,
    );
    if (list.next) return `${count} shown`;
    return `${count} ${count === 1 ? "incident" : "incidents"}`;
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
    return this.high.next || this.rest.next
      ? `${count} ${noun} loaded`
      : `${count} ${noun}`;
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
    for (const list of [this.high, this.rest])
      Object.assign(list, { next: null, pages: 1, appending: false });
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
    this.forgetPartner();
    this.selected = incident;
    this.detailProblem = null;
    this.opening = "";
    this.leaveDialog();
    this.syncAddress();
    void this.loadEvents(incident);
  }
  /** Opens another incident of a case: the one loaded, or through the address when no page lists it. */
  openLink(link: IncidentLink) {
    this.forgetPartner();
    const loaded = this.incidents.find((item) => item.id === link.id);
    if (loaded && !isUnavailableIncident(loaded)) this.open(loaded);
    // The address names it already, so nothing would move.
    else if (incidentFromQuery(location.search) === link.id)
      void this.openFromAddress(true);
    else
      void this.router.navigateByUrl(
        viewPath("incidents") + incidentFiltersToQuery(this.filters, link.id),
        { replaceUrl: true },
      );
  }
  close(sync = true) {
    this.forgetPartner();
    this.selected = null;
    this.opening = "";
    this.leaveDialog();
    if (sync) this.syncAddress();
  }
  /**
   * Opens the incident the address names from incidents.read, whether or not
   * a loaded page lists it; one on its way isn't read twice. A refusal or a
   * missing incident is explained above the list, where it stays while it is
   * read `again`.
   */
  async openFromAddress(again = false) {
    const scope = this.scope;
    const id = incidentFromQuery(location.search);
    if (!scope || !id || !this.host.identity || !this.readable) return;
    this.forgetPartner();
    if (this.opening === id) return;
    const generation = this.generation;
    this.opening = id;
    if (!again) this.detailProblem = null;
    try {
      const incident = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(id)}`,
      );
      if (generation !== this.generation || this.opening !== id) return;
      this.opening = "";
      if (isUnavailableIncident(incident)) {
        this.explainUnopened({ message: NOT_SHOWN, code: "", status: 0 });
        return;
      }
      this.forgetPartner();
      this.selected = incident;
      this.leaveDialog();
      // The Try again it was asked from goes.
      if (document.activeElement?.closest(UNOPENED))
        afterNextRender(() => this.focusTitle(), { injector: this.injector });
      this.detailProblem = null;
      void this.loadEvents(incident);
    } catch (error) {
      if (generation !== this.generation || this.opening !== id) return;
      this.opening = "";
      this.explainUnopened(describeError(error));
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  /** Why the incident couldn't be opened; another kind of failure draws another Try again. */
  private explainUnopened(problem: PlainError) {
    if (document.activeElement?.closest(UNOPENED)) {
      const page = this.element.nativeElement;
      const again = `${UNOPENED} button`;
      afterNextRender(() => page.querySelector<HTMLElement>(again)?.focus(), {
        injector: this.injector,
      });
    }
    this.detailProblem = problem;
  }
  openRun(id: string) {
    void this.host.viewRun({ id });
  }
  /**
   * Opens a related incident from the drawer: the one loaded, or the one
   * incidents.read answers when no page lists it. A second press while it is
   * on its way reads nothing.
   */
  openPartner(link: IncidentLink) {
    const from = this.selected;
    if (!from || (this.partner?.id === link.id && this.partner.reading)) return;
    const loaded = this.incidents.find((item) => item.id === link.id);
    if (loaded && !isUnavailableIncident(loaded)) {
      this.open(loaded);
      this.focusTitle();
      return;
    }
    this.offerRead = null;
    // Asked again after a failure, it keeps how long the next wait is.
    if (this.asked?.id !== link.id) {
      this.forgetPartner();
      this.asked = { id: link.id, from: from.id, failures: 0, timer: null };
    }
    void this.readPartner();
  }
  /**
   * Reads the related incident asked for, for the incident it was asked in
   * only. A failed read waits 5 s, doubling to 60 s; a refusal or an incident
   * that can't be found is final. A retry nobody pressed (`waited`) moves the
   * focus only within the drawer.
   */
  async readPartner(waited = false) {
    const scope = this.scope;
    const asked = this.asked;
    if (!scope || !asked || this.partner?.reading) return;
    if (asked.timer !== null) clearTimeout(asked.timer);
    asked.timer = null;
    const generation = this.generation;
    const current = () =>
      this.alive &&
      generation === this.generation &&
      this.asked === asked &&
      this.selected?.id === asked.from;
    const say = (message: string, code = "", again = false) => {
      this.partner = {
        id: asked.id,
        reading: false,
        problem: { message, code, again },
      };
      if (!again) this.leaveTryNow(asked.id);
    };
    // What went wrong stays while it is read again, and Try now its focus.
    const problem = this.partner?.problem ?? null;
    this.partner = { id: asked.id, reading: true, problem };
    try {
      const incident = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(asked.id)}`,
      );
      if (!current()) return;
      if (isUnavailableIncident(incident)) say(NOT_SHOWN);
      else {
        const takesFocus =
          !waited || !!document.activeElement?.closest(".record-detail");
        this.open(incident);
        if (takesFocus) this.focusTitle();
      }
    } catch (error) {
      if (!current()) return;
      const plain = describeError(error);
      if (plain.status === 403) say(READ_REFUSED, plain.code);
      else if (plain.status === 404) say(NOT_FOUND, plain.code);
      else {
        const wait = retryDelay(++asked.failures);
        say(
          `Studio couldn't read this incident. It tries again in ${wait / 1000} s.`,
          plain.code,
          true,
        );
        asked.timer = setTimeout(() => {
          asked.timer = null;
          if (current()) void this.readPartner(true);
        }, wait);
      }
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  /** Another incident, a decision or no drawer: what was asked for in this one is no longer wanted. */
  private forgetPartner() {
    if (this.asked?.timer) clearTimeout(this.asked.timer);
    this.asked = null;
    this.partner = null;
    this.offerRead = null;
  }
  /** Try now goes once an answer is final: its link's Open takes the focus. */
  private leaveTryNow(id: string) {
    if (!document.activeElement?.closest(".record-detail .txn-links .notice"))
      return;
    const open = `.record-detail [data-incident="${CSS.escape(id)}"]`;
    const page = this.element.nativeElement;
    afterNextRender(() => page.querySelector<HTMLElement>(open)?.focus(), {
      injector: this.injector,
    });
  }
  /** An incident's control in the list: its row, or its Open while no page lists it. */
  private control(id: string): HTMLElement | null {
    const control = `.incident-list [data-incident="${CSS.escape(id)}"]`;
    const page = this.element.nativeElement;
    return (
      page.querySelector<HTMLElement>(`${control}.row-open`) ??
      page.querySelector<HTMLElement>(control)
    );
  }
  /**
   * Where a control of the list is, as the stops to look for once it is gone:
   * itself, what follows it in its band, the band's heading, then the rest of
   * the list, the nearest row first. A row is named by its incident, a heading
   * by its ID.
   */
  private placeOf(control: HTMLElement): string[] {
    const list = control.closest(".incident-list");
    const band = control.closest(".incident-band");
    const stops = [...(list?.querySelectorAll<HTMLElement>(STOPS) ?? [])];
    const at = stops.indexOf(control);
    const before = stops.slice(0, at).reverse();
    const after = stops.slice(at + 1);
    return [
      control,
      ...after.filter((stop) => band?.contains(stop)),
      ...stops.filter((stop) => band?.contains(stop) && stop.id),
      ...after,
      ...before,
    ].map((stop) => stop.dataset["incident"] ?? stop.id);
  }
  /** The first of these stops the list still draws, or the page's title. */
  private land(place: string[]): HTMLElement | null {
    const page = this.element.nativeElement;
    for (const stop of place) {
      const drawn =
        this.control(stop) ??
        page.querySelector<HTMLElement>(`.incident-band #${CSS.escape(stop)}`);
      if (drawn) return drawn;
    }
    return place.length ? page.querySelector("h1") : null;
  }
  /** Closing the drawer over the list returns to the control that opened it, or to the next one once it is gone. */
  readonly drawerOpener = () => this.land(this.opener);
  private holdFocus() {
    const focused = document.activeElement?.closest<HTMLElement>(
      `.incident-list :is(${STOPS})`,
    );
    if (focused) this.focused = this.placeOf(focused);
    const opener =
      this.selected && this.openedFrom ? this.control(this.openedFrom) : null;
    if (opener) this.opener = this.placeOf(opener);
  }
  /**
   * A row can be drawn elsewhere (one list turns into bands, a case forms
   * around it) or leave the list. The focus it took along goes to its
   * incident's control there, or to the next stop.
   */
  private restoreFocus() {
    if (this.focused.length && document.activeElement === document.body)
      this.land(this.focused)?.focus({ preventScroll: true });
    this.focused = [];
  }
  /** The control that opened the incident is gone: its title takes the focus. */
  private focusTitle() {
    setTimeout(() =>
      document.querySelector<HTMLElement>("#incident-detail-title")?.focus(),
    );
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
    this.dropAsked();
    this.leaveDialog();
    this.resolving = true;
  }
  /**
   * Opens a decision on the transaction in doubt. A retry decides on the
   * drawer's record; any other reads the incident first, so the checklist,
   * what stays done and the revision it is sent with are the platform's now.
   * The last one pressed opens, for the incident it was pressed in only.
   */
  async startDeciding(action: TransactionAction) {
    const incident = this.selected;
    const scope = this.scope;
    if (!incident || !scope || this.resolving) return;
    if (this.offerRead?.reading && this.offerRead.action === action) return;
    this.dropAsked();
    if (action === "retry") return this.openDecision(action);
    const read: OfferRead = { action, reading: true, problem: null };
    this.offerRead = read;
    const generation = this.generation;
    const current = () =>
      this.alive &&
      generation === this.generation &&
      this.offerRead === read &&
      this.selected?.id === incident.id;
    const say = (message: string, code = "") => {
      this.offerRead = { action, reading: false, problem: { message, code } };
    };
    try {
      const fresh = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(incident.id)}`,
      );
      if (!current()) return;
      this.offerRead = null;
      if (isUnavailableIncident(fresh)) say(NOT_SHOWN);
      else if (this.readAgain(fresh, action)) this.openDecision(action);
    } catch (error) {
      if (!current()) return;
      const plain = describeError(error);
      say(
        plain.status === 403
          ? READ_REFUSED
          : plain.status === 404
            ? NOT_FOUND
            : "Studio couldn't read this incident. Choose again to read it.",
        plain.code,
      );
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  private openDecision(action: TransactionAction) {
    this.leaveDialog();
    this.resolving = true;
    this.deciding = action;
  }
  /** An incident on its way never opens under a decision. */
  private dropAsked() {
    this.forgetPartner();
    if (!this.opening) return;
    this.opening = "";
    this.syncAddress();
  }
  /** Closing a decision returns to its offer. */
  stopDeciding(action: TransactionAction) {
    this.leaveDialog();
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>(`[data-offer="${action}"]`)
        ?.focus(),
    );
  }
  /** No dialog is open any more, or another one opens. */
  private leaveDialog() {
    this.resolving = false;
    this.deciding = null;
    this.failure = null;
    this.refreshing = false;
    this.dialogs++;
  }
  /**
   * The incident under a decision as just read: whether the decision still
   * fits it. Otherwise the drawer says how it stands now, and no dialog does.
   */
  private readAgain(fresh: IncidentView, action: TransactionAction): boolean {
    this.selected = fresh;
    this.relist(fresh);
    const offer = transactionActions(fresh, (capability) =>
      this.host.can(capability),
    ).find((item) => item.action === action);
    if (offer && !offer.missing) return true;
    this.leaveDialog();
    if (!inDoubt(fresh))
      this.host.notify(`Incident ${this.whereOf(fresh)} is no longer active.`);
    void this.loadEvents(fresh);
    this.focusTitle();
    return false;
  }
  /**
   * An incident as just read or decided, in the lists that hold it. One the
   * status filter no longer matches leaves them at once, as the platform's
   * next page would leave it out.
   */
  private relist(fresh: IncidentView) {
    const listed =
      this.filters.status === "all" || fresh.status === this.filters.status;
    for (const list of [this.high, this.rest])
      list.items = list.items.flatMap((item) =>
        item.id !== fresh.id ? [item] : listed ? [fresh] : [],
      );
  }
  private whereOf(incident: IncidentView) {
    return `${incident.code} at ${this.stepOf(incident)}`;
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
    const where = this.whereOf(incident);
    const offer = this.deciding
      ? transactionActions(incident, () => true).find(
          (item) => item.action === this.deciding,
        )
      : null;
    const dialog = this.dialogs;
    // Still this page, environment and person; and this incident still open.
    const samePage = () => this.alive && generation === this.generation;
    const stillOpen = () => samePage() && this.selected?.id === incident.id;
    // A decision on a transaction in doubt is refused in its own dialog only.
    const stillAsked = () =>
      stillOpen() && this.resolving && (!offer || dialog === this.dialogs);
    try {
      const row = await this.host.api.request<IncidentView>(
        `${scope}/incidents/${encodeURIComponent(incident.id)}/resolve`,
        "POST",
        { receipt_id: receipt, ...request },
        { "If-Match": `"${incident.revision}"` },
      );
      if (this.pending.get(incident.id)?.receipt === receipt)
        this.pending.delete(incident.id);
      const resolved = { ...unchanging(incident), ...row };
      if (stillOpen()) {
        const dialogOpen = this.resolving;
        this.leaveDialog();
        this.selected = resolved;
        this.host.notify("Incident resolved.");
        void this.loadEvents(resolved);
        void this.readDecided(resolved);
        // The dialog's button is gone.
        if (dialogOpen) this.focusTitle();
      } else {
        // The person has moved on: nothing on screen changes but the list.
        this.host.notify(`Incident ${where} is resolved.`);
        if (!samePage()) return;
      }
      this.relist(resolved);
      // Not while the person decides on another incident: reading the list
      // would change the revision that decision is sent with.
      if (!this.resolving) void this.poller.refresh();
    } catch (error) {
      const plain = offer
        ? explainDecision(describeError(error), offer)
        : describeError(error);
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
      if (stillAsked()) this.failure = explained;
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
   * leaving the old revision to fail again. A decision on a transaction in
   * doubt keeps why it was refused while it is read, and closes when it no
   * longer fits.
   */
  async reloadIncident() {
    const incident = this.selected;
    const scope = this.scope;
    const action = this.deciding;
    if (!incident || !scope || this.refreshing) return;
    const generation = this.generation;
    const dialog = this.dialogs;
    if (action) this.refreshing = true;
    else this.failure = null;
    const current = () =>
      this.alive &&
      generation === this.generation &&
      this.selected?.id === incident.id &&
      this.resolving &&
      (!action || dialog === this.dialogs);
    try {
      const fresh = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(incident.id)}`,
      );
      if (!current()) return;
      if (isUnavailableIncident(fresh))
        this.failure = {
          message:
            "Studio can't show this incident any more. Close this dialog and look for it in the list.",
          code: "",
          status: 0,
        };
      else if (action) {
        this.failure = null;
        this.readAgain(fresh, action);
      } else {
        this.selected = fresh;
        for (const list of [this.high, this.rest])
          list.items = list.items.map((item) =>
            item.id === fresh.id ? fresh : item,
          );
      }
    } catch (error) {
      if (!current()) return;
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
      if (dialog === this.dialogs) this.refreshing = false;
      if (this.alive) this.cdr.markForCheck();
    }
  }
  /**
   * The platform's answer to a decision is the incident's own row: when it
   * opened, its workflow and its run come from reading it again. The decision
   * is recorded, so a read that fails leaves what the answer said.
   */
  private async readDecided(decided: IncidentView) {
    const scope = this.scope;
    if (!scope) return;
    const generation = this.generation;
    try {
      const fresh = await this.host.api.request<IncidentRecord>(
        `${scope}/incidents/${encodeURIComponent(decided.id)}`,
      );
      if (
        !this.alive ||
        generation !== this.generation ||
        this.selected?.id !== decided.id ||
        isUnavailableIncident(fresh) ||
        fresh.revision < this.selected.revision
      )
        return;
      this.selected = fresh;
      this.relist(fresh);
    } catch {
      // What the answer said stays.
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  unavailable(item: IncidentRecord) {
    return isUnavailableIncident(item);
  }
  view(item: IncidentRecord) {
    return item as IncidentView;
  }
  readonly inDoubt = inDoubt;
  readonly caseLabel = caseLabel;
  readonly linkPlace = linkPlace;
  readonly missingText = missingText;
  stepOf(incident: IncidentView) {
    return stepOfIncident(incident) ?? "the run";
  }
  /** A row's step: a run held whole names one that is no step of the workflow. */
  stepText(
    incident: Pick<IncidentView, "node_id" | "instance_key" | "priority">,
  ) {
    return (
      stepOfIncident(incident) ??
      (incident.node_id ? "Not tied to a step" : "the run")
    );
  }
  /** The step of a linked incident, which is in doubt whatever its status. */
  linkStep(link: IncidentLink) {
    return this.stepText({ ...link, priority: "high" });
  }
  /** What a row in doubt says under its code: a band already says it is in doubt. */
  doubtText(banded: boolean, key: boolean) {
    const said = [
      ...(banded ? [] : ["In doubt"]),
      ...(key ? ["Business key"] : []),
    ];
    return said.join(" · ");
  }
  label(incident: Pick<IncidentView, "status">) {
    return incidentStatusLabel(incident.status);
  }
  tone(incident: Pick<IncidentView, "status">) {
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
