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
// A run's Transactions section: a summary, then each transaction with its
// pill, business key, cause, record, timeline and resolution. Completed
// transactions nobody had to resolve fold to one line. It reads
// runs.transactions 50 at a time, every 2 s while the run goes on; a failed
// read waits 5 s, doubling to 60 s. An answer is applied only while the same
// run, environment and person are on screen.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  ElementRef,
  OnDestroy,
  inject,
  input,
  output,
} from "@angular/core";
import { Router } from "@angular/router";
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { toneAttribute, type Tone } from "../../status-labels";
import { OperateState } from "../operate-state";
import { openIncident } from "./open-incident";
import {
  readRunTransactions,
  type RunTransactionView,
} from "./transaction-contracts";
import {
  causeText,
  clockTime,
  foldedLine,
  recordBadge,
  recordRows,
  recordTimeline,
  resolutionText,
  retryDelay,
  sectionSummary,
  type RecordRow,
} from "./transaction-record";
import {
  TransactionRecordTable,
  copyOperationKey,
} from "./transaction-record-table";

/** How often the section reads again while the run goes on. */
const LIVE_MS = 2_000;
/** A first read slower than this shows the loading lines. */
const SLOW_MS = 300;

type Phase = "idle" | "loading" | "ready" | "failed" | "forbidden";
/** What one read is for: answers for anything else are dropped. */
interface Scope {
  key: string;
  runId: string;
  environment: string;
  generation: number;
}

@Component({
  selector: "weave-run-transactions",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, OperateState, TransactionRecordTable],
  styleUrl: "./run-transactions.css",
  template: `@if (shown()) {
    <section class="run-transactions" aria-labelledby="run-transactions-title">
      <div class="txn-heading">
        <h3 id="run-transactions-title">Transactions</h3>
        @if (views.length) {
          @if (next) {
            <p class="txn-summary">
              The first {{ views.length }} of this run's transactions
            </p>
          } @else {
            @let summary = summaryParts();
            <p class="txn-summary">
              {{ summary.lead }}:
              @for (part of summary.parts; track $index) {
                @if (part.inDoubt) {
                  <button
                    type="button"
                    class="text-link"
                    (click)="focusInDoubt()"
                  >
                    {{ part.text }}</button
                  >{{ $last ? "" : "," }}
                } @else {
                  {{ part.text }}{{ $last ? "" : "," }}
                }
              }
            </p>
          }
        }
      </div>
      @if (phase === "forbidden") {
        <p class="txn-access">
          You need <code>run.read</code> in this environment. Ask an
          administrator in Settings › People and access.
        </p>
      } @else if (phase === "loading" && !views.length) {
        <weave-operate-state kind="loading" noun="transactions" [rows]="4" />
      }
      @if (phase === "failed" && problem; as error) {
        <div class="notice txn-problem" role="alert">
          <weave-icon name="warning" [size]="16" />
          <div>
            <p>
              Studio couldn't read this run's transactions. It tries again in
              {{ waitSeconds() }} s.
            </p>
            @if (error.code) {
              <p class="support-code">Support code: {{ error.code }}</p>
            }
            <button type="button" class="sm" (click)="tryNow()">Try now</button>
          </div>
        </div>
      }
      @for (view of views; track view.instance_key) {
        @let folded = isFolded(view);
        @let badge = badgeOf(view);
        <article
          class="txn-block"
          [class.in-doubt]="view.state === 'in_doubt'"
          [class.folded]="folded"
          [attr.aria-labelledby]="headingId(view)"
        >
          <div class="txn-title">
            <button
              type="button"
              class="icon-button txn-chevron"
              [attr.aria-expanded]="!folded"
              [attr.aria-controls]="folded ? null : bodyId(view)"
              [attr.aria-label]="
                (folded ? 'Show' : 'Hide') + ' transaction ' + view.instance_key
              "
              (click)="toggle(view)"
            >
              <weave-icon
                name="chevron"
                [size]="16"
                class="disclosure"
                [class.open]="!folded"
              />
            </button>
            <h4 [id]="headingId(view)" tabindex="-1">
              Transaction <code>{{ view.instance_key }}</code>
            </h4>
            <span class="status-pill" [attr.data-tone]="tone(badge.tone)">{{
              badge.text
            }}</span>
            @if (folded) {
              <p class="txn-folded">
                @for (part of foldedOf(view); track $index) {
                  <span>{{ part }}</span>
                }
              </p>
            }
            @if (view.state === "in_doubt" && view.incident_id) {
              <button
                type="button"
                class="sm txn-open-incident"
                (click)="openIncident(view.incident_id)"
              >
                <weave-icon name="incidents" [size]="16" />Open incident
              </button>
            }
          </div>
          @if (!folded) {
            <div class="txn-body" [id]="bodyId(view)">
              <p class="txn-meta">
                @if (view.business_key) {
                  <span
                    >Business key <strong>{{ view.business_key }}</strong></span
                  >
                }
                <span
                  >Opened at
                  <time [attr.datetime]="view.opened_at">{{
                    clock(view.opened_at)
                  }}</time></span
                >
              </p>
              @if (causeOf(view); as cause) {
                <p class="txn-cause">
                  <weave-icon
                    [name]="view.state === 'in_doubt' ? 'failCircle' : 'undo'"
                    [size]="16"
                    [attr.data-tone]="
                      view.state === 'in_doubt' ? 'danger' : 'warning'
                    "
                  />{{ cause }}
                </p>
              }
              <weave-transaction-record
                [rows]="rowsOf(view)"
                [transaction]="view.instance_key"
                (openRun)="openRun($event)"
                (copyKey)="copy($event)"
              />
              <ol class="txn-timeline">
                @for (moment of timelineOf(view); track $index) {
                  <li>
                    <time [attr.datetime]="moment.at">{{
                      clock(moment.at)
                    }}</time>
                    <span>{{ moment.text }}</span>
                  </li>
                }
              </ol>
              @if (view.resolution; as resolution) {
                <dl class="txn-resolution">
                  <dt>Resolved</dt>
                  <dd>{{ resolvedAs(view) }}</dd>
                  <dt>By</dt>
                  <dd>
                    @if (resolution.workflow_run_id; as resolver) {
                      <button
                        type="button"
                        class="text-link"
                        (click)="openRun(resolver)"
                      >
                        Resolved by workflow run {{ short(resolver) }}
                      </button>
                    } @else if (resolution.actor_id; as actor) {
                      @if (isViewer(actor)) {
                        You
                      } @else {
                        <code>{{ short(actor) }}</code>
                      }
                    } @else {
                      Weave
                    }
                    @if (resolution.resolved_at; as at) {
                      · <time [attr.datetime]="at">{{ clock(at) }}</time>
                    }
                  </dd>
                  <dt>Reason</dt>
                  <dd>{{ resolution.reason }}</dd>
                  @if (resolution.evidence_reference; as evidence) {
                    <dt>Evidence</dt>
                    <dd>{{ evidence }}</dd>
                  }
                </dl>
              }
            </div>
          }
        </article>
      }
      @if (next) {
        <button
          type="button"
          class="load-more"
          [attr.aria-disabled]="appending ? 'true' : null"
          (click)="showMore()"
        >
          Show more transactions
        </button>
      }
    </section>
  }`,
})
export class RunTransactions implements DoCheck, OnDestroy {
  host = input.required<App>();
  runId = input.required<string>();
  /** The run goes on: its transactions are read again every 2 s. */
  live = input(false);
  /**
   * The run's transactions each time a read lands, for the run's actions,
   * and whether they are all of them: no page is left to read.
   */
  loaded = output<{
    runId: string;
    views: RunTransactionView[];
    complete: boolean;
  }>();
  /** A read failed or was refused and no transaction of the run is listed. */
  unread = output<string>();
  private cdr = inject(ChangeDetectorRef);
  private element = inject(ElementRef<HTMLElement>);
  private router = inject(Router);
  readonly short = shortId;

  views: RunTransactionView[] = [];
  next: string | null = null;
  phase: Phase = "idle";
  problem: PlainError | null = null;
  appending = false;
  /** The first read took long enough to show its loading lines. */
  private slow = false;
  private failures = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private slowTimer: ReturnType<typeof setTimeout> | null = null;
  private scope: Scope | null = null;
  private generation = 0;
  private alive = true;
  /** Transactions the person opened or folded, by instance key. */
  private opened = new Map<string, boolean>();
  /** Derived words, kept while the record they come from is the same. */
  private derived = new WeakMap<RunTransactionView, Derived>();

  ngDoCheck() {
    const key = this.scopeKey();
    if (key !== this.scope?.key) this.restart(key);
  }
  ngOnDestroy() {
    this.alive = false;
    this.clearTimers();
  }

  // ---------------------------------------------------------------- reads

  private environment() {
    try {
      return this.host().api.environment;
    } catch {
      return "";
    }
  }
  private scopeKey() {
    return JSON.stringify([
      this.runId(),
      this.environment(),
      this.host().identity?.principal_id ?? null,
    ]);
  }
  /** Another run, environment or person: start again from nothing. */
  private restart(key: string) {
    this.clearTimers();
    this.views = [];
    this.next = null;
    this.problem = null;
    this.failures = 0;
    this.appending = false;
    this.morePages = false;
    this.slow = false;
    this.opened = new Map();
    const runId = this.runId();
    const environment = this.environment();
    this.scope = { key, runId, environment, generation: ++this.generation };
    if (!runId || !environment) {
      this.phase = "idle";
      return;
    }
    this.phase = "loading";
    this.slowTimer = setTimeout(() => {
      this.slow = true;
      this.changed();
    }, SLOW_MS);
    void this.read();
  }
  private clearTimers() {
    if (this.timer !== null) clearTimeout(this.timer);
    if (this.slowTimer !== null) clearTimeout(this.slowTimer);
    this.timer = this.slowTimer = null;
  }
  private current(scope: Scope) {
    return (
      this.alive &&
      this.scope === scope &&
      scope.generation === this.generation &&
      this.scopeKey() === scope.key
    );
  }
  /** Reads the first page again: the person's own pages stop the 2 s reads. */
  private async read() {
    const scope = this.scope;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (!scope) return;
    try {
      const page = await readRunTransactions(
        this.host().api,
        scope.environment,
        scope.runId,
        null,
      );
      if (!this.current(scope) || this.appending) return;
      this.views = page.views;
      this.next = page.next;
      this.phase = "ready";
      this.problem = null;
      this.failures = 0;
      this.emitLoaded(scope);
    } catch (error) {
      if (!this.current(scope)) return;
      const plain = describeError(error);
      if (plain.status === 403) {
        this.phase = "forbidden";
        this.views = [];
        this.next = null;
        this.unread.emit(scope.runId);
        this.changed();
        return;
      }
      this.failures++;
      this.problem = plain;
      this.phase = "failed";
      if (!this.views.length) this.unread.emit(scope.runId);
    }
    this.schedule(scope);
    this.changed();
  }
  private schedule(scope: Scope) {
    if (!this.current(scope)) return;
    const delay = this.failures
      ? retryDelay(this.failures)
      : this.live() && !this.morePages
        ? LIVE_MS
        : null;
    if (delay === null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.current(scope)) void this.read();
    }, delay);
  }
  /** Pages the person added with Show more transactions. */
  private morePages = false;
  tryNow() {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    void this.read();
  }
  async showMore() {
    const scope = this.scope;
    const cursor = this.next;
    if (!scope || !cursor || this.appending) return;
    this.appending = true;
    this.morePages = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.changed();
    try {
      const page = await readRunTransactions(
        this.host().api,
        scope.environment,
        scope.runId,
        cursor,
      );
      if (!this.current(scope)) return;
      const known = new Set(this.views.map((view) => view.instance_key));
      this.views = [
        ...this.views,
        ...page.views.filter((view) => !known.has(view.instance_key)),
      ];
      this.next = page.next;
      this.emitLoaded(scope);
    } catch (error) {
      if (!this.current(scope)) return;
      this.host().notify(
        `Studio couldn't read more transactions. ${describeError(error).message}`,
      );
    } finally {
      if (this.current(scope)) {
        this.appending = false;
        this.changed();
      }
    }
  }
  private emitLoaded(scope: Scope) {
    this.loaded.emit({
      runId: scope.runId,
      views: this.views,
      complete: this.next === null,
    });
  }
  private changed() {
    if (this.alive) this.cdr.markForCheck();
  }

  // ---------------------------------------------------------------- view

  shown() {
    return (
      this.views.length > 0 ||
      this.phase === "forbidden" ||
      this.phase === "failed" ||
      (this.phase === "loading" && this.slow)
    );
  }
  waitSeconds() {
    return retryDelay(this.failures) / 1000;
  }
  summaryParts() {
    const { text } = sectionSummary(this.views);
    const [lead, rest = ""] = text.split(": ");
    return {
      lead,
      parts: rest
        .split(", ")
        .map((part) => ({ text: part, inDoubt: part.endsWith(" in doubt") })),
    };
  }
  focusInDoubt() {
    const first = sectionSummary(this.views).firstInDoubt;
    const view = this.views.find((item) => item.instance_key === first);
    if (!view) return;
    const heading = (
      this.element.nativeElement as HTMLElement
    ).querySelector<HTMLElement>(`#${CSS.escape(this.headingId(view))}`);
    heading?.scrollIntoView({ block: "start" });
    heading?.focus({ preventScroll: true });
  }
  isFolded(view: RunTransactionView) {
    const chosen = this.opened.get(view.instance_key);
    if (chosen !== undefined) return !chosen;
    return this.of(view).folded !== null;
  }
  toggle(view: RunTransactionView) {
    this.opened.set(view.instance_key, this.isFolded(view));
    this.changed();
  }
  headingId(view: RunTransactionView) {
    return `txn-${this.slug(view.instance_key)}`;
  }
  bodyId(view: RunTransactionView) {
    return `txn-body-${this.slug(view.instance_key)}`;
  }
  private slug(key: string) {
    return key.replace(/[^A-Za-z0-9_-]/g, (char) => `_${char.charCodeAt(0)}`);
  }
  private of(view: RunTransactionView): Derived {
    let derived = this.derived.get(view);
    if (!derived) {
      derived = {
        folded: foldedLine(view),
        badge: recordBadge(view),
        cause: causeText(view),
        rows: recordRows(view, this.runId()),
        timeline: recordTimeline(view),
        resolution: view.resolution
          ? resolutionText(view.resolution, view.participants)
          : "",
      };
      this.derived.set(view, derived);
    }
    return derived;
  }
  foldedOf(view: RunTransactionView) {
    return this.of(view).folded ?? [];
  }
  badgeOf(view: RunTransactionView) {
    return this.of(view).badge;
  }
  causeOf(view: RunTransactionView) {
    return this.of(view).cause;
  }
  rowsOf(view: RunTransactionView) {
    return this.of(view).rows;
  }
  timelineOf(view: RunTransactionView) {
    return this.of(view).timeline;
  }
  resolvedAs(view: RunTransactionView) {
    return this.of(view).resolution;
  }
  tone(tone: Tone) {
    return toneAttribute(tone);
  }
  clock(value: string) {
    return clockTime(value);
  }
  isViewer(actor: string) {
    return actor === this.host().identity?.principal_id;
  }
  openIncident(id: string) {
    void openIncident(this.host(), this.router, id);
  }
  openRun(id: string) {
    void this.host().viewRun({ id });
  }
  copy(key: string) {
    void copyOperationKey(this.host(), key);
  }
}

interface Derived {
  folded: string[] | null;
  badge: { text: string; tone: Tone };
  cause: string | null;
  rows: RecordRow[];
  timeline: { at: string; text: string }[];
  resolution: string;
}
