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
// Operate › Runs: run summaries (run_summaries.list), newest first, with the
// filters a person shares in the address and the private ones (business and
// correlation keys) kept in the page. Each refresh reads the first page again
// with the time window resolved to now; Load more continues that same chain
// with its frozen bounds. The open run keeps the shell's run detail and gets
// its steps from runs.steps, every 2 s until it finishes. The list refreshes
// every 10 s while Auto-refresh is on and the page is visible.
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
import { NgTemplateOutlet } from "@angular/common";
import { NavigationEnd, Router } from "@angular/router";
import type { App } from "../../app";
import { copyText } from "../../connection";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import { RunDetail } from "../../operations/run-detail";
import { statusLabel, toneAttribute } from "../../status-labels";
import { MAX_PAGES } from "../operate-limits";
import { environmentOf, listFailure, scopeKeyOf } from "../operate-list";
import { viewFromPath, viewPath } from "../operate-routes";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { RefreshStatus } from "../refresh-status";
import {
  asRunViewsApi,
  isUnavailableRun,
  listRunSteps,
  listRunSummaries,
  runOrigins,
  runStatuses,
  stepOutput,
  terminalRunStatuses,
  type RunStatus,
  type RunSummary,
  type RunSummaryItem,
  type RunSummaryPage,
  type RunSummaryQuery,
  type StepFact,
} from "../run-contracts";
import {
  explainRefusal,
  isTerminal,
  reasonProblem,
  retryInput,
} from "./run-actions";
import {
  MAX_RANGE_MS,
  defaultRunsFilters,
  handledText,
  isTerminalRun,
  liveDuration,
  originLabel,
  rangePresets,
  resolveRunsQuery,
  runFilterCount,
  runOutcome,
  runsFiltersFromQuery,
  runsFiltersToQuery,
  runsQuery,
  type RunsFilters,
} from "./runs-model";
import {
  instanceKey,
  type RunStepsView,
  type StepOutputState,
} from "./step-timeline";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
/**
 * Whether only the person behind the same platform, environment and sign-in
 * became known. Runs reads before Studio knows them, with the same session,
 * so nothing read belongs to anyone else; any other change of scope does.
 */
function personLearned(before: string, after: string): boolean {
  if (!before) return false;
  const [environment, person, ended] = JSON.parse(before) as unknown[];
  const [nextEnvironment, nextPerson, nextEnded] = JSON.parse(
    after,
  ) as unknown[];
  return (
    person === null &&
    nextPerson !== null &&
    environment === nextEnvironment &&
    ended === nextEnded
  );
}
/** The most runs one bulk action takes, one after another. */
const BULK_LIMIT = 50;
const STEP_PAGE = 200;

/** The steps of the run that is open, and the outputs a person asked for. */
interface StepsState {
  runId: string;
  environment: string;
  items: StepFact[];
  complete: boolean;
  nextCursor: string | null;
  loading: boolean;
  error: PlainError | null;
  outputs: Map<string, StepOutputState>;
}
/** One chain of pages: the query its first page resolved, and where it continues. */
interface Chain {
  generation: number;
  environment: string;
  query: RunSummaryQuery;
  cursor: string | null;
}
interface BulkResult {
  id: string;
  ok: boolean;
  text: string;
}

@Component({
  selector: "weave-runs-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    ModalSheet,
    NgTemplateOutlet,
    OperateState,
    RefreshStatus,
    RunDetail,
  ],
  styleUrls: ["../operate.css", "./runs.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1>Runs</h1>
        <p class="subtitle">
          Every run in this environment, newest first. Open one to follow its
          steps.
        </p>
      </div>
      @if (ready && readable && !forbidden) {
        <div class="actions">
          <weave-refresh-status
            [updatedAt]="poller.lastSuccessAt"
            [busy]="poller.busy"
            (refresh)="refreshNow()"
          />
          <label class="checkbox-field toggle-field"
            ><input
              type="checkbox"
              role="switch"
              [checked]="autoRefresh"
              (change)="setAutoRefresh(checked($event))"
            />Auto-refresh</label
          >
          @if (host.can("run.start")) {
            <button class="primary" (click)="host.startRun()">
              <weave-icon name="plus" [size]="16" />Start run
            </button>
          }
        </div>
      }
    </div>
    @if (!host.profile) {
      <div class="empty-state">
        <h2>Runs appear here once you connect</h2>
        <p>Runs happen on a platform. Connect one to start and follow runs.</p>
        <button class="primary" (click)="host.openWizard('server')">
          <weave-icon name="cloud" [size]="16" />Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded || host.platformSignedOut) {
      <div class="empty-state">
        <h2>Sign in to see runs</h2>
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
    } @else if (!readable || forbidden) {
      <weave-operate-state kind="access" capability="run.read" />
    } @else {
      <div
        class="run-filters"
        role="search"
        aria-label="Filter runs"
        [class.open]="filtersOpen"
      >
        <button
          type="button"
          class="filters-toggle"
          aria-controls="runs-filter-body"
          [attr.aria-expanded]="filtersOpen"
          (click)="filtersOpen = !filtersOpen"
        >
          <weave-icon name="search" [size]="16" />Filters ({{ filterCount() }})
        </button>
        <div class="filter-body" id="runs-filter-body">
          <div class="filter-row">
            <div class="field">
              <label for="runs-range">Time range</label>
              <select id="runs-range" (change)="setRange(value($event))">
                @for (preset of presets; track preset.id) {
                  <option
                    [value]="preset.id"
                    [selected]="rangeId() === preset.id"
                  >
                    {{ preset.label }}
                  </option>
                }
                <option value="custom" [selected]="rangeId() === 'custom'">
                  Custom range
                </option>
              </select>
            </div>
            @if (rangeId() === "custom") {
              <div class="field">
                <label for="runs-from">From</label>
                <input
                  id="runs-from"
                  type="datetime-local"
                  [value]="localTime(customFrom())"
                  [attr.aria-invalid]="rangeProblem ? 'true' : null"
                  [attr.aria-describedby]="
                    rangeProblem ? 'runs-range-problem' : null
                  "
                  (change)="setCustom('from', value($event))"
                />
              </div>
              <div class="field">
                <label for="runs-to">To</label>
                <input
                  id="runs-to"
                  type="datetime-local"
                  [value]="localTime(customTo())"
                  [attr.aria-invalid]="rangeProblem ? 'true' : null"
                  [attr.aria-describedby]="
                    rangeProblem ? 'runs-range-problem' : null
                  "
                  (change)="setCustom('to', value($event))"
                />
              </div>
            }
            <div class="field">
              <label for="runs-workflow">Workflow</label>
              <select id="runs-workflow" (change)="setWorkflow(value($event))">
                <option value="" [selected]="!filters.workflow">
                  Any workflow
                </option>
                @for (name of workflowNames(); track name) {
                  <option [value]="name" [selected]="filters.workflow === name">
                    {{ name }}
                  </option>
                }
              </select>
            </div>
            <div class="field">
              <label for="runs-version">Version</label>
              <select
                id="runs-version"
                [disabled]="!filters.workflow"
                (change)="setFilter('version', value($event) || undefined)"
              >
                <option value="" [selected]="!filters.version">
                  Any version
                </option>
                @for (version of versionsOf(filters.workflow); track version) {
                  <option
                    [value]="version"
                    [selected]="filters.version === version"
                  >
                    {{ version }}
                  </option>
                }
              </select>
            </div>
            <div class="field">
              <label for="runs-origin">Origin</label>
              <select
                id="runs-origin"
                (change)="setFilter('origin', $any(value($event)) || undefined)"
              >
                <option value="" [selected]="!filters.origin">
                  Any origin
                </option>
                @for (origin of origins; track origin) {
                  <option
                    [value]="origin"
                    [selected]="filters.origin === origin"
                  >
                    {{ originLabel(origin) }}
                  </option>
                }
              </select>
            </div>
            <div class="field key-field">
              <label for="runs-business-key">Business key</label>
              <input
                id="runs-business-key"
                autocomplete="off"
                spellcheck="false"
                [value]="filters.business_key ?? ''"
                (input)="setPrivate('business_key', value($event))"
              />
            </div>
          </div>
          @if (rangeProblem) {
            <p class="field-error" id="runs-range-problem" role="alert">
              {{ rangeProblem }}
            </p>
          }
          <div class="filter-row">
            <div class="status-filter" role="group" aria-label="Status">
              @for (status of statuses; track status) {
                <button
                  type="button"
                  class="status-choice"
                  [attr.aria-pressed]="hasStatus(status)"
                  (click)="toggleStatus(status)"
                >
                  {{ statusWord(status) }}
                </button>
              }
            </div>
            <label class="checkbox-field"
              ><input
                type="checkbox"
                [checked]="!!filters.include_test"
                (change)="setFilter('include_test', checked($event))"
              />Include test runs</label
            >
            <button
              type="button"
              class="tertiary"
              aria-controls="runs-more-filters"
              [attr.aria-expanded]="moreFilters"
              (click)="moreFilters = !moreFilters"
            >
              More filters<weave-icon
                name="chevron"
                [size]="16"
                class="disclosure-chevron"
                [class.open]="moreFilters"
              />
            </button>
            @if (filterCount()) {
              <button type="button" class="tertiary" (click)="clearFilters()">
                Clear filters ({{ filterCount() }})
              </button>
            }
          </div>
          @if (moreFilters) {
            <div class="more-filters" id="runs-more-filters">
              <div class="field key-field">
                <label for="runs-correlation-key">Correlation key</label>
                <input
                  id="runs-correlation-key"
                  autocomplete="off"
                  spellcheck="false"
                  [value]="filters.correlation_key ?? ''"
                  (input)="setPrivate('correlation_key', value($event))"
                />
              </div>
              <div class="field">
                <label for="runs-handled">Handled errors</label>
                <select id="runs-handled" (change)="setHandled(value($event))">
                  <option
                    value=""
                    [selected]="filters.has_handled_errors === undefined"
                  >
                    Any run
                  </option>
                  <option
                    value="true"
                    [selected]="filters.has_handled_errors === true"
                  >
                    With handled errors
                  </option>
                  <option
                    value="false"
                    [selected]="filters.has_handled_errors === false"
                  >
                    Without handled errors
                  </option>
                </select>
              </div>
              <div class="field">
                <label for="runs-order">Order</label>
                <select
                  id="runs-order"
                  (change)="setFilter('order', $any(value($event)))"
                >
                  <option
                    value="started_desc"
                    [selected]="
                      (filters.order ?? 'started_desc') === 'started_desc'
                    "
                  >
                    Newest first
                  </option>
                  <option
                    value="started_asc"
                    [selected]="filters.order === 'started_asc'"
                  >
                    Oldest first
                  </option>
                  <option
                    value="updated_desc"
                    [selected]="filters.order === 'updated_desc'"
                  >
                    Recently updated
                  </option>
                </select>
              </div>
              <div class="more-checks">
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="filters.has_active_incident === true"
                    (change)="
                      setFilter(
                        'has_active_incident',
                        checked($event) || undefined
                      )
                    "
                  />Has active incident</label
                >
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="!!filters.top_level_only"
                    (change)="setFilter('top_level_only', checked($event))"
                  />Top-level runs only</label
                >
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="!!filters.include_archived"
                    (change)="setFilter('include_archived', checked($event))"
                  />Include archived runs</label
                >
              </div>
            </div>
          }
        </div>
        @if (filters.caller_run_id || filters.retried_from_run_id) {
          <p class="filter-chip" role="status">
            {{
              filters.caller_run_id
                ? "Runs called by run " + short(filters.caller_run_id)
                : "Retries of run " + short(filters.retried_from_run_id)
            }}
            <button type="button" class="tertiary" (click)="clearLink()">
              Show all runs
            </button>
          </p>
        }
      </div>
      @if (host.runRefused && !forbidden) {
        <weave-operate-state
          kind="access"
          heading="You don't have access to this run"
          capability="run.read"
        />
      }
      @if (loaded && error) {
        <weave-operate-state
          kind="error"
          [message]="'Studio could not refresh the runs. ' + error.message"
          [code]="error.code"
          (retry)="refreshNow()"
        />
      }
      @if (selection.size) {
        <div class="bulk-bar" role="region" aria-label="Selected runs">
          <span class="bulk-count numeric" role="status"
            >{{ selection.size }} selected</span
          >
          @if (host.can("run.cancel")) {
            <button
              type="button"
              class="danger"
              [disabled]="bulkRunning"
              (click)="bulkCancel()"
            >
              Cancel runs
            </button>
          }
          @if (host.can("run.retry")) {
            <button
              type="button"
              [disabled]="bulkRunning"
              (click)="bulkRetry()"
            >
              Retry runs
            </button>
          }
          <button
            type="button"
            class="tertiary"
            [disabled]="bulkRunning"
            (click)="clearSelection()"
          >
            Clear selection
          </button>
        </div>
      }
      @if (bulk; as outcome) {
        <section class="bulk-results" aria-labelledby="bulk-results-title">
          <div class="bulk-results-head">
            <h2 id="bulk-results-title">{{ outcome.title }}</h2>
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
      @if (!loaded && error && !openRunId()) {
        <weave-operate-state
          kind="error"
          [message]="error.message"
          [code]="error.code"
          (retry)="refreshNow()"
        />
      } @else {
        <div class="runs-layout" [class.run-focus]="!!openRunId()">
          <div class="runs-list">
            @if (!loaded && error) {
              <weave-operate-state
                kind="error"
                [message]="error.message"
                [code]="error.code"
                (retry)="refreshNow()"
              />
            } @else if (!loaded) {
              <weave-operate-state kind="loading" noun="runs" [rows]="6" />
            } @else if (!runs.length) {
              @if (filterCount()) {
                <weave-operate-state
                  kind="empty"
                  heading="No runs match these filters."
                  text="Change or clear the filters to see more runs."
                >
                  <button type="button" (click)="clearFilters()">
                    Clear filters
                  </button>
                </weave-operate-state>
              } @else {
                <weave-operate-state
                  kind="empty"
                  heading="No runs yet"
                  text="Activate a workflow version, then start a run to follow it here."
                />
              }
            } @else {
              <div
                class="resource-table runs-table"
                role="table"
                aria-label="Runs"
              >
                <div role="rowgroup">
                  <div class="table-head runs-head" role="row">
                    <span role="columnheader" class="cell-select">
                      @if (canBulk()) {
                        <label class="select-box"
                          ><input
                            type="checkbox"
                            aria-label="Select every run on this page, up to 50"
                            [checked]="allSelected()"
                            (change)="selectAll(checked($event))"
                        /></label>
                      }</span
                    ><span role="columnheader" class="cell-status">Status</span
                    ><span role="columnheader" class="cell-workflow"
                      >Workflow</span
                    ><span role="columnheader" class="cell-origin">Origin</span
                    ><span role="columnheader" class="cell-started"
                      >Started</span
                    ><span
                      role="columnheader"
                      class="cell-duration numeric-cell"
                      >Duration</span
                    ><span
                      role="columnheader"
                      class="cell-incidents numeric-cell"
                      >Incidents</span
                    ><span role="columnheader" class="cell-key"
                      >Business key</span
                    ><span role="columnheader" class="cell-run">Run</span
                    ><span role="columnheader" class="cell-meta">Details</span>
                  </div>
                </div>
                <div role="rowgroup">
                  @for (item of runs; track item.id) {
                    @if (unavailable(item)) {
                      <div class="resource-row run-row unavailable" role="row">
                        <span role="cell" class="cell-select"></span
                        ><span role="cell" class="cell-status"></span
                        ><span role="cell" class="cell-workflow"
                          ><span class="row-text">Unavailable run</span></span
                        ><span
                          role="cell"
                          class="cell-origin cell-explain"
                          aria-colspan="5"
                          >Studio can't show this run's details.</span
                        ><span role="cell" class="cell-run"
                          ><code [attr.title]="item.id">{{
                            short(item.id)
                          }}</code
                          ><button
                            type="button"
                            class="icon-button copy-id"
                            [attr.aria-label]="'Copy run ID ' + item.id"
                            (click)="copy(item.id)"
                          >
                            <weave-icon
                              name="copy"
                              [size]="16"
                            /></button></span
                        ><span role="cell" class="cell-meta"
                          ><span>Studio can't show this run's details.</span
                          ><code [attr.title]="item.id">{{
                            short(item.id)
                          }}</code></span
                        >
                      </div>
                    } @else {
                      @let run = summaryOf(item);
                      @let outcome = outcomeOf(run);
                      <div
                        class="resource-row run-row"
                        role="row"
                        [class.selected]="openRunId() === run.id"
                      >
                        <span role="cell" class="cell-select">
                          @if (canBulk()) {
                            <label class="select-box"
                              ><input
                                type="checkbox"
                                [attr.aria-label]="
                                  'Select run ' + short(run.id)
                                "
                                [checked]="selection.has(run.id)"
                                [disabled]="
                                  !selection.has(run.id) &&
                                  selection.size >= bulkLimit
                                "
                                (change)="select(run.id, checked($event))"
                            /></label>
                          }</span
                        ><span role="cell" class="cell-status badges"
                          ><span
                            class="status-pill"
                            [attr.data-tone]="tone(outcome.tone)"
                            >{{ outcome.label }}</span
                          >
                          @if (run.paused) {
                            <span class="status-pill" data-tone="warning"
                              >Paused</span
                            >
                          }
                          @if (run.test) {
                            <span class="tag">Test</span>
                          }
                          @if (handled(run)) {
                            <span class="handled">{{ handled(run) }}</span>
                          }</span
                        ><span role="cell" class="cell-workflow"
                          ><button
                            type="button"
                            class="row-open"
                            [attr.aria-current]="
                              openRunId() === run.id ? 'true' : null
                            "
                            (click)="openRun(run)"
                          >
                            {{ run.workflow.name }}</button
                          ><span class="tag version-tag">{{
                            run.workflow.version
                          }}</span></span
                        ><span role="cell" class="cell-origin">
                          <ng-container
                            *ngTemplateOutlet="
                              origin;
                              context: { $implicit: run }
                            " /></span
                        ><span role="cell" class="cell-started"
                          ><time
                            [attr.datetime]="iso(run.started_at)"
                            [attr.title]="absolute(run.started_at)"
                            >{{ relative(run.started_at) }}</time
                          ></span
                        ><span
                          role="cell"
                          class="cell-duration numeric numeric-cell"
                          >{{ duration(run) }}</span
                        ><span role="cell" class="cell-incidents numeric-cell">
                          @if (run.active_incidents) {
                            <span
                              class="status-pill"
                              data-tone="danger"
                              [attr.aria-label]="
                                incidentsLabel(run.active_incidents)
                              "
                              ><span class="numeric">{{
                                run.active_incidents
                              }}</span></span
                            >
                          } @else {
                            <span class="muted" aria-label="No active incidents"
                              >—</span
                            >
                          }</span
                        ><span role="cell" class="cell-key">{{
                          run.business_key ?? "—"
                        }}</span
                        ><span role="cell" class="cell-run"
                          ><code [attr.title]="run.id">{{ short(run.id) }}</code
                          ><button
                            type="button"
                            class="icon-button copy-id"
                            [attr.aria-label]="'Copy run ID ' + run.id"
                            (click)="copy(run.id)"
                          >
                            <weave-icon
                              name="copy"
                              [size]="16"
                            /></button></span
                        ><span role="cell" class="cell-meta"
                          ><ng-container
                            *ngTemplateOutlet="
                              origin;
                              context: { $implicit: run }
                            "
                          /><span
                            ><time
                              [attr.datetime]="iso(run.started_at)"
                              [attr.title]="absolute(run.started_at)"
                              >{{ relative(run.started_at) }}</time
                            ></span
                          ><span class="numeric">{{ duration(run) }}</span>
                          @if (run.active_incidents) {
                            <span>{{
                              incidentsLabel(run.active_incidents)
                            }}</span>
                          }
                          @if (run.business_key) {
                            <span class="meta-key">{{ run.business_key }}</span>
                          }
                          <code [attr.title]="run.id">{{
                            short(run.id)
                          }}</code></span
                        >
                      </div>
                    }
                  }
                </div>
              </div>
              @if (canLoadMore) {
                <button
                  type="button"
                  class="load-more"
                  [attr.aria-disabled]="appending ? 'true' : null"
                  (click)="loadMore()"
                >
                  Load more runs
                </button>
              } @else if (atLimit) {
                <p class="list-limit" role="status">
                  Showing the first {{ maxRuns }} runs. Narrow the filters to
                  see others.
                </p>
              }
            }
          </div>
          @if (openRunId()) {
            <section
              class="record-detail run-detail-panel"
              role="complementary"
              aria-labelledby="record-detail-title"
              [weaveModalSheet]="true"
              [sheetWhen]="sheetWhen.detail"
              sheetInitialFocus="#record-detail-title"
              (sheetDismiss)="host.closeRecord()"
            >
              <weave-run-detail [host]="host" [timeline]="stepsView()" />
            </section>
          }
        </div>
      }
    }
    <ng-template #origin let-run>
      @if (run.retried_from_run_id) {
        <button
          type="button"
          class="text-link origin-link"
          [attr.aria-label]="
            'Open run ' +
            short(run.retried_from_run_id) +
            ', the run this one retried'
          "
          (click)="openLinked(run.retried_from_run_id)"
        >
          Retry of {{ short(run.retried_from_run_id) }}
        </button>
      } @else if (run.caller) {
        <button
          type="button"
          class="text-link origin-link"
          [attr.aria-label]="
            'Open run ' +
            short(run.caller.run_id) +
            ', the run that called this one'
          "
          (click)="openLinked(run.caller.run_id)"
        >
          Called by {{ short(run.caller.run_id) }}
        </button>
      } @else {
        <span>{{ originLabel(run.origin) }}</span>
      }
    </ng-template>
  `,
})
export class RunsPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  private navigated = this.router.events.subscribe((event) => {
    if (event instanceof NavigationEnd) this.addressChanged(true);
  });
  readonly sheetWhen = sheetWhen;
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly originLabel = originLabel;
  readonly handled = handledText;
  readonly presets = rangePresets;
  readonly origins = runOrigins;
  readonly statuses = runStatuses;
  readonly bulkLimit = BULK_LIMIT;
  readonly maxRuns = MAX_PAGES * 50;
  /** The list refreshes by itself every 10 s while this is on. */
  autoRefresh = true;
  readonly poller = new Poller(
    () => this.refreshFirstPage(),
    10_000,
    undefined,
    // A refresh reads the first page again: it waits while a person works
    // with more pages, a selection or a bulk action.
    () =>
      !this.autoRefresh ||
      this.pages > 1 ||
      this.selection.size > 0 ||
      this.bulkRunning,
  );
  /** The open run's detail and steps, every 2 s until the run finishes. */
  readonly detailPoller = new Poller(() => this.pollDetail(), 2_000);
  filters: RunsFilters = defaultRunsFilters();
  runs: RunSummaryItem[] = [];
  chain: Chain | null = null;
  loaded = false;
  forbidden = false;
  error: PlainError | null = null;
  appending = false;
  moreFilters = false;
  /** Narrow screens fold the filters behind "Filters (n)". */
  filtersOpen = false;
  rangeProblem = "";
  selection = new Set<string>();
  bulk: { title: string; results: BulkResult[] } | null = null;
  bulkRunning = false;
  steps: StepsState | null = null;
  private pages = 1;
  private generation = 0;
  private scopeKey = "";
  private privateTimer: ReturnType<typeof setTimeout> | null = null;
  /** The filters changed with a run open: the address follows once the run has closed. */
  private syncPending = false;
  private stepsView$: {
    state: StepsState;
    now: number;
    view: RunStepsView;
  } | null = null;
  private detailStatus = "";
  /** The first-page read in flight, which Load more waits for. */
  private refreshing: Promise<boolean> | null = null;
  // Durations of running runs count up between refreshes.
  private ticker = setInterval(() => this.cdr.markForCheck(), 1000);

  /**
   * Whether to read runs: with the person known, only with run.read; before
   * Studio knows them, the platform decides, as it does for every read.
   */
  get readable() {
    return !this.host.identity || this.host.canAnywhere("run.read");
  }
  get ready() {
    return (
      !!this.host.profile &&
      !this.host.signInEnded &&
      !this.host.platformSignedOut
    );
  }
  /** An ended sign-in is the shell's to explain, with its own Sign in again. */
  private signInGone(error: unknown) {
    const plain = describeError(error);
    if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
      this.host.fail(error);
  }
  /** More runs follow, and Load more may read them. */
  get canLoadMore() {
    return !!this.chain?.cursor && this.pages < MAX_PAGES;
  }
  /** More runs follow beyond the most a list reads. */
  get atLimit() {
    return !!this.chain?.cursor && this.pages >= MAX_PAGES;
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.navigated.unsubscribe();
    clearInterval(this.ticker);
    if (this.privateTimer) clearTimeout(this.privateTimer);
    this.poller.stop();
    this.detailPoller.stop();
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    if (key !== this.scopeKey && personLearned(this.scopeKey, key)) {
      // The same session's person became known: the rows, the open run and
      // focus stay while the first page is read again as that person.
      this.scopeKey = key;
      this.generation++;
      this.appending = false;
      if (environmentOf(this.host) && this.readable) void this.poller.refresh();
    } else if (key !== this.scopeKey) {
      // Another platform, environment or person: nothing read before, no key
      // typed before and no cursor from before stays.
      this.scopeKey = key;
      this.generation++;
      this.runs = [];
      this.chain = null;
      this.loaded = false;
      this.forbidden = false;
      this.error = null;
      this.pages = 1;
      this.appending = false;
      this.selection = new Set();
      this.bulk = null;
      this.steps = null;
      this.detailStatus = "";
      this.detailPoller.stop();
      this.filters = runsFiltersFromQuery(location.search);
      if (this.privateTimer) clearTimeout(this.privateTimer);
      this.privateTimer = null;
      if (environmentOf(this.host) && this.readable) void this.poller.refresh();
    }
    this.followDetail();
  }

  // ---------------------------------------------------------------- list

  /**
   * Reads the first page again with the window resolved to now: a new chain
   * whose cursor replaces the old one. Answers for an older chain or another
   * scope change nothing.
   */
  async refreshFirstPage(): Promise<boolean> {
    const read = this.readFirstPage();
    this.refreshing = read;
    try {
      return await read;
    } finally {
      if (this.refreshing === read) this.refreshing = null;
    }
  }
  private async readFirstPage(): Promise<boolean> {
    const environment = environmentOf(this.host);
    if (!environment || !this.readable) return false;
    const generation = ++this.generation;
    const key = scopeKeyOf(this.host);
    const query = resolveRunsQuery(this.filters, new Date());
    try {
      const page = await listRunSummaries(asRunViewsApi(this.host.api), query);
      if (generation !== this.generation || key !== scopeKeyOf(this.host))
        return false;
      this.runs = page.items;
      this.chain = { generation, environment, query, cursor: page.next_cursor };
      this.pages = 1;
      this.loaded = true;
      this.forbidden = false;
      this.error = null;
      const listed = new Set(page.items.map((item) => item.id));
      this.selection = new Set(
        [...this.selection].filter((id) => listed.has(id)),
      );
      return true;
    } catch (error) {
      if (generation !== this.generation || key !== scopeKeyOf(this.host))
        return false;
      // The old chain cannot continue: its rows stay, its cursor does not.
      if (this.chain) this.chain = { ...this.chain, cursor: null };
      ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
      this.signInGone(error);
      throw error;
    } finally {
      this.cdr.markForCheck();
    }
  }
  /**
   * Continues the current chain with its own query; never resolves a new
   * window. Asked while a refresh is out, it continues the chain that refresh
   * starts.
   */
  async loadMore() {
    if (this.appending) return;
    this.appending = true;
    const key = scopeKeyOf(this.host);
    try {
      await this.refreshing?.catch(() => undefined);
      const chain = this.chain;
      if (
        !chain?.cursor ||
        chain.generation !== this.generation ||
        this.pages >= MAX_PAGES ||
        key !== scopeKeyOf(this.host)
      )
        return;
      const page = await this.host.api.request<RunSummaryPage>(
        `${chain.environment}/run-summaries${runsQuery(chain.query, chain.cursor)}`,
      );
      if (
        this.chain !== chain ||
        chain.generation !== this.generation ||
        key !== scopeKeyOf(this.host)
      )
        return;
      const seen = new Set(this.runs.map((item) => item.id));
      this.runs = [
        ...this.runs,
        ...page.items.filter((item) => !seen.has(item.id)),
      ];
      this.chain = { ...chain, cursor: page.next_cursor };
      this.pages++;
      this.error = null;
    } catch (error) {
      if (key === scopeKeyOf(this.host))
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
    } finally {
      this.appending = false;
      this.cdr.markForCheck();
    }
  }
  /** Refresh: the first page again, from now; selection and extra pages go. */
  refreshNow() {
    this.selection = new Set();
    void this.poller.refresh();
  }
  setAutoRefresh(on: boolean) {
    this.autoRefresh = on;
    if (on) void this.poller.refresh();
  }

  // ---------------------------------------------------------------- filters

  setFilter<K extends keyof RunsFilters>(key: K, value: RunsFilters[K]) {
    const next = { ...this.filters, [key]: value };
    if (value === undefined) delete next[key];
    this.apply(next);
  }
  setWorkflow(name: string) {
    const next = { ...this.filters };
    delete next.version;
    if (name) next.workflow = name;
    else delete next.workflow;
    this.apply(next);
  }
  setHandled(value: string) {
    this.setFilter(
      "has_handled_errors",
      value === "true" ? true : value === "false" ? false : undefined,
    );
  }
  hasStatus(status: RunStatus) {
    return (this.filters.status ?? []).includes(status);
  }
  toggleStatus(status: RunStatus) {
    const current = this.filters.status ?? [];
    this.setFilter(
      "status",
      current.includes(status)
        ? current.filter((item) => item !== status)
        : runStatuses.filter(
            (item) => item === status || current.includes(item),
          ),
    );
  }
  statusWord(status: RunStatus) {
    return statusLabel("run", status);
  }
  rangeId() {
    const window = this.filters.timeWindow;
    if (window.kind === "absolute") return "custom";
    return (
      rangePresets.find((preset) => preset.milliseconds === window.milliseconds)
        ?.id ?? "custom"
    );
  }
  setRange(id: string) {
    this.rangeProblem = "";
    const preset = rangePresets.find((item) => item.id === id);
    if (preset) {
      this.setFilter("timeWindow", {
        kind: "relative",
        milliseconds: preset.milliseconds,
      });
      return;
    }
    // A custom range starts as the window it replaces, with fixed bounds.
    const resolved = resolveRunsQuery(this.filters, new Date());
    this.setFilter("timeWindow", {
      kind: "absolute",
      started_after: resolved.started_after,
      started_before: resolved.started_before,
    });
  }
  customFrom() {
    const window = this.filters.timeWindow;
    return window.kind === "absolute" ? (window.started_after ?? "") : "";
  }
  customTo() {
    const window = this.filters.timeWindow;
    return window.kind === "absolute" ? (window.started_before ?? "") : "";
  }
  /** "2026-10-07T12:30" in this computer's time zone, for a datetime-local field. */
  localTime(value: string) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return "";
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }
  setCustom(edge: "from" | "to", value: string) {
    const date = value ? new Date(value) : null;
    const instant =
      date && Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
    const window =
      this.filters.timeWindow.kind === "absolute"
        ? this.filters.timeWindow
        : { kind: "absolute" as const };
    const next = {
      kind: "absolute" as const,
      started_after: edge === "from" ? instant : window.started_after,
      started_before: edge === "to" ? instant : window.started_before,
    };
    const from = next.started_after ? Date.parse(next.started_after) : null;
    const to = next.started_before ? Date.parse(next.started_before) : null;
    if (from !== null && to !== null && from >= to) {
      this.rangeProblem = "Choose a start before the end.";
      return;
    }
    if (from !== null && to !== null && to - from > MAX_RANGE_MS) {
      this.rangeProblem = "Choose a range of 400 days or less.";
      return;
    }
    this.rangeProblem = "";
    this.setFilter("timeWindow", next);
  }
  /** Business and correlation keys: sent after a pause in typing, never put in the address. */
  setPrivate(key: "business_key" | "correlation_key", value: string) {
    const next = { ...this.filters };
    if (value.trim()) next[key] = value;
    else delete next[key];
    this.filters = next;
    if (this.privateTimer) clearTimeout(this.privateTimer);
    this.privateTimer = setTimeout(() => {
      this.privateTimer = null;
      this.changed();
    }, 400);
  }
  clearFilters() {
    this.rangeProblem = "";
    this.apply(defaultRunsFilters());
  }
  clearLink() {
    const next = { ...this.filters };
    delete next.caller_run_id;
    delete next.retried_from_run_id;
    this.apply(next);
  }
  filterCount() {
    return runFilterCount(this.filters);
  }
  private apply(next: RunsFilters) {
    this.filters = next;
    void this.syncAddress();
    this.changed();
  }
  /** New filters: a new first page, no selection, and the open run closes. */
  private changed() {
    this.selection = new Set();
    this.bulk = null;
    void this.poller.refresh();
  }
  /** Workflow names this person has seen: published versions and listed runs. */
  workflowNames() {
    const names = new Set<string>();
    for (const version of this.host.workflowVersions.values())
      if (version.name) names.add(version.name);
    for (const item of this.runs)
      if (!isUnavailableRun(item)) names.add(item.workflow.name);
    if (this.filters.workflow) names.add(this.filters.workflow);
    return [...names].sort();
  }
  versionsOf(name: string | undefined) {
    if (!name) return [];
    const versions = new Set<string>();
    for (const version of this.host.workflowVersions.values())
      if (version.name === name && version.version)
        versions.add(version.version);
    for (const item of this.runs)
      if (!isUnavailableRun(item) && item.workflow.name === name)
        versions.add(item.workflow.version);
    if (this.filters.version) versions.add(this.filters.version);
    return [...versions].sort();
  }

  // ---------------------------------------------------------------- address

  @HostListener("window:popstate") locationChanged() {
    this.addressChanged();
  }
  /** The address moved (Back, Forward, a link): the page follows its filters. */
  private addressChanged(routed = false) {
    const route = viewFromPath(location.pathname);
    if (route?.view !== "runs") return;
    // A run's own address carries no list filters: the list keeps its own.
    if (route.id && !this.syncPending) return;
    if (this.syncPending) {
      // The open run is closing for new filters: once the router has landed
      // on the list, the address takes those filters.
      if (routed && !route.id) {
        this.syncPending = false;
        void this.syncAddress();
      }
      return;
    }
    const shared = runsFiltersFromQuery(location.search);
    if (runsFiltersToQuery(shared) === runsFiltersToQuery(this.filters)) return;
    this.filters = {
      ...shared,
      ...(this.filters.business_key
        ? { business_key: this.filters.business_key }
        : {}),
      ...(this.filters.correlation_key
        ? { correlation_key: this.filters.correlation_key }
        : {}),
    };
    this.changed();
  }
  /** Puts the shared filters in the address; a run open first steps back to the list. */
  private async syncAddress() {
    if (viewFromPath(location.pathname)?.id) {
      this.syncPending = true;
      this.host.closeRecord();
      return;
    }
    const path = viewPath("runs") + runsFiltersToQuery(this.filters);
    if (location.pathname + location.search !== path)
      await this.router.navigateByUrl(path, { replaceUrl: true });
  }

  // ---------------------------------------------------------------- run detail

  /** The run whose detail is open, or "". */
  openRunId() {
    const record = this.host.view === "runs" ? this.host.selectedRecord : null;
    return record && typeof record["id"] === "string" ? record["id"] : "";
  }
  openRun(run: RunSummary) {
    void this.host.open({
      id: run.id,
      workflow: run.workflow,
      workflow_name: run.workflow.name,
      workflow_version: run.workflow.version,
      business_key: run.business_key,
      correlation_key: run.correlation_key,
      state: { status: run.status },
    });
  }
  /** A linked run (the one retried, or the caller) opens in the same detail. */
  openLinked(id: string) {
    void this.host.viewRun({ id });
  }
  /** Starts or stops following the open run as it changes. */
  private followDetail() {
    const id = this.openRunId();
    const environment = environmentOf(this.host);
    if (!id || !environment || !this.readable) {
      if (this.steps) {
        this.steps = null;
        this.detailStatus = "";
        this.detailPoller.stop();
      }
      return;
    }
    if (this.steps?.runId === id && this.steps.environment === environment) {
      const status = this.runStatus();
      if (status && status !== this.detailStatus) {
        this.detailStatus = status;
        if (terminalRunStatuses.includes(status as RunStatus))
          this.detailPoller.stop();
      }
      return;
    }
    this.steps = {
      runId: id,
      environment,
      items: [],
      complete: true,
      nextCursor: null,
      loading: true,
      error: null,
      outputs: new Map(),
    };
    this.detailStatus = this.runStatus();
    this.detailPoller.stop();
    // The shell reads the run as it opens; its steps are read here at once,
    // and both again every 2 s while it is still going.
    void this.readSteps(this.steps).catch(() => undefined);
    if (!terminalRunStatuses.includes(this.detailStatus as RunStatus))
      this.detailPoller.start(false);
  }
  /** The open run's status as its detail last read it. */
  private runStatus(): string {
    const record = this.host.selectedRecord ?? {};
    const state = isRecord(record["state"]) ? record["state"] : {};
    return typeof state["status"] === "string" ? state["status"] : "";
  }
  /**
   * Every 2 s while the run is going: its steps, and the run itself. When the
   * run changed, the shell reads its whole detail again, as Refresh does.
   */
  private async pollDetail(): Promise<boolean> {
    const state = this.steps;
    if (!state) return false;
    const [view] = await Promise.all([
      this.host.api.request<Json>(
        `${state.environment}/runs/${encodeURIComponent(state.runId)}`,
      ),
      this.readSteps(state),
    ]);
    if (state !== this.steps || this.openRunId() !== state.runId) return false;
    if (JSON.stringify(view) !== JSON.stringify(this.host.selectedRecord))
      await this.host.readDetail();
    if (isTerminal(view)) this.detailPoller.stop();
    this.cdr.markForCheck();
    return true;
  }
  /** Reads the first page of the open run's steps. */
  private async readSteps(state: StepsState) {
    try {
      const page = await listRunSteps(
        {
          environment: state.environment,
          request: (path) => this.host.api.request(path),
        },
        state.runId,
        { limit: STEP_PAGE },
      );
      if (state !== this.steps) return;
      state.items = page.items;
      state.complete = page.complete;
      state.nextCursor = page.next_cursor;
      state.error = null;
    } catch (error) {
      if (state !== this.steps) return;
      state.error = describeError(error);
      throw error;
    } finally {
      if (state === this.steps) {
        state.loading = false;
        this.stepsView$ = null;
        this.cdr.markForCheck();
      }
    }
  }
  private async moreSteps(state: StepsState) {
    if (!state.nextCursor || state.loading) return;
    state.loading = true;
    this.stepsView$ = null;
    try {
      const page = await listRunSteps(
        {
          environment: state.environment,
          request: (path) => this.host.api.request(path),
        },
        state.runId,
        { limit: STEP_PAGE, cursor: state.nextCursor },
      );
      if (state !== this.steps) return;
      state.items = [...state.items, ...page.items];
      state.nextCursor = page.next_cursor;
      state.complete = state.complete && page.complete;
    } catch (error) {
      if (state === this.steps) state.error = describeError(error);
    } finally {
      if (state === this.steps) {
        state.loading = false;
        this.stepsView$ = null;
        this.cdr.markForCheck();
      }
    }
  }
  /** One step's output, only when a person asks for it. */
  private async readOutput(state: StepsState, item: StepFact) {
    const key = instanceKey(item);
    state.outputs.set(key, { kind: "loading" });
    this.stepsView$ = null;
    try {
      let cursor: string | undefined;
      for (let page = 0; page < MAX_PAGES; page++) {
        const answer = await listRunSteps(
          {
            environment: state.environment,
            request: (path) => this.host.api.request(path),
          },
          state.runId,
          {
            step: item.node_id,
            include: "output",
            limit: 500,
            ...(cursor ? { cursor } : {}),
          },
        );
        if (state !== this.steps) return;
        const found = answer.items.find(
          (fact) => fact.instance_key === item.instance_key,
        );
        if (found || !answer.next_cursor) {
          state.outputs.set(key, {
            kind: "ready",
            output: found ? stepOutput(found) : { kind: "none" },
          });
          return;
        }
        cursor = answer.next_cursor;
      }
      state.outputs.set(key, { kind: "ready", output: { kind: "none" } });
    } catch (error) {
      if (state === this.steps)
        state.outputs.set(key, { kind: "error", error: describeError(error) });
    } finally {
      if (state === this.steps) {
        this.stepsView$ = null;
        this.cdr.markForCheck();
      }
    }
  }
  /** What the run detail shows in its Steps section; the same object until something changes. */
  stepsView(): RunStepsView | null {
    const state = this.steps;
    if (!state) return null;
    const second = Math.floor(Date.now() / 1000);
    if (this.stepsView$?.state === state && this.stepsView$.now === second)
      return this.stepsView$.view;
    const view: RunStepsView = {
      runId: state.runId,
      items: state.items,
      complete: state.complete,
      nextCursor: state.nextCursor,
      loading: state.loading,
      error: state.error,
      now: new Date(),
      outputs: new Map(state.outputs),
      loadMore: () => void this.moreSteps(state),
      retry: () => {
        state.loading = true;
        state.error = null;
        this.stepsView$ = null;
        void this.readSteps(state).catch(() => undefined);
      },
      readOutput: (item) => void this.readOutput(state, item),
      openRun: (id) => this.openLinked(id),
    };
    this.stepsView$ = { state, now: second, view };
    return view;
  }

  // ---------------------------------------------------------------- selection and bulk actions

  canBulk() {
    return this.host.can("run.cancel") || this.host.can("run.retry");
  }
  select(id: string, on: boolean) {
    const next = new Set(this.selection);
    if (on && next.size < BULK_LIMIT) next.add(id);
    else next.delete(id);
    this.selection = next;
  }
  selectable() {
    return this.runs
      .filter((item) => !isUnavailableRun(item))
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
  clearSelection() {
    this.selection = new Set();
  }
  /** Cancel runs: one reason for all, sent one run at a time, each with its own answer. */
  async bulkCancel() {
    const ids = [...this.selection];
    if (!ids.length || this.bulkRunning || !this.host.can("run.cancel")) return;
    const values = await this.host.dialogs.form({
      title:
        ids.length === 1 ? "Cancel this run?" : `Cancel ${ids.length} runs?`,
      message:
        "Steps that already started may still finish outside Weave. Give a reason for the audit log.",
      confirmLabel: ids.length === 1 ? "Cancel run" : "Cancel runs",
      cancelLabel: "Keep running",
      danger: true,
      fields: [
        {
          name: "reason",
          label: "Reason",
          required: true,
          multiline: true,
          validate: reasonProblem,
        },
      ],
    });
    if (!values) return;
    await this.runBulk(
      "Cancel runs",
      ids,
      async (run, environment) => {
        if (isTerminalRun(run))
          return { ok: false, text: "Not canceled: it has already finished." };
        await this.host.safeMutation(
          `${environment}/runs/${encodeURIComponent(run.id)}/cancel`,
          "POST",
          { reason: values["reason"] },
          undefined,
          "Cancel run",
        );
        return { ok: true, text: "Canceled." };
      },
      "run.cancel",
    );
  }
  /** Retry runs on the same version, with each run's own input. */
  async bulkRetry() {
    const ids = [...this.selection];
    if (!ids.length || this.bulkRunning || !this.host.can("run.retry")) return;
    const confirmed = await this.host.dialogs.confirm({
      title: ids.length === 1 ? "Retry this run?" : `Retry ${ids.length} runs?`,
      message:
        "Each run starts again on the same version, with the input it started with.",
      confirmLabel: ids.length === 1 ? "Retry run" : "Retry runs",
      cancelLabel: "Keep as is",
    });
    if (!confirmed) return;
    await this.runBulk(
      "Retry runs",
      ids,
      async (run, environment) => {
        if (!isTerminalRun(run))
          return { ok: false, text: "Not retried: it has not finished." };
        const base = `${environment}/runs/${encodeURIComponent(run.id)}`;
        const view = await this.host.api.request<Json>(base);
        const activation = isRecord(view["activation"])
          ? view["activation"]
          : {};
        if (typeof activation["id"] !== "string")
          return { ok: false, text: "Not retried: its version can't be read." };
        const created = await this.host.safeMutation<Json>(
          `${base}/retry`,
          "POST",
          {
            activation_id: activation["id"],
            input: retryInput(view),
            ...(run.business_key ? { business_key: run.business_key } : {}),
            ...(run.correlation_key
              ? { correlation_key: run.correlation_key }
              : {}),
          },
          undefined,
          "Retry run",
        );
        return { ok: true, text: `Retried as run ${shortId(created["id"])}.` };
      },
      "run.retry",
    );
  }
  private async runBulk(
    title: string,
    ids: string[],
    act: (
      run: RunSummary,
      environment: string,
    ) => Promise<{ ok: boolean; text: string }>,
    capability: string,
  ) {
    const environment = environmentOf(this.host);
    const key = scopeKeyOf(this.host);
    const runs = new Map(
      this.runs
        .filter((item) => !isUnavailableRun(item))
        .map((item) => [item.id, item as RunSummary]),
    );
    const results: BulkResult[] = ids.map((id) => ({
      id,
      ok: false,
      text: "Waiting…",
    }));
    // The results on screen; a newer bulk action replaces them.
    let shown = { title, results: [...results] };
    this.bulk = shown;
    this.bulkRunning = true;
    this.cdr.markForCheck();
    let stopped = "";
    try {
      for (const [index, id] of ids.slice(0, BULK_LIMIT).entries()) {
        if (key !== scopeKeyOf(this.host))
          stopped = "Not sent: the workspace changed.";
        const run = runs.get(id);
        let result: { ok: boolean; text: string };
        if (stopped) result = { ok: false, text: stopped };
        else if (!run)
          result = { ok: false, text: "Not sent: it is no longer listed." };
        else
          try {
            result = await act(run, environment);
          } catch (error) {
            if (this.host.unknownCommand) {
              stopped =
                "Not sent: Studio didn't get an answer for an earlier run. Use Check now first.";
              result = {
                ok: false,
                text: "No answer: it may have worked. Use Check now.",
              };
            } else
              result = {
                ok: false,
                text: explainRefusal(error, capability).message,
              };
          }
        results[index] = { id, ...result };
        if (this.bulk === shown) {
          shown = { title, results: [...results] };
          this.bulk = shown;
        }
        this.cdr.markForCheck();
      }
    } finally {
      this.bulkRunning = false;
      this.selection = new Set();
      this.cdr.markForCheck();
    }
    if (key === scopeKeyOf(this.host)) void this.poller.refresh();
  }

  // ---------------------------------------------------------------- cells

  unavailable(item: RunSummaryItem) {
    return isUnavailableRun(item);
  }
  summaryOf(item: RunSummaryItem) {
    return item as RunSummary;
  }
  outcomeOf(run: RunSummary) {
    return runOutcome(run);
  }
  tone(tone: Parameters<typeof toneAttribute>[0]) {
    return toneAttribute(tone);
  }
  relative(value: string) {
    return timeRelative(value);
  }
  duration(run: RunSummary) {
    return liveDuration(run, new Date());
  }
  incidentsLabel(count: number) {
    return `${count} active ${count === 1 ? "incident" : "incidents"}`;
  }
  async copy(id: string) {
    if (await copyText(id)) this.host.notify("Copied the run ID.");
    else
      this.host.notify("Studio couldn't copy. Select the run ID and copy it.");
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
}
