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
// Operate › Overview: platform health, load, workers and clusters, and what
// needs attention, from one environment summary every 15 s while the page is
// visible. Each section shows its own access or loading problem; the rest of
// the page stays usable. Ages tick on the server's clock between loads, and
// health is only ever what the last answer said.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  Input,
  OnDestroy,
  OnInit,
  inject,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Router } from "@angular/router";
import type { App, View } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { relativeTime } from "../../format";
import { AnchoredPopover } from "../../forms/ui/anchored-popover";
import { Icon } from "../../icon";
import { statusLabel, statusTone, toneAttribute } from "../../status-labels";
import { environmentOf, listFailure, scopeKeyOf } from "../operate-list";
import { viewFromPath } from "../operate-routes";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import { timeAbsolute, timeIso } from "../operate-time";
import { RefreshStatus } from "../refresh-status";
import type { StatisticsSeries } from "../metrics/chart-model";
import { LineChart, Sparkline, TimeChart } from "../metrics/charts";
import {
  attentionLabels,
  attentionLink,
  clockOffset,
  countText,
  dayHeadline,
  dayQuery,
  healthTiles,
  isQuiet,
  keepsStatistics,
  loadFigures,
  loadSeriesQuery,
  loadSparklines,
  readSummary,
  sectionOf,
  slotMeter,
  statisticsMissing,
  type AttentionItem,
  type HealthState,
  type HealthTile,
  type OperationsSummary,
  type RunsDaySection,
  type TileId,
} from "./overview-model";

// The first statistics read waits this long after the summary, so the page's opening reads (Home's too,
// when it was just open) have finished: a poll's byte budget counts each refresh on its own.
const STATISTICS_DELAY_MS = 5_000;

@Component({
  selector: "weave-overview-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    AnchoredPopover,
    Icon,
    LineChart,
    NgTemplateOutlet,
    OperateState,
    RefreshStatus,
    Sparkline,
    TimeChart,
  ],
  styleUrls: ["../operate.css", "./overview.css"],
  template: `
    <div class="operate-heading">
      <div>
        <h1>Overview</h1>
        <p class="subtitle">
          Platform health and the work in this environment.
        </p>
      </div>
      @if (readable && summary) {
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
        <h2>Platform health appears here once you connect</h2>
        <p>Overview reads the health and load of a platform environment.</p>
        <button class="primary" (click)="host.openWizard('server')">
          <weave-icon name="cloud" [size]="16" />Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded || host.platformSignedOut) {
      <div class="empty-state">
        <h2>Sign in to see platform health</h2>
        <p>
          {{
            host.signInEnded
              ? "Studio shows it as soon as you're signed in again."
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
      <ng-container *ngTemplateOutlet="skeleton" />
    } @else if (!readable || forbidden) {
      <weave-operate-state kind="access" capability="status.read" />
    } @else if (!summary && error) {
      <weave-operate-state
        kind="error"
        [message]="error.message"
        [code]="error.code"
        (retry)="poller.refresh()"
      />
    } @else if (!summary) {
      <ng-container *ngTemplateOutlet="skeleton" />
    } @else {
      @let s = summary;
      @if (error) {
        <weave-operate-state
          kind="error"
          message="Studio could not refresh the overview. The figures below are from its last answer."
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      }
      <!-- Present only with live figures: real-platform tests time the first meaningful paint by it. -->
      <div class="overview" data-testid="overview-meaningful">
        <section class="band" aria-labelledby="overview-health">
          <h2 id="overview-health" class="band-title">Health</h2>
          <ul class="health-strip" role="list">
            @for (tile of tiles(); track tile.id) {
              <li
                class="health-tile"
                [attr.data-tile]="tile.id"
                (keydown.escape)="open === tile.id && close(anchor)"
                (focusout)="leave($event, tile.id)"
              >
                <div class="tile-head">
                  <span class="tile-label">{{ tile.label }}</span
                  ><button
                    #anchor
                    type="button"
                    class="icon-button details-toggle"
                    title="Details"
                    aria-haspopup="dialog"
                    [attr.aria-expanded]="open === tile.id"
                    [attr.aria-controls]="
                      open === tile.id ? 'overview-health-details' : null
                    "
                    [attr.aria-label]="tile.label + ' details'"
                    (click)="toggle(tile.id)"
                  >
                    <weave-icon name="info" [size]="16" />
                  </button>
                </div>
                <span
                  class="status-pill tile-state"
                  [attr.data-tone]="tone(tile.state)"
                  >{{ stateLabel(tile.state) }}</span
                >
                <span class="tile-figure numeric">{{ tile.figure }}</span>
                @if (open === tile.id) {
                  <div
                    id="overview-health-details"
                    class="health-details"
                    role="dialog"
                    [attr.aria-label]="tile.label + ' details'"
                    [weaveAnchoredPopover]="true"
                    [popoverAnchor]="anchor"
                    (popoverClosed)="open = null"
                  >
                    <div class="details-head">
                      <h3>{{ tile.label }}</h3>
                      <button
                        type="button"
                        class="icon-button"
                        [attr.aria-label]="'Close ' + tile.label + ' details'"
                        (click)="close(anchor)"
                      >
                        <weave-icon name="close" [size]="16" />
                      </button>
                    </div>
                    @if (tile.facts.length) {
                      <dl class="detail-facts">
                        @for (fact of tile.facts; track fact[0]) {
                          <div class="detail-fact">
                            <dt>{{ fact[0] }}</dt>
                            <dd>{{ fact[1] }}</dd>
                          </div>
                        }
                      </dl>
                    }
                    @if (tile.rows.length) {
                      <div
                        class="loop-table"
                        role="table"
                        [attr.aria-label]="tile.label + ' loops'"
                      >
                        <div class="loop-row loop-head" role="row">
                          <span role="columnheader">Loop</span
                          ><span role="columnheader">State</span
                          ><span role="columnheader">Last success</span
                          ><span role="columnheader" class="numeric-cell"
                            >Failures</span
                          >
                        </div>
                        @for (row of tile.rows; track row.label) {
                          <div class="loop-row" role="row">
                            <span role="cell" class="loop-name">{{
                              row.label
                            }}</span
                            ><span role="cell"
                              ><span
                                class="status-pill"
                                [attr.data-tone]="tone(row.state)"
                                >{{ stateLabel(row.state) }}</span
                              ></span
                            ><span role="cell">
                              @if (row.lastSuccessAt) {
                                <time
                                  [attr.datetime]="iso(row.lastSuccessAt)"
                                  [attr.title]="absolute(row.lastSuccessAt)"
                                  >{{ ago(row.lastSuccessAt) }}</time
                                >
                              } @else {
                                {{ row.lastSuccess || "—" }}
                              }</span
                            ><span role="cell" class="numeric numeric-cell">{{
                              row.failures
                            }}</span>
                          </div>
                        }
                      </div>
                      <p class="details-note">
                        Failures count failed cycles in a row.
                      </p>
                    }
                  </div>
                }
              </li>
            }
          </ul>
        </section>

        <section class="band" aria-labelledby="overview-load">
          <h2 id="overview-load" class="band-title">Load</h2>
          @let load = sectionOf(s.load);
          @if (load.kind === "ready") {
            @if (quiet()) {
              <div class="band-empty">
                <p>
                  No activity yet. Run a workflow to see throughput, failures
                  and queues here.
                </p>
                <button type="button" (click)="go('workflows')">
                  Open workflows
                </button>
              </div>
            } @else {
              <ul
                class="figures"
                role="list"
                [class.with-lines]="loadLines.size > 0"
              >
                @for (figure of figures(); track figure.id) {
                  <li class="figure" [attr.data-figure]="figure.id">
                    <span class="figure-label">{{ figure.label }}</span
                    ><span class="figure-value numeric">{{
                      count(figure.value)
                    }}</span
                    ><span class="figure-detail">{{ figure.detail }}</span>
                    @if (loadLines.size) {
                      <weave-sparkline
                        class="figure-line"
                        [points]="loadLines.get(figure.id) ?? []"
                        [name]="figure.label"
                      />
                    }
                  </li>
                }
              </ul>
            }
          } @else {
            <weave-operate-state kind="partial" (retry)="poller.refresh()" />
          }
        </section>

        @let dayBand = dayState(s);
        @if (dayBand !== "none") {
          <section class="band" aria-labelledby="overview-day">
            <div class="band-head">
              <h2 id="overview-day" class="band-title">Last 24 h</h2>
              @if (dayOf(s); as runs) {
                <span class="band-headline numeric">{{ headline(runs) }}</span>
              }
            </div>
            @if (dayBand === "unavailable") {
              <weave-operate-state
                kind="partial"
                (retry)="statisticsPoller.refresh()"
              />
            } @else if (dayBand === "loading") {
              <div class="day-charts" role="status">
                <span class="sr-only">Loading the last 24 hours…</span>
                <span
                  class="chart-card skeleton-block day-skeleton"
                  aria-hidden="true"
                ></span>
                <span
                  class="chart-card skeleton-block day-skeleton"
                  aria-hidden="true"
                ></span>
              </div>
            } @else {
              <div class="day-charts">
                <div class="chart-card">
                  <weave-time-chart
                    label="Runs per hour"
                    [series]="dayRuns"
                    [stacked]="[
                      'runs_succeeded',
                      'runs_failed',
                      'runs_cancelled',
                    ]"
                    [lines]="['runs_started']"
                    granularity="1h"
                  />
                </div>
                <div class="chart-card">
                  <weave-line-chart
                    label="Failure rate"
                    unit="ratio"
                    [series]="dayRate"
                    granularity="1h"
                    empty="No runs ended in the last 24 hours"
                  />
                </div>
              </div>
            }
          </section>
        }

        <section class="band" aria-labelledby="overview-capacity">
          <h2 id="overview-capacity" class="band-title">
            Workers and clusters
          </h2>
          <div class="panels">
            <article class="panel" aria-labelledby="overview-workers">
              <h3 id="overview-workers">Workers</h3>
              @let workers = sectionOf(s.workers);
              @if (workers.kind === "ready") {
                @let w = workers.value;
                <dl class="counts">
                  <div class="count-row">
                    <dt>Online</dt>
                    <dd class="numeric">{{ count(w.online) }}</dd>
                  </div>
                  <div class="count-row">
                    <dt>Offline</dt>
                    <dd class="numeric">{{ count(w.offline) }}</dd>
                  </div>
                  <div class="count-row">
                    <dt>Draining</dt>
                    <dd class="numeric">{{ count(w.draining) }}</dd>
                  </div>
                </dl>
                @let meter = meterOf(s);
                @if (meter) {
                  <div class="slots">
                    <span class="slots-label">Free slots and ready tasks</span>
                    <span
                      class="meter"
                      role="img"
                      [class.over]="meter.over"
                      [attr.aria-label]="
                        meter.text +
                        (meter.over ? ', more than workers can take' : '')
                      "
                      ><span class="meter-track" aria-hidden="true"
                        ><span
                          class="meter-fill"
                          [style.width.%]="meter.fill * 100"
                        ></span></span
                      ><span class="meter-text" aria-hidden="true">{{
                        meter.text
                      }}</span></span
                    >
                  </div>
                }
              } @else {
                <weave-operate-state
                  kind="partial"
                  (retry)="poller.refresh()"
                />
              }
            </article>
            <article class="panel" aria-labelledby="overview-clusters">
              <h3 id="overview-clusters">Clusters</h3>
              @let targets = sectionOf(s.targets);
              @if (targets.kind === "ready") {
                @let t = targets.value;
                <dl class="counts">
                  <div class="count-row">
                    <dt>Runners online</dt>
                    <dd class="numeric">{{ count(t.runner_online) }}</dd>
                  </div>
                  <div class="count-row">
                    <dt>Runners offline</dt>
                    <dd class="numeric">
                      {{ count(t.total - t.runner_online) }}
                    </dd>
                  </div>
                  <div class="count-row">
                    <dt>Busy targets</dt>
                    <dd class="numeric">{{ count(t.busy) }}</dd>
                  </div>
                  <div class="count-row">
                    <dt>Need reconciliation</dt>
                    <dd class="numeric">
                      {{ count(t.reconciliation_required) }}
                    </dd>
                  </div>
                  <div class="count-row">
                    <dt>Plans awaiting approval</dt>
                    <dd class="numeric">{{ count(t.awaiting_approval) }}</dd>
                  </div>
                </dl>
              } @else if (targets.kind === "forbidden") {
                <ng-container
                  *ngTemplateOutlet="
                    access;
                    context: { $implicit: 'deployment.read' }
                  "
                />
              } @else {
                <weave-operate-state
                  kind="partial"
                  (retry)="poller.refresh()"
                />
              }
            </article>
            <article class="panel" aria-labelledby="overview-incidents">
              <h3 id="overview-incidents">Incidents</h3>
              @let incidents = sectionOf(s.incidents);
              @if (incidents.kind === "ready") {
                <dl class="counts">
                  <div class="count-row">
                    <dt>Active</dt>
                    <dd class="numeric">
                      {{ count(incidents.value.active) }}
                    </dd>
                  </div>
                  <div class="count-row">
                    <dt>Opened in the last 24 hours</dt>
                    <dd class="numeric">
                      {{ count(incidents.value.opened_24h) }}
                    </dd>
                  </div>
                </dl>
              } @else if (incidents.kind === "forbidden") {
                <ng-container
                  *ngTemplateOutlet="
                    access;
                    context: { $implicit: 'incident.read' }
                  "
                />
              } @else {
                <weave-operate-state
                  kind="partial"
                  (retry)="poller.refresh()"
                />
              }
            </article>
          </div>
        </section>

        <section class="band" aria-labelledby="overview-attention">
          <h2 id="overview-attention" class="band-title">Needs attention</h2>
          @let attention = sectionOf(s.attention);
          @if (attention.kind === "ready") {
            @if (attention.value.length) {
              <ul class="attention" role="list">
                @for (item of attention.value; track item.kind + item.id) {
                  @let link = linkOf(item);
                  <li class="attention-item" [attr.data-kind]="item.kind">
                    <weave-icon
                      class="attention-icon"
                      [name]="labelOf(item)[1]"
                      [size]="16"
                    /><span class="attention-kind">{{ labelOf(item)[0] }}</span>
                    @if (link) {
                      <a
                        class="attention-title"
                        [href]="link"
                        (click)="follow($event, link)"
                        >{{ item.title }}</a
                      >
                    } @else {
                      <span class="attention-title">{{ item.title }}</span>
                    }
                    @if (item.expires_at) {
                      <time
                        class="attention-when"
                        [attr.datetime]="iso(item.expires_at)"
                        [attr.title]="absolute(item.expires_at)"
                        >Expires {{ ago(item.expires_at) }}</time
                      >
                    }
                  </li>
                }
              </ul>
            } @else {
              <p class="band-note">Nothing needs attention.</p>
            }
          } @else {
            <weave-operate-state kind="partial" (retry)="poller.refresh()" />
          }
        </section>
      </div>
    }
    <ng-template #access let-capability>
      <p class="section-access">
        You need <code>{{ capability }}</code> in this environment. Ask an
        administrator in Settings › People and access.
      </p>
    </ng-template>
    <ng-template #skeleton>
      <div class="overview" role="status">
        <span class="sr-only">Loading platform health…</span>
        <div class="band" aria-hidden="true">
          <span class="band-title skeleton-line"></span>
          <div class="health-strip">
            @for (tile of [1, 2, 3, 4, 5, 6]; track tile) {
              <span class="health-tile skeleton-block"></span>
            }
          </div>
        </div>
        <div class="band" aria-hidden="true">
          <span class="band-title skeleton-line"></span>
          <div class="figures">
            @for (tile of [1, 2, 3, 4, 5]; track tile) {
              <span class="figure skeleton-block"></span>
            }
          </div>
        </div>
      </div>
    </ng-template>
  `,
})
export class OverviewPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  readonly poller = new Poller(() => this.load(), 15_000);
  // Run statistics refresh every minute, only on a server that keeps them.
  readonly statisticsPoller = new Poller(() => this.loadStatistics(), 60_000);
  /** The hour's line under each Load figure, by figure ID. */
  loadLines = new Map<string, Array<number | null>>();
  /** The last 24 hours, hour by hour; null until read, or on a server without statistics. */
  day: StatisticsSeries[] | null = null;
  dayRuns: StatisticsSeries[] = [];
  dayRate: StatisticsSeries[] = [];
  private statisticsStarted = false;
  private statisticsSoon: ReturnType<typeof setTimeout> | undefined;
  /** False once the server answered that it keeps no run statistics. */
  private statisticsServed = true;
  /** The last read of the day's hours failed for another reason than missing statistics. */
  private dayFailed = false;
  readonly sectionOf = sectionOf;
  readonly count = countText;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  summary: OperationsSummary | null = null;
  error: PlainError | null = null;
  forbidden = false;
  /** The tile whose Details are open. */
  open: TileId | null = null;
  /** Milliseconds from this computer's clock to the server's. */
  private offset = 0;
  private scopeKey = "";
  private generation = 0;
  // Ages and uptime count on between loads; health itself never does.
  private ticker = setInterval(() => this.cdr.markForCheck(), 1000);

  get readable() {
    return this.host.canAnywhere("status.read");
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    clearInterval(this.ticker);
    clearTimeout(this.statisticsSoon);
    this.poller.stop();
    this.statisticsPoller.stop();
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    // Another platform, environment or person: nothing from before stays.
    this.scopeKey = key;
    this.generation++;
    this.summary = null;
    this.error = null;
    this.forbidden = false;
    this.open = null;
    this.offset = 0;
    this.statisticsPoller.stop();
    clearTimeout(this.statisticsSoon);
    this.statisticsStarted = false;
    this.statisticsServed = true;
    this.dayFailed = false;
    this.loadLines = new Map();
    this.day = null;
    this.dayRuns = [];
    this.dayRate = [];
    if (environmentOf(this.host) && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  private async load(): Promise<boolean> {
    if (!environmentOf(this.host) || !this.host.identity || !this.readable)
      return false;
    const key = scopeKeyOf(this.host),
      generation = this.generation;
    try {
      const value = await readSummary(this.host.api);
      if (generation !== this.generation || key !== scopeKeyOf(this.host))
        return false;
      this.summary = value;
      this.offset = clockOffset(value.observed_at, Date.now());
      this.error = null;
      this.forbidden = false;
      if (!this.statisticsStarted && keepsStatistics(value)) {
        this.statisticsStarted = true;
        // A moment after the summary, as a refresh of its own: the page's first
        // reads and the statistics never share one poll's byte budget.
        this.statisticsPoller.start(false);
        clearTimeout(this.statisticsSoon);
        this.statisticsSoon = setTimeout(
          () => void this.statisticsPoller.refresh(),
          STATISTICS_DELAY_MS,
        );
      }
      return true;
    } catch (error) {
      if (generation !== this.generation || key !== scopeKeyOf(this.host))
        return false;
      ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
      // An ended sign-in is the shell's to explain, with its own Sign in again.
      const plain = describeError(error);
      if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
        this.host.fail(error);
      throw error;
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** Now, by the server's clock. */
  private now() {
    return new Date(Date.now() + this.offset);
  }
  /**
   * The hour's Load lines and the last 24 hours. A read the server doesn't
   * serve stops these reads quietly; any other failure keeps what was shown.
   */
  private async loadStatistics(): Promise<boolean> {
    if (!environmentOf(this.host) || !this.host.identity || !this.readable)
      return false;
    const key = scopeKeyOf(this.host),
      generation = this.generation,
      now = Date.now();
    const path = `${this.host.api.environment}/run-statistics/series?`;
    const [load, day] = await Promise.allSettled([
      this.host.api.request<{ series: StatisticsSeries[] }>(
        path + loadSeriesQuery(now, this.offset),
      ),
      this.host.api.request<{ series: StatisticsSeries[] }>(
        path + dayQuery(now, this.offset),
      ),
    ]);
    if (generation !== this.generation || key !== scopeKeyOf(this.host))
      return false;
    try {
      for (const result of [load, day])
        if (
          result.status === "rejected" &&
          statisticsMissing(describeError(result.reason))
        ) {
          this.statisticsPoller.stop();
          this.statisticsServed = false;
          this.loadLines = new Map();
          this.day = null;
          this.dayRuns = [];
          this.dayRate = [];
          return false;
        }
      if (load.status === "fulfilled")
        this.loadLines = loadSparklines(
          Array.isArray(load.value?.series) ? load.value : null,
        );
      this.dayFailed = day.status === "rejected";
      if (day.status === "fulfilled") {
        this.day = Array.isArray(day.value?.series) ? day.value.series : null;
        this.dayRuns = (this.day ?? []).filter(
          (item) => item.signal !== "failure_rate",
        );
        this.dayRate = (this.day ?? []).filter(
          (item) => item.signal === "failure_rate",
        );
      }
      return load.status === "fulfilled" || day.status === "fulfilled";
    } finally {
      this.cdr.markForCheck();
    }
  }
  /**
   * The Last 24 h band on a server that keeps statistics: loading until the
   * hours answer, unavailable when the summary or the hours could not be read.
   */
  dayState(
    summary: OperationsSummary,
  ): "none" | "loading" | "unavailable" | "ready" {
    const runs = summary.runs_24h;
    if (!runs || !this.statisticsServed) return "none";
    if ("state" in runs || this.dayFailed) return "unavailable";
    return this.day ? "ready" : "loading";
  }
  /** The Last 24 h band shows when the summary has the day's runs and the hours answered. */
  dayOf(summary: OperationsSummary): RunsDaySection | null {
    const runs = summary.runs_24h;
    if (!runs || !this.day || "state" in runs) return null;
    return runs;
  }
  headline(runs: RunsDaySection) {
    return dayHeadline(runs);
  }
  tiles(): HealthTile[] {
    return this.summary ? healthTiles(this.summary, this.now()) : [];
  }
  toggle(id: TileId) {
    this.open = this.open === id ? null : id;
  }
  /** Focus moved on past the tile: its Details close, so they never cover it. */
  leave(event: FocusEvent, id: TileId) {
    const tile = event.currentTarget as HTMLElement;
    if (this.open === id && !tile.contains(event.relatedTarget as Node | null))
      this.open = null;
  }
  /** Closes Details and gives focus back to the button that opened them. */
  close(anchor: HTMLElement) {
    this.open = null;
    anchor.focus();
  }
  figures() {
    const load = this.summary ? sectionOf(this.summary.load) : null;
    return load?.kind === "ready" ? loadFigures(load.value) : [];
  }
  quiet() {
    return !!this.summary && isQuiet(this.summary);
  }
  meterOf(summary: OperationsSummary) {
    const workers = sectionOf(summary.workers);
    const load = sectionOf(summary.load);
    return workers.kind === "ready"
      ? slotMeter(workers.value, load.kind === "ready" ? load.value : null)
      : null;
  }
  stateLabel(state: HealthState) {
    return statusLabel("health", state);
  }
  tone(state: HealthState) {
    return toneAttribute(statusTone("health", state));
  }
  labelOf(item: AttentionItem) {
    return attentionLabels[item.kind];
  }
  linkOf(item: AttentionItem) {
    return attentionLink(item);
  }
  /** "2 min ago" or "in 4 min", by the server's clock. */
  ago(value: string) {
    return relativeTime(value, this.now()) || "at an unknown time";
  }
  go(view: View) {
    void this.host.navigate(view);
  }
  /** Opens an attention item's record in its page; a modified click opens a new tab. */
  async follow(event: MouseEvent, link: string) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey)
      return;
    event.preventDefault();
    const [path] = link.split("?");
    const route = viewFromPath(path);
    if (!route) return;
    if (route.view === "runs" && route.id) {
      await this.host.viewRun({ id: route.id });
      return;
    }
    await this.host.navigate(route.view as View);
    if (location.pathname + location.search !== link)
      await this.router.navigateByUrl(link, { replaceUrl: true });
  }
}
