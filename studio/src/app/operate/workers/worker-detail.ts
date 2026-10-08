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
// One worker: where its contact and load stand, and Drain or Resume. The
// server checks worker.drain and the revision on every command.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnChanges,
  OnDestroy,
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { toneAttribute } from "../../status-labels";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import {
  claimsLabel,
  workerBadges,
  workerLoad,
  workerPresence,
  type WorkerStatus,
} from "./workers-model";

@Component({
  selector: "weave-worker-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  styleUrls: ["../operate.css", "./workers.css"],
  template: `@let w = worker();
    @let presence = presenceOf(w);
    <header class="detail-header">
      <h2 id="worker-detail-title" tabindex="-1">Worker {{ short(w.id) }}</h2>
      <button
        type="button"
        class="icon-button"
        aria-label="Close detail"
        (click)="closed.emit()"
      >
        <weave-icon name="close" />
      </button>
    </header>
    <p class="badges">
      @for (badge of badges(w, presence); track badge.label) {
        <span class="status-pill" [attr.data-tone]="tone(badge.tone)">{{
          badge.label
        }}</span>
      }
    </p>
    <p class="hint">
      Registration is not health. Contact and load are the platform's
      observation; they don't prove the container is ready.
    </p>
    <dl class="facts">
      <dt>New tasks</dt>
      <dd>{{ claims(w) }}</dd>
      <dt>Last contact</dt>
      <dd>
        @if (w.last_seen_at) {
          <time
            [attr.datetime]="iso(w.last_seen_at)"
            [attr.title]="absolute(w.last_seen_at)"
            >{{ relative(w.last_seen_at) }}</time
          >
        } @else {
          Never
        }
      </dd>
      <dt>Active tasks</dt>
      <dd class="numeric">{{ load(w) }}</dd>
      <dt>Free task slots</dt>
      <dd class="numeric">
        {{
          presence === "online"
            ? (w.available_capacity ?? "Unknown")
            : "Unknown"
        }}
      </dd>
      <dt>Task types</dt>
      <dd>{{ w.task_types.join(", ") || "None" }}</dd>
      <dt>Observed</dt>
      <dd>
        <time [attr.datetime]="iso(w.observed_at)">{{
          absolute(w.observed_at) || "Unknown"
        }}</time>
      </dd>
    </dl>
    <p class="hint">
      Draining stops new tasks. Running tasks finish or expire; draining doesn't
      stop the container or cancel them.
    </p>
    @if (problem; as failed) {
      <div class="notice" data-tone="danger" role="alert">
        <p>{{ failed.message }}</p>
        @if (failed.code) {
          <small class="support-code">Support code: {{ failed.code }}</small>
        }
        @if (failed.code === "WV-WORKER-REVISION") {
          <button type="button" (click)="refresh.emit()">Refresh worker</button>
        }
      </div>
    }
    @if (canControl(w)) {
      <div class="action-row">
        @if (w.draining) {
          <button type="button" [disabled]="busy" (click)="control('resume')">
            <weave-icon name="play" [size]="16" />Resume
          </button>
        } @else {
          <button type="button" [disabled]="busy" (click)="control('drain')">
            <weave-icon name="drain" [size]="16" />Drain
          </button>
        }
      </div>
    }
    <details class="disclosure">
      <summary>Technical details</summary>
      <dl class="facts">
        <dt>Worker ID</dt>
        <dd>
          <code class="mono-id">{{ w.id }}</code>
        </dd>
        <dt>Release ID</dt>
        <dd>
          <code class="mono-id">{{ w.release_id }}</code>
        </dd>
        <dt>Revision</dt>
        <dd>{{ w.revision }}</dd>
      </dl>
    </details>`,
})
export class WorkerDetail implements OnChanges, OnDestroy {
  host = input.required<App>();
  worker = input.required<WorkerStatus>();
  /** The worker after a command, read again from the platform. */
  changed = output<WorkerStatus>();
  refresh = output<void>();
  closed = output<void>();
  problem: PlainError | null = null;
  private cdr = inject(ChangeDetectorRef);
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly relative = timeRelative;
  readonly badges = workerBadges;
  readonly tone = toneAttribute;
  /** The workers whose command is on its way; one worker's never blocks another's. */
  private running = new Set<string>();
  private alive = true;

  /** Whether the worker on screen has a command on its way. */
  get busy() {
    return this.running.has(this.worker().id);
  }
  ngOnDestroy() {
    this.alive = false;
  }

  ngOnChanges(changes: SimpleChanges) {
    // Another worker, or the same one read again at a new revision, leaves
    // nothing for an earlier problem to explain.
    const before = changes["worker"]?.previousValue;
    const after = changes["worker"]?.currentValue;
    if (before?.id !== after?.id || before?.revision !== after?.revision)
      this.problem = null;
  }
  presenceOf(worker: WorkerStatus) {
    return workerPresence(worker);
  }
  claims(worker: WorkerStatus) {
    return claimsLabel(worker);
  }
  load(worker: WorkerStatus) {
    const load = workerLoad(worker);
    return load.capacity === null
      ? load.text
      : `${load.text} task slots in use`;
  }
  canControl(worker: WorkerStatus) {
    const host = this.host();
    return (
      !host.signInEnded &&
      !worker.revoked &&
      Number.isInteger(worker.revision) &&
      host.can("worker.drain", worker.id)
    );
  }
  /** The environment commands go to; empty when there is none. */
  private environment() {
    try {
      return this.host().api.environment;
    } catch {
      return "";
    }
  }
  /**
   * Whether a command's answer still belongs on screen: the same worker is
   * open, in the same environment, for the same person.
   */
  private current(worker: WorkerStatus, scope: string, identity: string) {
    return (
      this.alive &&
      this.worker().id === worker.id &&
      this.environment() === scope &&
      JSON.stringify(this.host().identity) === identity
    );
  }
  private explain(error: unknown): PlainError {
    const plain = describeError(error);
    return plain.code === "WV-WORKER-REVISION"
      ? {
          ...plain,
          message:
            "This worker changed since you opened it. Refresh it, then try again.",
        }
      : plain;
  }
  async control(action: "drain" | "resume") {
    const host = this.host();
    const worker = this.worker();
    if (this.running.has(worker.id) || !this.canControl(worker)) return;
    const scope = this.environment();
    const identity = JSON.stringify(host.identity);
    if (action === "drain") {
      const tasks = worker.active_leases ?? 0;
      const confirmed = await host.dialogs.confirm({
        title: "Drain this worker?",
        message: `It finishes its ${tasks} active ${tasks === 1 ? "task" : "tasks"} and takes no new ones. You can resume it at any time.`,
        confirmLabel: "Drain",
      });
      // Another worker may be on screen by now: never act on one the person
      // is no longer looking at.
      if (!confirmed || !this.current(worker, scope, identity)) return;
    }
    this.running.add(worker.id);
    this.problem = null;
    this.cdr.markForCheck();
    const named = `Worker ${shortId(worker.id)}`;
    try {
      const path = `${scope}/workers/${encodeURIComponent(worker.id)}`;
      await host.api.request(
        `${path}/${action}`,
        "POST",
        { expected_revision: worker.revision },
        { "Idempotency-Key": crypto.randomUUID() },
      );
      // A replayed command answers with its first result: read the worker now.
      const read = await host.api.request<WorkerStatus>(path);
      if (this.current(worker, scope, identity)) this.changed.emit(read);
      host.notify(
        action === "drain"
          ? `${named} is draining.`
          : `${named} takes new tasks again.`,
      );
    } catch (error) {
      const plain = this.explain(error);
      // An answer for a worker that is no longer open never becomes this
      // detail's problem; a short notice says what happened to it.
      if (this.current(worker, scope, identity)) this.problem = plain;
      else
        host.notify(
          `${named} was not ${action === "drain" ? "drained" : "resumed"}. ${plain.message}`,
        );
    } finally {
      this.running.delete(worker.id);
      if (this.alive) this.cdr.markForCheck();
    }
  }
}
