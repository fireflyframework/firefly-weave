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
// Step logs under Output in run mode: folded until opened, then the step's
// log entries in the run, read once (50 at most), each with its time, level,
// message, code and how many values the platform hid. A platform that
// doesn't serve logs yet says so once, and Studio asks it no more.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  signal,
  untracked,
} from "@angular/core";
import { ApiError } from "../../../api";
import { describeError } from "../../../errors";
import { Icon } from "../../../icon";
import {
  listRunLogs,
  type RunLogEntry,
  type RunViewsApi,
} from "../../../operate/run-contracts";

/** Platforms (account and environment) that answered they don't serve logs. */
const unserved = new Set<string>();
const LEVEL_TONES: Readonly<Record<string, string | null>> = {
  error: "danger",
  warning: "warning",
  info: null,
  debug: null,
};

type LogsState =
  | { kind: "folded" }
  | { kind: "loading" }
  | { kind: "ready"; entries: readonly RunLogEntry[] }
  | { kind: "unserved" }
  | { kind: "refused" }
  | { kind: "error"; message: string };

@Component({
  selector: "weave-run-step-logs",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  template: `@let s = state();
    <section class="run-logs">
      <button
        type="button"
        class="run-logs-toggle"
        [attr.aria-expanded]="s.kind !== 'folded'"
        (click)="toggle()"
      >
        <weave-icon
          name="chevron"
          [size]="16"
          [class.turned]="s.kind !== 'folded'"
        />Step logs
      </button>
      @switch (s.kind) {
        @case ("loading") {
          <p class="run-logs-note" role="status">Reading the step's logs…</p>
        }
        @case ("unserved") {
          <p class="run-logs-note">
            Logs aren't available from this platform yet.
          </p>
        }
        @case ("refused") {
          <p class="run-logs-note">
            You need run.read here to read this step's logs.
          </p>
        }
        @case ("error") {
          <p class="run-logs-note">
            {{ s.message }}
            <button type="button" class="sm" (click)="load()">Try again</button>
          </p>
        }
        @case ("ready") {
          @let entries = shown();
          @if (!entries.length) {
            <p class="run-logs-note">This step wrote no logs in this run.</p>
          }
          @for (entry of entries; track entry.id) {
            <div class="run-log-row">
              <span class="run-log-time" [attr.title]="entry.at">{{
                time(entry.at)
              }}</span>
              <span
                class="run-log-level"
                [attr.data-tone]="tone(entry.level)"
                >{{ entry.level }}</span
              >
              <span class="run-log-message"
                >{{ entry.message }}
                @if (entry.code) {
                  <code>{{ entry.code }}</code>
                }
                @if (entry.redactions) {
                  <span class="run-log-hidden"
                    ><weave-icon name="lock" [size]="16" />{{
                      entry.redactions === 1
                        ? "1 value hidden"
                        : entry.redactions + " values hidden"
                    }}</span
                  >
                }
              </span>
            </div>
          }
        }
      }
    </section>`,
  styles: `
    .run-logs {
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      overflow: hidden;
      margin-top: var(--space-3);
    }
    .run-logs-toggle {
      width: 100%;
      justify-content: flex-start;
      min-height: 40px;
      border: 0;
      border-radius: 0;
      background: var(--sunken);
      font: var(--type-label);
    }
    .run-logs-toggle weave-icon {
      color: var(--muted);
      transition: transform var(--dur-fast) var(--ease);
    }
    .run-logs-toggle weave-icon.turned {
      transform: rotate(90deg);
    }
    .run-logs-note {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      margin: 0;
      padding: var(--space-3);
      border-top: 1px solid var(--line);
      font: var(--type-small);
      color: var(--muted);
    }
    .run-log-row {
      display: grid;
      grid-template-columns: 64px 60px minmax(0, 1fr);
      column-gap: var(--space-2);
      align-items: start;
      padding: 6px var(--space-3);
      border-top: 1px solid var(--line);
      font: var(--type-small);
    }
    .run-log-time {
      font: var(--type-mono);
      color: var(--subtle);
      line-height: 20px;
    }
    .run-log-level {
      font: var(--type-caption);
      line-height: 20px;
      color: var(--muted);
    }
    .run-log-level[data-tone="danger"] {
      color: var(--danger-ink);
    }
    .run-log-level[data-tone="warning"] {
      color: var(--warning-ink);
    }
    .run-log-message {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .run-log-message code {
      margin-left: 4px;
      font: var(--type-mono);
      color: var(--muted);
    }
    .run-log-hidden {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      margin-left: 6px;
      font: var(--type-caption);
      color: var(--muted);
    }
    @media (prefers-reduced-motion: reduce) {
      .run-logs-toggle weave-icon {
        transition: none;
      }
    }
  `,
})
export class RunStepLogs {
  readonly runId = input.required<string>();
  readonly stepId = input.required<string>();
  /** The iteration shown, when the step ran several times. */
  readonly iterationKey = input<string | null>(null);
  readonly api = input.required<RunViewsApi>();
  /** The account and environment the logs are read for. */
  readonly scope = input.required<string>();
  readonly state = signal<LogsState>({ kind: "folded" });
  private asked: { run: string; step: string; scope: string } | null = null;
  readonly shown = computed(() => {
    const s = this.state();
    if (s.kind !== "ready") return [];
    const key = this.iterationKey();
    return key
      ? s.entries.filter((entry) => entry.instance_key === key)
      : s.entries;
  });

  constructor() {
    // Another step or run: its logs fold, and an answer still out is dropped.
    effect(() => {
      this.runId();
      this.stepId();
      this.scope();
      untracked(() => {
        this.asked = null;
        this.state.set({ kind: "folded" });
      });
    });
  }
  toggle(): void {
    if (this.state().kind !== "folded")
      return this.state.set({ kind: "folded" });
    void this.load();
  }
  async load(): Promise<void> {
    const ticket = {
      run: this.runId(),
      step: this.stepId(),
      scope: this.scope(),
    };
    if (unserved.has(ticket.scope)) return this.state.set({ kind: "unserved" });
    this.asked = ticket;
    this.state.set({ kind: "loading" });
    try {
      const page = await listRunLogs(this.api(), ticket.run, {
        node_id: ticket.step,
        limit: 50,
      });
      if (this.asked === ticket)
        this.state.set({ kind: "ready", entries: page.items });
    } catch (error) {
      if (this.asked !== ticket) return;
      const status = error instanceof ApiError ? error.status : 0;
      if (status === 501) {
        unserved.add(ticket.scope);
        return this.state.set({ kind: "unserved" });
      }
      if (status === 403) return this.state.set({ kind: "refused" });
      this.state.set({ kind: "error", message: describeError(error).message });
    }
  }
  time(at: string): string {
    const date = new Date(at);
    return Number.isNaN(date.getTime())
      ? at
      : date.toLocaleTimeString(undefined, { hour12: false });
  }
  tone(level: string): string | null {
    return LEVEL_TONES[level] ?? null;
  }
}
