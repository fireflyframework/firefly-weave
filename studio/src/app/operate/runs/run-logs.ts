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
// A run's Logs section, after its Steps: the open run's entries in time
// order, newest at the bottom, on every level and step until the person
// narrows them. They are read once the run opens, and every 5 s while it is
// still going; an older platform is asked once per session.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  OnDestroy,
  computed,
  input,
} from "@angular/core";
import type { App } from "../../app";
import { Icon } from "../../icon";
import { LogEntries } from "../logs/log-entries";
import { levelChoices, runCountText } from "../logs/logs-model";
import { RunLogsStore } from "../logs/logs-store";
import { environmentOf, scopeKeyOf } from "../operate-list";
import type { LogsLevel } from "../operate-routes";
import { Poller } from "../operate-store";
import { asRunViewsApi, type StepFact } from "../run-contracts";

/** How often a running run's entries are read again. */
const LIVE_MS = 5_000;

@Component({
  selector: "weave-run-logs",
  standalone: true,
  // The shell's fields (the person, the environment's name) are not signals.
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, LogEntries],
  styleUrls: ["../operate.css", "../logs/logs.css"],
  template: `@let s = store.state();
    @let filters = store.filters();
    <section class="run-logs" aria-labelledby="run-logs-title">
      <div class="run-logs-head">
        <h3 id="run-logs-title">Logs</h3>
        @if (
          s.kind !== "unserved" && s.kind !== "refused" && s.kind !== "withheld"
        ) {
          <div class="inline-field">
            <label for="run-logs-level">Level</label>
            <select id="run-logs-level" (change)="setLevel(value($event))">
              @for (choice of levels; track choice.value) {
                <option
                  [value]="choice.value"
                  [selected]="filters.level === choice.value"
                >
                  {{ choice.label }}
                </option>
              }
            </select>
          </div>
          <div class="inline-field">
            <label for="run-logs-step">Step</label>
            <select id="run-logs-step" (change)="setStep(value($event))">
              <option value="" [selected]="!filters.step">All steps</option>
              @for (name of stepNames(); track name) {
                <option [value]="name" [selected]="filters.step === name">
                  {{ name }}
                </option>
              }
            </select>
          </div>
        }
      </div>
      @if (store.refreshError(); as problem) {
        <div class="notice logs-notice" data-tone="danger" role="alert">
          <p>Studio could not refresh the logs. {{ problem.message }}</p>
          <button type="button" class="sm" (click)="store.refresh()">
            Try again
          </button>
        </div>
      }
      @switch (s.kind) {
        @case ("unserved") {
          <div class="logs-empty">
            <span class="badge"><weave-icon name="info" [size]="20" /></span>
            <p class="empty-title">This platform can't show logs yet.</p>
          </div>
        }
        @case ("refused") {
          <p class="logs-access">
            <weave-icon name="lock" [size]="16" /><span
              >You need <code>run.read</code> in {{ environmentName() }}. Ask an
              administrator in Settings › People and access.</span
            >
          </p>
        }
        @case ("withheld") {
          <p class="logs-access">
            <weave-icon name="lock" [size]="16" /><span
              >This run's details are withheld.</span
            >
          </p>
        }
        @case ("slow") {
          <div class="notice logs-notice" role="alert">
            <p><weave-icon name="stale" [size]="16" />{{ failureMessage() }}</p>
            <button type="button" class="sm" (click)="retry()">
              Try again
            </button>
          </div>
        }
        @case ("error") {
          <div class="notice logs-notice" data-tone="danger" role="alert">
            <p>Studio could not load the logs. {{ failureMessage() }}</p>
            @if (failureCode()) {
              <small class="support-code"
                >Support code: {{ failureCode() }}</small
              >
            }
            <button type="button" class="sm" (click)="retry()">
              Try again
            </button>
          </div>
        }
        @case ("ready") {
          @if (rows().length) {
            <weave-log-entries
              [rows]="rows()"
              (openRun)="host().viewRun({ id: $event })"
            />
            <div class="logs-footer">
              @if (more()) {
                <button
                  type="button"
                  [attr.aria-disabled]="store.appending() ? 'true' : null"
                  (click)="store.loadMore()"
                >
                  Load more entries
                </button>
              }
              <p class="hint">{{ count() }}</p>
            </div>
          } @else {
            <div class="logs-empty">
              <span class="badge"><weave-icon name="logs" [size]="20" /></span>
              <p class="empty-title">No log entries match.</p>
              <p>
                Workers write entries while steps run; entries are kept for 7
                days.
              </p>
            </div>
          }
        }
        @default {
          <div class="entries loading" role="status">
            <span class="sr-only">Loading log entries…</span>
            @for (row of [0, 1, 2, 3]; track row) {
              <div class="entry" aria-hidden="true">
                <span class="bar"></span><span class="bar"></span
                ><span class="bar mid"></span><span class="bar mid"></span
                ><span class="bar wide msg"></span>
              </div>
            }
          </div>
        }
      }
    </section>`,
  styles: `
    .run-logs {
      display: grid;
      gap: var(--group-gap);
      min-width: 0;
      margin: var(--section-gap) 0;
      container: run-logs / inline-size;
    }
    .run-logs-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2) var(--space-4);
    }
    .run-logs-head h3 {
      flex: 1 1 auto;
      margin: 0;
      font: var(--type-title-sm);
      line-height: var(--target);
    }
    .inline-field {
      display: flex;
      align-items: center;
      gap: var(--space-2);
    }
    .inline-field label {
      font: var(--type-label);
      white-space: nowrap;
    }
    .inline-field select {
      min-width: calc(var(--space-10) * 4);
      min-height: var(--target);
    }
    @container run-logs (max-width: 479px) {
      .run-logs-head h3 {
        flex-basis: 100%;
      }
    }
  `,
})
export class RunLogs implements DoCheck, OnDestroy {
  readonly host = input.required<App>();
  readonly runId = input.required<string>();
  /** The run's step instances as Steps lists them, for the Step filter. */
  readonly steps = input<readonly StepFact[]>([]);
  /** The run is still going: its entries are read again every 5 s. */
  readonly live = input(false);
  readonly levels = levelChoices;
  readonly store = new RunLogsStore({
    api: () => asRunViewsApi(this.host().api),
    scope: () => scopeKeyOf(this.host()),
  });
  private readonly poller = new Poller(() => this.poll(), LIVE_MS);
  private scope = "";
  private following = false;
  readonly rows = computed(() => {
    const state = this.store.state();
    return state.kind === "ready" ? state.rows : [];
  });
  readonly more = computed(() => {
    const state = this.store.state();
    return state.kind === "ready" && state.more;
  });
  readonly count = computed(() =>
    runCountText(this.rows().length, this.more()),
  );
  /** The steps that ran, then any other step an entry or the filter names. */
  readonly stepNames = computed(() => {
    const names = new Set(this.steps().map((item) => item.node_id));
    for (const row of this.rows())
      if (row.kind === "entry" && row.entry.node_id)
        names.add(row.entry.node_id);
    const chosen = this.store.filters().step;
    if (chosen) names.add(chosen);
    return [...names];
  });

  ngDoCheck() {
    const host = this.host();
    const scope = scopeKeyOf(host);
    if (scope !== this.scope) {
      // Another platform, environment or person: nothing read before stays.
      this.scope = scope;
      this.store.forget();
    }
    const id = this.runId();
    if (id && environmentOf(host) && id !== this.store.run)
      void this.store.open(id);
    const live = this.live();
    if (live && !this.following) {
      this.following = true;
      this.poller.start(false);
    } else if (!live && this.following) {
      // The run ended: its last entries are read once more.
      this.following = false;
      this.poller.stop();
      void this.store.refresh();
    }
  }
  ngOnDestroy() {
    this.poller.stop();
    this.store.forget();
  }
  private async poll(): Promise<boolean> {
    const result = await this.store.refresh();
    if (result === "failed") throw new Error("The logs could not be read.");
    return result === "applied";
  }
  setLevel(level: string) {
    void this.store.setFilters({
      ...this.store.filters(),
      level: level as LogsLevel,
    });
  }
  setStep(step: string) {
    void this.store.setFilters({ ...this.store.filters(), step });
  }
  retry() {
    void this.store.setFilters(this.store.filters());
  }
  environmentName() {
    return this.host().workspaceNames?.environment || "this environment";
  }
  failureMessage() {
    const state = this.store.state();
    return state.kind === "slow" || state.kind === "error"
      ? state.error.message
      : "";
  }
  failureCode() {
    const state = this.store.state();
    return state.kind === "error" ? state.error.code : "";
  }
  value(event: Event) {
    return (event.target as HTMLSelectElement).value;
  }
}
