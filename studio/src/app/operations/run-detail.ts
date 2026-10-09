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
// A run, read by an operator: what it is, what it waits for now, where it
// stopped, its workflow with the live thread, a plain-language timeline, and
// the technical details only on request. Part of the lazy operations chunk.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  OnDestroy,
  inject,
  input,
  signal,
} from "@angular/core";
import { FFlowModule } from "@foblex/flow";
import type { App } from "../app";
import { copyText } from "../connection";
import { Icon } from "../icon";
import { RunActions, runState } from "../operate/runs/run-actions";
import { StartRunDialog } from "../run/start-run-dialog";
import type { Node } from "../model";
import { absoluteTime, isoTime, relativeTime, shortId } from "../format";
import {
  runStatus,
  statusLabel,
  statusTone,
  toneAttribute,
} from "../status-labels";
import {
  finishedSteps,
  runIncident,
  runNow,
  runWorkflow,
  stepProgress,
  stepSummary,
  timeline,
  type RunStep,
} from "./records-model";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);

@Component({
  selector: "weave-run-detail",
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [Icon, FFlowModule, StartRunDialog],
  styleUrl: "./run-detail.css",
  template: `@let h = host();
    @let run = h.selectedRecord!;
    @let id = text(run["id"]);
    <header class="run-header">
      <div class="run-heading">
        <h2 id="record-detail-title" tabindex="-1">{{ title() }}</h2>
        <p class="run-meta">
          <span class="status-pill" [attr.data-tone]="tone()">{{
            status()
          }}</span>
          @if (run["business_key"]) {
            <span class="meta-item"
              >Business key <strong>{{ run["business_key"] }}</strong></span
            >
          }
          <span class="meta-item run-id"
            >Run ID <code class="tag" [attr.title]="id">{{ short(id) }}</code
            ><button
              type="button"
              class="tertiary sm"
              [attr.aria-label]="'Copy run ID ' + id"
              (click)="copyId(id)"
            >
              <weave-icon name="copy" [size]="16" />Copy
            </button></span
          >
        </p>
      </div>
      <button
        class="icon-button"
        aria-label="Close detail"
        (click)="h.closeRecord()"
      >
        <weave-icon name="close" />
      </button>
    </header>
    @if (now(); as now) {
      <section class="run-now" aria-label="Now">
        <span class="now-label">Now</span>
        <p>
          {{ now.lead }}
          <strong>{{ now.subject }}</strong
          ><span>{{ now.rest }}</span>
        </p>
        @if (now.action) {
          <button type="button" class="primary sm" (click)="h.openRunTask()">
            {{ now.action === "task" ? "Open task" : "Open My tasks" }}
          </button>
        }
      </section>
    }
    @if (incident(); as incident) {
      <div class="notice incident" data-tone="danger" role="note">
        <weave-icon name="failCircle" [size]="20" />
        <div>
          <p>
            <strong>This run stopped at {{ incident.step }}.</strong>
          </p>
          @if (incident.text) {
            <p class="incident-text">{{ incident.text }}</p>
          }
        </div>
      </div>
    }
    <div class="action-row run-controls">
      @if (waiting() && h.can("run.signal", id)) {
        <button
          [disabled]="h.busy !== '' || h.unknownCommand"
          (click)="actions.signal()"
        >
          Send signal
        </button>
      }
      @if (!h.runTerminal() && paused() && h.can("run.resume", id)) {
        <button
          [disabled]="h.busy !== '' || h.unknownCommand"
          (click)="h.runControl('resume')"
        >
          Resume run
        </button>
      } @else if (!h.runTerminal() && !paused() && h.can("run.pause", id)) {
        <button
          [disabled]="h.busy !== '' || h.unknownCommand"
          (click)="h.runControl('pause')"
        >
          Pause run
        </button>
      }
      @if (!h.runTerminal() && h.can("run.cancel", id)) {
        <button
          class="danger"
          [disabled]="h.busy !== '' || h.unknownCommand"
          (click)="actions.cancel()"
        >
          Cancel run
        </button>
      }
      @if (h.runTerminal() && h.can("run.retry", id)) {
        <button
          [disabled]="h.busy !== '' || h.unknownCommand"
          (click)="actions.openRetry()"
        >
          Retry run
        </button>
      }
      <button
        class="tertiary"
        [attr.aria-disabled]="actions.exporting ? 'true' : null"
        (click)="actions.exportHistory()"
      >
        <weave-icon name="download" [size]="16" />Export history
      </button>
    </div>
    @if (actions.retry; as retry) {
      <weave-start-run-dialog
        heading="Retry this run"
        closeLabel="Close retry this run"
        [description]="
          'Starts a new run linked to run ' +
          short(id) +
          ' with the input below.' +
          retry.notice
        "
        submitLabel="Retry run"
        busyLabel="Retrying…"
        [api]="h.api"
        [fileAccess]="h.fileAccess"
        [activations]="retry.activations"
        [labels]="retry.labels"
        [initialActivationId]="retry.initial"
        [initialInput]="retry.input"
        [initialKeys]="retry.keys"
        [versions]="h.workflowVersions"
        [busy]="h.busy !== '' || h.unknownCommand"
        [failure]="retry.failure"
        (start)="actions.confirmRetry($event)"
        (cancel)="actions.retry = null"
      />
    }
    @if (h.runCanvas; as canvas) {
      <section class="run-workflow" aria-labelledby="run-graph-title">
        <h3 id="run-graph-title">Workflow {{ workflowName() }}</h3>
        <div
          class="run-graph"
          tabindex="0"
          role="group"
          aria-label="Workflow version for this run"
          aria-describedby="run-steps"
        >
          <div
            class="run-graph-world"
            [style.width.px]="world().width"
            [style.height.px]="world().height"
          >
            <f-flow
              ><f-canvas [scale]="1" [position]="{ x: 24, y: 16 }"
                ><div fNodes>
                  <svg
                    class="edges"
                    [attr.width]="world().width"
                    [attr.height]="world().height"
                    aria-hidden="true"
                  >
                    @for (edge of edges(); track $index) {
                      <g [class.edge-taken]="edge.taken">
                        <path
                          [attr.d]="edge.d"
                          fill="none"
                          stroke-width="1.5"
                        />
                      </g>
                    }
                  </svg>
                  @for (boundary of ["start", "end"]; track boundary) {
                    <div
                      fNode
                      [fNodeId]="'run-$' + boundary"
                      [fNodePosition]="
                        boundary === 'start'
                          ? canvas.boundaries().start
                          : canvas.boundaries().end
                      "
                      class="boundary-node"
                      [class.end]="boundary === 'end'"
                      role="img"
                      [attr.aria-label]="
                        boundary === 'start' ? 'Workflow start' : 'Workflow end'
                      "
                    >
                      {{ boundary === "start" ? "Start" : "End" }}
                    </div>
                  }
                  @for (node of nodes(); track node.step.id) {
                    @let progress = progressOf(node.step.id);
                    <div
                      fNode
                      [fNodeId]="'run-' + node.step.id"
                      [fNodePosition]="node.point"
                      class="run-node"
                      [class.is-live]="progress === 'live'"
                      [class.is-done]="progress === 'done'"
                      [class.is-unreached]="progress === 'unreached'"
                    >
                      <span class="step-icon"
                        ><weave-icon [name]="node.step.kind" [size]="16"
                      /></span>
                      <span class="node-text"
                        ><strong>{{ node.step.id }}</strong
                        ><small>{{ h.label(node.step.kind) }}</small></span
                      >
                      @if (progress === "live") {
                        <span class="run-active-label">Now</span>
                      } @else if (progress === "done") {
                        <span class="node-badge"
                          ><weave-icon name="check" [size]="16"
                        /></span>
                      }
                    </div>
                  }</div></f-canvas
            ></f-flow>
          </div>
        </div>
        <ol id="run-steps" class="sr-only" aria-label="Steps in this version">
          @for (step of steps(); track step.id) {
            <li>{{ summary(step) }}</li>
          }
        </ol>
      </section>
    }
    <section class="run-timeline" aria-labelledby="run-timeline-title">
      <h3 id="run-timeline-title">Timeline</h3>
      @if (rows().length) {
        <ol class="timeline">
          @for (row of rows(); track $index) {
            <li [attr.data-tone]="row.tone">
              <span class="dot" aria-hidden="true"></span>
              <div class="event">
                <p>
                  <span class="sentence">{{ row.sentence }}</span>
                  @if (row.at) {
                    <time
                      [attr.datetime]="iso(row.at)"
                      [attr.title]="absolute(row.at)"
                      >{{ relative(row.at) }}</time
                    >
                  }
                  <button
                    type="button"
                    class="tertiary sm raw-toggle"
                    [attr.aria-expanded]="showsRaw($index)"
                    [attr.aria-controls]="'raw-event-' + $index"
                    (click)="toggleRaw($index)"
                  >
                    View raw event
                  </button>
                </p>
                @if (showsRaw($index)) {
                  <pre [id]="'raw-event-' + $index">{{
                    h.pretty(row.raw)
                  }}</pre>
                }
              </div>
            </li>
          }
        </ol>
      } @else {
        <p class="hint">No events recorded yet.</p>
      }
    </section>
    <details class="disclosure technical">
      <summary>Technical details</summary>
      <dl class="task-metadata">
        <dt>Run ID</dt>
        <dd>
          <code class="mono-id">{{ id }}</code>
        </dd>
        <dt>Correlation key</dt>
        <dd>{{ run["correlation_key"] || "None" }}</dd>
        <dt>Artifact</dt>
        <dd>
          <code class="mono-id">{{ run["artifact_digest"] || "—" }}</code>
        </dd>
        @if (activationRevision()) {
          <dt>Activation revision</dt>
          <dd>{{ activationRevision() }}</dd>
        }
      </dl>
      <h4>Run data (JSON)</h4>
      <pre>{{ h.pretty(run["state"]) }}</pre>
    </details>
    @if (h.runLifecycle; as lifecycle) {
      @if (h.can("run.archive", id)) {
        <section class="run-archive" aria-labelledby="run-archive-title">
          <h3 id="run-archive-title">Archive</h3>
          <p class="archive-state">
            {{
              lifecycle["purged"]
                ? "Data deleted. The audit record stays."
                : lifecycle["archived"]
                  ? "Archived. Hidden from Runs until you include archived runs."
                  : "Visible in Runs"
            }}
          </p>
          @if (!lifecycle["purged"]) {
            <div class="action-row">
              @if (lifecycle["archived"]) {
                <button
                  [disabled]="h.busy !== '' || h.unknownCommand"
                  (click)="h.lifecycleCommand('restore')"
                >
                  Restore run
                </button>
              } @else {
                <button
                  [disabled]="
                    !h.runTerminal() || h.busy !== '' || h.unknownCommand
                  "
                  [attr.aria-describedby]="
                    h.runTerminal() ? null : 'archive-hint'
                  "
                  (click)="h.lifecycleCommand('archive')"
                >
                  Archive run
                </button>
              }
              @if (lifecycle["archived"] && h.can("run.purge", id)) {
                <button
                  class="danger"
                  [disabled]="
                    !h.runTerminal() || h.busy !== '' || h.unknownCommand
                  "
                  (click)="h.lifecycleCommand('purge')"
                >
                  Delete run data
                </button>
              }
            </div>
            <p class="hint" id="archive-hint">
              You can archive a run once it finishes. Archived runs are hidden
              until you include them.
            </p>
          }
        </section>
      }
    }
    <div class="action-row detail-refresh">
      <button type="button" class="tertiary" (click)="h.readDetail()">
        <weave-icon name="refresh" [size]="16" />Refresh
      </button>
    </div>`,
})
export class RunDetail implements DoCheck, OnDestroy {
  host = input.required<App>();
  private cdr = inject(ChangeDetectorRef);
  /** Cancel run, Retry run, Send signal and Export history. */
  readonly actions = new RunActions(
    () => this.host(),
    () => this.cdr.markForCheck(),
  );

  ngDoCheck() {
    // A Retry run dialog belongs to the run it was opened for.
    this.actions.follow(this.text(this.run()["id"]));
  }
  ngOnDestroy() {
    this.actions.destroy();
  }

  private run(): Json {
    return this.host().selectedRecord ?? {};
  }
  text(value: unknown) {
    return typeof value === "string" ? value : "";
  }
  short(id: string) {
    return shortId(id);
  }
  status() {
    return statusLabel("run", runStatus(this.run()));
  }
  tone() {
    return toneAttribute(statusTone("run", runStatus(this.run())));
  }
  /** Waiting for an event; a paused run still is, and may be sent a signal. */
  waiting() {
    return runState(this.run()) === "waiting";
  }
  paused() {
    const state = this.run()["state"];
    return isRecord(state) && !!state["manual_paused"];
  }
  /** "expense-review 1.0.0", or the business key, or "Run 700587c4". */
  title() {
    const run = this.run();
    return (
      runWorkflow(run, { versions: this.host().workflowVersions }) ||
      this.text(run["business_key"]) ||
      `Run ${shortId(run["id"])}`
    );
  }
  /** The workflow version the run's graph shows. */
  workflowName() {
    const metadata = this.host().runCanvas?.definition.metadata;
    return metadata ? `${metadata.name} ${metadata.version}` : "";
  }
  activationRevision() {
    const activation = this.run()["activation"];
    return isRecord(activation) ? (activation["revision"] ?? "") : "";
  }
  nodes(): Node[] {
    return this.host().runNodes();
  }
  steps(): RunStep[] {
    return this.nodes().map((node) => ({
      id: node.step.id,
      kind: node.step.kind,
      assignment:
        typeof node.step["assignment"] === "string"
          ? node.step["assignment"]
          : undefined,
    }));
  }
  /** Kept while the run, its task and its workflow stay the same. */
  private nowCache: {
    key: unknown[];
    now: ReturnType<typeof runNow>;
    incident: ReturnType<typeof runIncident>;
  } = { key: [], now: null, incident: null };
  private derived() {
    const h = this.host();
    const key = [this.run(), h.runTask, h.runCanvas];
    if (key.some((part, i) => part !== this.nowCache.key[i]))
      this.nowCache = {
        key,
        now: runNow(this.run(), this.steps(), h.runTask),
        incident: runIncident(this.run()),
      };
    return this.nowCache;
  }
  now() {
    return this.derived().now;
  }
  incident() {
    return this.derived().incident;
  }
  private finishedCache: { history: unknown; steps: Set<string> } = {
    history: null,
    steps: new Set(),
  };
  /** Steps the history says finished (read once per history page). */
  private finished() {
    const history = this.host().runHistory;
    if (this.finishedCache.history !== history)
      this.finishedCache = { history, steps: finishedSteps(history) };
    return this.finishedCache.steps;
  }
  progressOf(id: string) {
    return stepProgress(this.run(), id, this.finished());
  }
  summary(step: RunStep) {
    return stepSummary(this.run(), step, this.finished());
  }
  /** Timeline rows whose raw event is shown, for one run. */
  private readonly raw = signal<{ run: unknown; rows: ReadonlySet<number> }>({
    run: null,
    rows: new Set(),
  });
  showsRaw(index: number) {
    const raw = this.raw();
    return raw.run === this.run()["id"] && raw.rows.has(index);
  }
  toggleRaw(index: number) {
    const run = this.run()["id"];
    const rows = new Set(this.raw().run === run ? this.raw().rows : []);
    if (rows.has(index)) rows.delete(index);
    else rows.add(index);
    this.raw.set({ run, rows });
  }
  /** The drawing's size at 100%: every node and the end boundary fit. */
  world() {
    const canvas = this.host().runCanvas;
    if (!canvas) return { width: 320, height: 220 };
    const points = [
      ...this.nodes().map((node) => node.point),
      canvas.boundaries().end,
    ];
    return {
      width: Math.max(320, ...points.map((p) => p.x)) + 240 + 48,
      height: Math.max(220, ...points.map((p) => p.y)) + 64 + 40,
    };
  }
  /** Edges, with the path the run took drawn as the live thread. */
  edges() {
    const h = this.host();
    const canvas = h.runCanvas;
    if (!canvas) return [];
    const nodes = this.nodes();
    const run = this.run();
    const finished = runStatus(run) === "succeeded";
    const done = this.finished();
    const reached = (id: string) =>
      id === "$start" ||
      (id === "$end" && finished) ||
      (!id.startsWith("$") && stepProgress(run, id, done) !== "unreached");
    return canvas.visualConnections().map((link) => ({
      d: h.connectionPath(canvas, link.from, link.to, nodes),
      taken:
        (link.from === "$start" ||
          stepProgress(run, link.from, done) === "done") &&
        reached(link.to),
    }));
  }
  private rowsCache: { history: unknown; rows: ReturnType<typeof timeline> } = {
    history: undefined,
    rows: [],
  };
  rows() {
    const history = this.host().runHistory;
    if (this.rowsCache.history !== history)
      this.rowsCache = { history, rows: timeline(history) };
    return this.rowsCache.rows;
  }
  iso(value: string) {
    return isoTime(value) || null;
  }
  absolute(value: string) {
    return absoluteTime(value);
  }
  relative(value: string) {
    return relativeTime(value);
  }
  async copyId(id: string) {
    const h = this.host();
    if (await copyText(id)) h.notify("Copied the run ID.");
    else h.notify("Studio couldn't copy. Select the run ID and copy it.");
  }
}
