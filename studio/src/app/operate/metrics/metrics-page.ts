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
// Operate › Metrics: throughput, failure rate and durations per workflow and
// per step, from the run statistics the platform keeps itself, plus the
// queue, outbox and worker load over the same range. Reloads every 60 s while
// visible. Each chart shows its own problem; the rest of the page stays
// usable. Test runs never count.
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
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { exportFile, exportNotice } from "../../export-file";
import { relativeTime } from "../../format";
import { environmentOf, listFailure, scopeKeyOf } from "../operate-list";
import {
  metricsPath,
  metricsRanges,
  metricsRoute,
  type MetricsRange,
  type MetricsRoute,
} from "../operate-routes";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import { timeAbsolute, timeIso } from "../operate-time";
import { RefreshStatus } from "../refresh-status";
import { statisticsMissing, type Unit } from "./chart-model";
import { LineChart, Sparkline, TimeChart } from "./charts";
import {
  chartRequests,
  freshness,
  kpis,
  rangeLabels,
  rangeWindow,
  sparklineRequest,
  sparklines,
  stepCsv,
  stepRows,
  workflowCsv,
  workflowRows,
  type ChartId,
  type SeriesAnswer,
  type StepAnswer,
  type WorkflowAnswer,
} from "./metrics-model";

interface ChartView {
  id: ChartId;
  label: string;
  unit: Unit;
  stacked: string[];
  lines: string[];
  empty: string;
}
const CHARTS: ChartView[] = [
  {
    id: "throughput",
    label: "Throughput",
    unit: "count",
    stacked: ["runs_succeeded", "runs_failed", "runs_cancelled"],
    lines: ["runs_started"],
    empty: "No runs in this range",
  },
  {
    id: "rate",
    label: "Failure rate",
    unit: "ratio",
    stacked: [],
    lines: [],
    empty: "No runs ended in this range",
  },
  {
    id: "durations",
    label: "Run duration, p50 and p95",
    unit: "ms",
    stacked: [],
    lines: [],
    empty: "No runs ended in this range",
  },
  {
    id: "queue",
    label: "Ready tasks by task type",
    unit: "count",
    stacked: [],
    lines: [],
    empty: "No ready tasks in this range",
  },
  {
    id: "outbox",
    label: "Outbox backlog",
    unit: "count",
    stacked: [],
    lines: [],
    empty: "No outbox backlog in this range",
  },
  {
    id: "workers",
    label: "Worker utilization",
    unit: "ratio",
    stacked: [],
    lines: [],
    empty: "No worker capacity in this range",
  },
];
const POLL_MS = 60_000;
/** An answer whose list is missing (an older or proxied server) reads as an empty one. */
function listed<T extends object>(value: T, key: keyof T): T {
  return Array.isArray(value?.[key]) ? value : { ...value, [key]: [] };
}

@Component({
  selector: "weave-metrics-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    LineChart,
    NgTemplateOutlet,
    OperateState,
    RefreshStatus,
    Sparkline,
    TimeChart,
  ],
  styleUrls: ["../operate.css", "./metrics.css"],
  host: { class: "metrics-page" },
  template: `
    <div class="operate-heading">
      <div>
        <h1>Metrics</h1>
        <p class="subtitle">
          Throughput, failures and durations of the runs in this environment.
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
        <h2>Metrics appear here once you connect</h2>
        <p>Metrics read the run statistics of a platform environment.</p>
        <button class="primary" (click)="host.openWizard('server')">
          Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded || host.platformSignedOut) {
      <div class="empty-state">
        <h2>Sign in to see metrics</h2>
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
      <ng-container *ngTemplateOutlet="skeleton" />
    } @else if (!readable || forbidden) {
      <weave-operate-state kind="access" capability="status.read" />
    } @else if (unsupported) {
      <weave-operate-state
        kind="empty"
        heading="This platform doesn't keep run statistics yet"
        text="Upgrade the platform to see throughput, failures and durations here."
      />
    } @else {
      <div class="metrics-controls">
        <div class="range-group" role="group" aria-label="Range">
          @for (range of ranges; track range) {
            <button
              type="button"
              class="range-option"
              [attr.aria-pressed]="route.range === range"
              (click)="setRange(range)"
            >
              {{ rangeLabel(range) }}
            </button>
          }
        </div>
        <label class="workflow-field">
          <span>Workflow</span>
          <select
            [value]="route.workflow"
            (change)="setWorkflow(selected($event))"
          >
            <option value="">All workflows</option>
            @for (name of workflowNames(); track name) {
              <option [value]="name" [selected]="name === route.workflow">
                {{ name }}
              </option>
            }
          </select>
        </label>
        @if (loaded) {
          <span
            class="freshness"
            [attr.title]="
              completeUntil ? absolute(completeUntil) : 'Nothing rolled up yet'
            "
            >{{ freshnessText() }}</span
          >
        }
      </div>
      @if (!loaded && error) {
        <weave-operate-state
          kind="error"
          [message]="error.message"
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      } @else if (!loaded) {
        <ng-container *ngTemplateOutlet="skeleton" />
      } @else {
        @if (error) {
          <weave-operate-state
            kind="error"
            message="Studio could not refresh the metrics. The figures below are from its last answer."
            [code]="error.code"
            (retry)="poller.refresh()"
          />
        }
        <div class="metrics" data-testid="metrics-meaningful">
          <ul class="kpis" role="list" aria-label="Figures for this range">
            @for (figure of figures(); track figure.id) {
              <li class="kpi" [attr.data-kpi]="figure.id">
                <span class="kpi-label">{{ figure.label }}</span>
                <span class="kpi-value numeric">{{ figure.value }}</span>
              </li>
            }
          </ul>
          <p class="metrics-note">Test runs are not included.</p>
          @if (quiet()) {
            <div class="band-empty metrics-empty">
              <p>
                No runs in this range yet. Run a workflow to see throughput,
                failures and durations here.
              </p>
            </div>
          }
          <div class="chart-grid">
            @for (chart of charts; track chart.id) {
              <section class="chart-card" [attr.data-chart]="chart.id">
                @if (chartAnswer(chart.id); as answer) {
                  @if (chart.stacked.length) {
                    <weave-time-chart
                      [series]="answer.series"
                      [stacked]="chart.stacked"
                      [lines]="chart.lines"
                      [unit]="chart.unit"
                      [label]="chart.label"
                      [granularity]="answer.granularity"
                      [empty]="chart.empty"
                    />
                  } @else {
                    <weave-line-chart
                      [series]="chartSeries(chart.id, answer)"
                      [unit]="chart.unit"
                      [label]="chart.label"
                      [granularity]="answer.granularity"
                      [empty]="chart.empty"
                    />
                  }
                } @else if (chartErrors[chart.id]; as failure) {
                  <h3 class="chart-title">{{ chart.label }}</h3>
                  <weave-operate-state
                    kind="partial"
                    [code]="failure.code"
                    (retry)="poller.refresh()"
                  />
                } @else {
                  <h3 class="chart-title">{{ chart.label }}</h3>
                  <weave-operate-state
                    kind="loading"
                    [noun]="chart.label"
                    [rows]="4"
                  />
                }
              </section>
            }
          </div>
          @if (route.workflow) {
            <section class="metrics-table-band" aria-labelledby="metrics-steps">
              <div class="table-heading">
                <h2 id="metrics-steps">Steps of {{ route.workflow }}</h2>
                <div class="table-actions">
                  <a
                    class="back-link"
                    [href]="pathOf({ range: route.range, workflow: '' })"
                    (click)="follow($event, '')"
                    >Back to all workflows</a
                  >
                  <button
                    type="button"
                    [disabled]="!steps?.steps?.length"
                    (click)="exportSteps()"
                  >
                    Export CSV
                  </button>
                </div>
              </div>
              @if (steps) {
                @if (stepTable().length) {
                  <div class="metrics-table-wrap">
                    <table class="metrics-table steps-table">
                      <caption class="sr-only">
                        Steps of
                        {{
                          route.workflow
                        }}
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">Step</th>
                          <th scope="col" data-narrow>Kind</th>
                          <th scope="col" class="numeric-cell">Executions</th>
                          <th scope="col" class="numeric-cell">Failures</th>
                          <th scope="col" class="numeric-cell" data-narrow>
                            p50
                          </th>
                          <th scope="col" class="numeric-cell">p95</th>
                          <th scope="col" class="numeric-cell" data-wide>
                            p95 queue wait
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        @for (row of stepTable(); track row.step) {
                          <tr>
                            <th scope="row" class="name-cell">
                              {{ row.step }}
                            </th>
                            <td data-narrow>{{ row.kind }}</td>
                            <td class="numeric numeric-cell">
                              {{ row.executions }}
                            </td>
                            <td class="numeric numeric-cell">
                              {{ row.failures }}
                            </td>
                            <td class="numeric numeric-cell" data-narrow>
                              {{ row.p50 }}
                            </td>
                            <td class="numeric numeric-cell">{{ row.p95 }}</td>
                            <td class="numeric numeric-cell" data-wide>
                              {{ row.queueP95 }}
                            </td>
                          </tr>
                        }
                      </tbody>
                    </table>
                  </div>
                } @else {
                  <p class="band-note">
                    No step of this workflow ran in this range.
                  </p>
                }
              } @else if (stepsError) {
                <weave-operate-state
                  kind="partial"
                  [code]="stepsError.code"
                  (retry)="poller.refresh()"
                />
              } @else {
                <weave-operate-state kind="loading" noun="steps" [rows]="3" />
              }
            </section>
          } @else {
            <section
              class="metrics-table-band"
              aria-labelledby="metrics-workflows"
            >
              <div class="table-heading">
                <h2 id="metrics-workflows">Workflows</h2>
                <div class="table-actions">
                  <button
                    type="button"
                    [disabled]="!workflows?.workflows?.length"
                    (click)="exportWorkflows()"
                  >
                    Export CSV
                  </button>
                </div>
              </div>
              @if (workflows) {
                @if (workflowTable().length) {
                  <div class="metrics-table-wrap">
                    <table class="metrics-table workflows-table">
                      <caption class="sr-only">
                        Workflows
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">Workflow</th>
                          <th scope="col" data-wide>Versions</th>
                          <th scope="col" class="numeric-cell">Runs</th>
                          <th scope="col" class="numeric-cell">Failure rate</th>
                          <th scope="col" class="numeric-cell" data-narrow>
                            p50
                          </th>
                          <th scope="col" class="numeric-cell">p95</th>
                          <th scope="col" data-wide>Last run</th>
                          <th scope="col" data-wide>Last 24 h</th>
                        </tr>
                      </thead>
                      <tbody>
                        @for (row of workflowTable(); track row.workflow) {
                          <tr>
                            <th scope="row" class="name-cell">
                              @if (row.other) {
                                {{ row.workflow }}
                              } @else {
                                <a
                                  [href]="
                                    pathOf({
                                      range: route.range,
                                      workflow: row.workflow,
                                    })
                                  "
                                  (click)="follow($event, row.workflow)"
                                  >{{ row.workflow }}</a
                                >
                              }
                            </th>
                            <td data-wide>{{ row.versions }}</td>
                            <td class="numeric numeric-cell">{{ row.runs }}</td>
                            <td class="numeric numeric-cell">
                              {{ row.failureRate }}
                            </td>
                            <td class="numeric numeric-cell" data-narrow>
                              {{ row.p50 }}
                            </td>
                            <td class="numeric numeric-cell">{{ row.p95 }}</td>
                            <td data-wide>
                              @if (row.lastRunAt) {
                                <time
                                  [attr.datetime]="iso(row.lastRunAt)"
                                  [attr.title]="absolute(row.lastRunAt)"
                                  >{{ ago(row.lastRunAt) }}</time
                                >
                              } @else {
                                —
                              }
                            </td>
                            <td data-wide class="spark-cell">
                              @if (lines.get(row.workflow); as points) {
                                <weave-sparkline
                                  [points]="points"
                                  [name]="'Runs started of ' + row.workflow"
                                  period="the last 24 hours"
                                />
                              }
                            </td>
                          </tr>
                        }
                      </tbody>
                    </table>
                  </div>
                } @else {
                  <p class="band-note">No workflow ran in this range.</p>
                }
              } @else {
                <weave-operate-state
                  kind="partial"
                  [code]="workflowsError?.code ?? ''"
                  (retry)="poller.refresh()"
                />
              }
            </section>
          }
        </div>
      }
    }
    <ng-template #skeleton>
      <div class="metrics" role="status">
        <span class="sr-only">Loading metrics…</span>
        <div class="kpis" aria-hidden="true">
          @for (tile of [1, 2, 3, 4, 5, 6]; track tile) {
            <span class="kpi skeleton-block"></span>
          }
        </div>
        <div class="chart-grid" aria-hidden="true">
          @for (tile of [1, 2, 3, 4, 5, 6]; track tile) {
            <span class="chart-card skeleton-block chart-skeleton"></span>
          }
        </div>
      </div>
    </ng-template>
  `,
})
export class MetricsPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  readonly poller = new Poller(() => this.load(), POLL_MS);
  readonly ranges = metricsRanges;
  readonly charts = CHARTS;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly pathOf = metricsPath;
  route: MetricsRoute = metricsRoute(location.search);
  answers: Partial<Record<ChartId, SeriesAnswer>> = {};
  chartErrors: Partial<Record<ChartId, PlainError>> = {};
  workflows: WorkflowAnswer | null = null;
  workflowsError: PlainError | null = null;
  steps: StepAnswer | null = null;
  stepsError: PlainError | null = null;
  lines = new Map<string, Array<number | null>>();
  completeUntil: string | null = null;
  loaded = false;
  /** The server answers none of the statistics reads: an older platform. */
  unsupported = false;
  error: PlainError | null = null;
  forbidden = false;
  private scopeKey = "";
  private search = location.search;
  private generation = 0;

  get readable() {
    return this.host.canAnywhere("status.read");
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.poller.stop();
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    // Back and Forward change the range or the workflow without this page.
    if (
      location.pathname === "/operate/metrics" &&
      location.search !== this.search
    ) {
      this.search = location.search;
      const route = metricsRoute(location.search);
      if (
        route.range !== this.route.range ||
        route.workflow !== this.route.workflow
      ) {
        this.route = route;
        this.forget(false);
        void this.poller.refresh();
      }
    }
    if (key === this.scopeKey) return;
    // Another platform, environment or person: nothing from before stays.
    this.scopeKey = key;
    this.forget(true);
    // An older platform stopped the reads; another one may serve them.
    this.poller.start(false);
    if (environmentOf(this.host) && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  /** Drops what was read; a new range or workflow keeps the page's frame. */
  private forget(everything: boolean) {
    this.generation++;
    this.answers = {};
    this.chartErrors = {};
    this.steps = null;
    this.stepsError = null;
    if (everything) {
      this.workflows = null;
      this.workflowsError = null;
      this.lines = new Map();
      this.completeUntil = null;
      this.loaded = false;
      this.unsupported = false;
      this.error = null;
      this.forbidden = false;
    }
  }
  private read<T>(path: string, query: string): Promise<T> {
    return this.host.api.request<T>(
      `${this.host.api.environment}/run-statistics/${path}?${query}`,
    );
  }
  private async load(): Promise<boolean> {
    if (!environmentOf(this.host) || !this.host.identity || !this.readable)
      return false;
    const key = scopeKeyOf(this.host),
      generation = this.generation,
      route = this.route;
    const now = Date.now();
    const window = rangeWindow(route.range, now);
    const range = new URLSearchParams(window).toString();
    const charts = chartRequests(window, route.workflow);
    const [workflows, lines, steps, ...series] = await Promise.allSettled([
      this.read<WorkflowAnswer>("workflows", range),
      this.read<SeriesAnswer>("series", sparklineRequest(now)),
      route.workflow
        ? this.read<StepAnswer>(
            "steps",
            `${range}&${new URLSearchParams({ workflow: route.workflow })}`,
          )
        : Promise.resolve(null),
      ...charts.map(({ query }) => this.read<SeriesAnswer>("series", query)),
    ]);
    if (generation !== this.generation || key !== scopeKeyOf(this.host))
      return false;
    try {
      const failures = [workflows, lines, steps, ...series].filter(
        (result) => result.status === "rejected",
      );
      const answered = series.some((result) => result.status === "fulfilled");
      if (!answered && workflows.status === "rejected") {
        if (
          failures.every((result) =>
            statisticsMissing(
              describeError((result as PromiseRejectedResult).reason),
            ),
          )
        ) {
          this.unsupported = true;
          this.poller.stop();
          return false;
        }
        const reason = (failures[0] as PromiseRejectedResult).reason;
        ({ error: this.error, forbidden: this.forbidden } =
          listFailure(reason));
        // An ended sign-in is the shell's to explain, with its own Sign in again.
        const plain = describeError(reason);
        if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
          this.host.fail(reason);
        throw reason;
      }
      this.answers = {};
      this.chartErrors = {};
      charts.forEach(({ chart }, index) => {
        const result = series[index];
        if (result.status === "fulfilled")
          this.answers[chart] = listed(result.value, "series");
        else this.chartErrors[chart] = describeError(result.reason);
      });
      if (workflows.status === "fulfilled") {
        this.workflows = listed(workflows.value, "workflows");
        this.workflowsError = null;
      } else {
        this.workflows = null;
        this.workflowsError = describeError(workflows.reason);
      }
      this.lines =
        lines.status === "fulfilled"
          ? sparklines(listed(lines.value, "series"))
          : new Map();
      if (steps.status === "fulfilled") {
        this.steps = steps.value && listed(steps.value, "steps");
        this.stepsError = null;
      } else {
        this.steps = null;
        this.stepsError = describeError(steps.reason);
      }
      const fresh = [...Object.values(this.answers), this.workflows].find(
        (answer) => answer,
      )?.complete_until;
      this.completeUntil = fresh ?? null;
      this.loaded = true;
      this.error = null;
      this.forbidden = false;
      return true;
    } finally {
      this.cdr.markForCheck();
    }
  }
  chartAnswer(id: ChartId): SeriesAnswer | null {
    return this.answers[id] ?? null;
  }
  /** The queue chart draws ready tasks; the oldest ready age is a figure above. */
  chartSeries(id: ChartId, answer: SeriesAnswer) {
    return id === "queue"
      ? answer.series.filter((item) => item.signal === "tasks_ready")
      : answer.series;
  }
  figures() {
    return kpis({
      throughput: this.answers.throughput,
      queue: this.answers.queue,
      workflows: this.workflows ?? undefined,
      workflow: this.route.workflow,
    });
  }
  /** Nothing ran in the range: the charts stay, with this said once above them. */
  quiet() {
    const throughput = this.answers.throughput;
    return (
      !!throughput &&
      !!this.workflows &&
      !this.workflows.workflows.length &&
      throughput.series.every((item) =>
        item.points.every(([, value]) => !value),
      )
    );
  }
  workflowNames(): string[] {
    const names = (this.workflows?.workflows ?? [])
      .map((row) => row.workflow)
      .filter((name) => name !== "(other)");
    if (this.route.workflow && !names.includes(this.route.workflow))
      names.unshift(this.route.workflow);
    return names;
  }
  workflowTable() {
    return workflowRows(this.workflows);
  }
  stepTable() {
    return stepRows(this.steps);
  }
  freshnessText() {
    return freshness(this.completeUntil);
  }
  rangeLabel(range: MetricsRange) {
    return rangeLabels[range];
  }
  ago(value: string) {
    return relativeTime(value, new Date()) || "at an unknown time";
  }
  selected(event: Event) {
    return (event.target as HTMLSelectElement).value;
  }
  setRange(range: MetricsRange) {
    if (range === this.route.range) return;
    void this.go({ ...this.route, range });
  }
  setWorkflow(workflow: string) {
    if (workflow === this.route.workflow) return;
    void this.go({ ...this.route, workflow });
  }
  /** Opens a workflow's steps (or all workflows); a modified click opens a new tab. */
  follow(event: MouseEvent, workflow: string) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey)
      return;
    event.preventDefault();
    void this.go({ ...this.route, workflow });
  }
  private async go(route: MetricsRoute) {
    this.route = route;
    this.forget(false);
    const path = metricsPath(route);
    this.search = path.includes("?") ? path.slice(path.indexOf("?")) : "";
    if (location.pathname + location.search !== path)
      await this.router.navigateByUrl(path);
    await this.poller.refresh();
  }
  private save(name: string, text: string) {
    if (exportFile(name, "text/csv", text) === "downloaded")
      this.host.notify(exportNotice([name]));
    else
      this.host.notify(
        "Studio couldn't export the table.",
        undefined,
        "danger",
      );
  }
  exportWorkflows() {
    if (this.workflows)
      this.save(
        `metrics-workflows-${this.route.range}.csv`,
        workflowCsv(this.workflows),
      );
  }
  exportSteps() {
    if (this.steps)
      this.save(
        `metrics-steps-${this.route.workflow}-${this.route.range}.csv`,
        stepCsv(this.steps),
      );
  }
}
