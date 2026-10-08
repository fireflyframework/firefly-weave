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
// One incident: what happened, the run's last engine events (types only),
// how it was resolved, and Resolve incident while it is active.
import { Component, input, output } from "@angular/core";
import type { App } from "../../app";
import type { PlainError } from "../../errors";
import { absoluteTime, isoTime, relativeTime, shortId } from "../../format";
import { Icon } from "../../icon";
import { toneAttribute } from "../../status-labels";
import { OperateState } from "../operate-state";
import {
  incidentStatusLabel,
  incidentTone,
  resolutionLabel,
  type EventLine,
  type IncidentView,
} from "./incidents-model";

/** The run's last events, or why they are missing. */
export interface RecentEvents {
  loading: boolean;
  lines: EventLine[];
  /** The run has more events than Studio read. */
  truncated: boolean;
  problem: PlainError | null;
}

@Component({
  selector: "weave-incident-drawer",
  standalone: true,
  imports: [Icon, OperateState],
  styleUrls: ["../operate.css", "./incidents.css"],
  template: `@let i = incident();
    <header class="detail-header">
      <h2 id="incident-detail-title" tabindex="-1">Incident {{ i.code }}</h2>
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
      <span class="status-pill" [attr.data-tone]="tone(i)">{{ label(i) }}</span>
    </p>
    @if (i.external_effects_may_continue) {
      <p class="notice" data-tone="warning">
        <weave-icon name="warning" [size]="16" />Effects may continue: ending
        the run doesn't undo work already sent to other systems.
      </p>
    }
    <dl class="facts">
      <dt>Code</dt>
      <dd>
        <code>{{ i.code }}</code>
      </dd>
      @if (i.origin_code !== i.code) {
        <dt>First reported as</dt>
        <dd>
          <code>{{ i.origin_code }}</code>
        </dd>
      }
      <dt>Step</dt>
      <dd>{{ i.node_id || "Not tied to a step" }}</dd>
      @if (i.generation !== null) {
        <dt>Attempt</dt>
        <dd>{{ i.generation }}</dd>
      }
      <dt>Run</dt>
      <dd>
        <button type="button" class="tertiary sm" (click)="openRun.emit()">
          Run {{ short(i.run_id) }}
        </button>
      </dd>
    </dl>
    @if (i.resolution; as resolution) {
      <h3>Resolution</h3>
      <dl class="facts">
        <dt>Decision</dt>
        <dd>{{ decision(resolution.kind) }}</dd>
        <dt>Reason</dt>
        <dd>{{ resolution.reason }}</dd>
        @if (resolution.evidence_reference) {
          <dt>Evidence</dt>
          <dd>{{ resolution.evidence_reference }}</dd>
        }
        @if (i.resolved_at) {
          <dt>Resolved</dt>
          <dd>
            <time
              [attr.datetime]="iso(i.resolved_at)"
              [attr.title]="absolute(i.resolved_at)"
              >{{ relative(i.resolved_at) }}</time
            >
          </dd>
        }
      </dl>
    }
    @if (i.status === "active" && canResolve()) {
      <div class="action-row">
        <button type="button" class="primary" (click)="resolve.emit()">
          Resolve incident
        </button>
      </div>
    }
    <section class="recent-events" aria-labelledby="incident-events-title">
      <h3 id="incident-events-title">Last engine events</h3>
      @let recent = events();
      @if (recent.problem) {
        @if (recent.problem.status === 403) {
          <weave-operate-state
            kind="access"
            heading="Run events need run access"
            capability="run.read"
          />
        } @else {
          <weave-operate-state
            kind="partial"
            [message]="recent.problem.message"
            [code]="recent.problem.code"
            (retry)="reloadEvents.emit()"
          />
        }
      } @else if (recent.loading) {
        <weave-operate-state kind="loading" noun="events" [rows]="3" />
      } @else if (!recent.lines.length) {
        <p class="hint">The run has no recorded events yet.</p>
      } @else {
        <ol class="event-lines">
          @for (line of recent.lines; track line.sequence) {
            <li>
              <span class="numeric">#{{ line.sequence }}</span
              ><code>{{ line.type }}</code>
            </li>
          }
        </ol>
        @if (recent.truncated) {
          <p class="hint">
            Studio read the run's first 1,000 events; later events are not
            shown.
          </p>
        }
      }
    </section>
    <details class="disclosure">
      <summary>Technical details</summary>
      <dl class="facts">
        <dt>Incident ID</dt>
        <dd>
          <code class="mono-id">{{ i.id }}</code>
        </dd>
        <dt>Incident key</dt>
        <dd>
          <code class="mono-id">{{ i.incident_key }}</code>
        </dd>
        <dt>Revision</dt>
        <dd>{{ i.revision }}</dd>
      </dl>
    </details>`,
})
export class IncidentDrawer {
  host = input.required<App>();
  incident = input.required<IncidentView>();
  events = input.required<RecentEvents>();
  resolve = output<void>();
  openRun = output<void>();
  reloadEvents = output<void>();
  closed = output<void>();
  readonly short = shortId;
  canResolve() {
    return this.host().can("incident.resolve", this.incident().id);
  }
  label(incident: IncidentView) {
    return incidentStatusLabel(incident.status);
  }
  tone(incident: IncidentView) {
    return toneAttribute(incidentTone(incident.status));
  }
  decision(kind: Parameters<typeof resolutionLabel>[0]) {
    return resolutionLabel(kind);
  }
  iso(value: string) {
    return isoTime(value) || null;
  }
  absolute(value: string) {
    return absoluteTime(value);
  }
  relative(value: string) {
    return relativeTime(value) || absoluteTime(value);
  }
}
