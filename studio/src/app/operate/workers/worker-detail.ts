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
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { absoluteTime, isoTime, relativeTime, shortId } from "../../format";
import { Icon } from "../../icon";
import { toneAttribute } from "../../status-labels";
import {
  claimsLabel,
  presenceLabel,
  presenceTone,
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
      @if (w.revoked) {
        <span class="status-pill">Revoked</span>
      } @else {
        <span class="status-pill" [attr.data-tone]="tone(presence)">{{
          label(presence)
        }}</span>
        @if (w.draining) {
          <span class="status-pill" data-tone="warning">Draining</span>
        }
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
export class WorkerDetail implements OnChanges {
  host = input.required<App>();
  worker = input.required<WorkerStatus>();
  /** The worker after a command, read again from the platform. */
  changed = output<WorkerStatus>();
  refresh = output<void>();
  closed = output<void>();
  busy = false;
  problem: PlainError | null = null;
  private cdr = inject(ChangeDetectorRef);
  readonly short = shortId;

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
  label(presence: ReturnType<typeof workerPresence>) {
    return presenceLabel(presence);
  }
  tone(presence: ReturnType<typeof workerPresence>) {
    return toneAttribute(presenceTone(presence));
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
  iso(value: string) {
    return isoTime(value) || null;
  }
  absolute(value: string) {
    return absoluteTime(value);
  }
  relative(value: string) {
    return relativeTime(value) || "Unknown";
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
  async control(action: "drain" | "resume") {
    const host = this.host();
    const worker = this.worker();
    if (this.busy || !this.canControl(worker)) return;
    if (action === "drain") {
      const tasks = worker.active_leases ?? 0;
      const confirmed = await host.dialogs.confirm({
        title: "Drain this worker?",
        message: `It finishes its ${tasks} active ${tasks === 1 ? "task" : "tasks"} and takes no new ones. You can resume it at any time.`,
        confirmLabel: "Drain",
      });
      if (!confirmed) return;
    }
    this.busy = true;
    this.problem = null;
    this.cdr.markForCheck();
    try {
      const scope = host.api.environment;
      const path = `${scope}/workers/${encodeURIComponent(worker.id)}`;
      await host.api.request(
        `${path}/${action}`,
        "POST",
        { expected_revision: worker.revision },
        { "Idempotency-Key": crypto.randomUUID() },
      );
      // A replayed command answers with its first result: read the worker now.
      this.changed.emit(await host.api.request<WorkerStatus>(path));
      host.notify(
        action === "drain"
          ? `Worker ${shortId(worker.id)} is draining.`
          : `Worker ${shortId(worker.id)} takes new tasks again.`,
      );
    } catch (error) {
      const plain = describeError(error);
      this.problem =
        plain.code === "WV-WORKER-REVISION"
          ? {
              ...plain,
              message:
                "This worker changed since you opened it. Refresh it, then try again.",
            }
          : plain;
    } finally {
      this.busy = false;
      this.cdr.markForCheck();
    }
  }
}
