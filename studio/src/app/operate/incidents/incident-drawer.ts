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
// One incident: what happened (its step and full step instance, the run and
// its status, when it opened, marked when that time is an estimate), or for
// one that holds a transaction its record and the related incidents; the
// run's last engine events (types only, never their data), how it was
// resolved, and while it is active Resolve incident, or the decisions that fit
// the transaction in doubt it holds. A time the platform doesn't know reads
// Unknown.
import { Component, input, output } from "@angular/core";
import type { App } from "../../app";
import type { PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { statusLabel, statusTone, toneAttribute } from "../../status-labels";
import { OperateState } from "../operate-state";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { copyOperationKey } from "../transactions/transaction-record-table";
import {
  incidentStatusLabel,
  incidentTone,
  openedText,
  type EventLine,
  type IncidentLink,
  type IncidentView,
} from "./incidents-model";
import { TransactionPanel, type PartnerRead } from "./transaction-panel";
import {
  decisionText,
  transactionActions,
  type TransactionAction,
} from "./transaction-resolutions";

/** A decision whose incident is read again before its dialog opens, or why it couldn't be. */
export interface OfferRead {
  action: TransactionAction;
  reading: boolean;
  problem: { message: string; code: string } | null;
}
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
  imports: [Icon, OperateState, TransactionPanel],
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
    <p class="badges" [class.over-panel]="i.priority === 'high'">
      <span class="status-pill" [attr.data-tone]="tone(i)">{{ label(i) }}</span>
      @if (i.test) {
        <span class="tag">Test run</span>
      }
    </p>
    @if (i.external_effects_may_continue) {
      <p class="notice" data-tone="warning">
        <weave-icon name="warning" [size]="16" />Effects may continue: ending
        the run doesn't undo work already sent to other systems.
      </p>
    }
    @if (i.priority === "high") {
      <weave-transaction-panel
        [incident]="i"
        [partner]="partner()"
        (openLink)="openLink.emit($event)"
        (tryNow)="tryPartner.emit()"
        (openRun)="openRun.emit($event)"
        (copyKey)="copy($event)"
      />
    } @else {
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
        <dt>Opened</dt>
        <dd class="opened">
          @if (i.opened_at && relative(i.opened_at)) {
            <time
              [attr.datetime]="iso(i.opened_at)"
              [attr.title]="absolute(i.opened_at)"
              >{{ opened(i) }}</time
            >
            @if (i.opened_at_estimated) {
              <small class="hint"
                >From the run's last recorded event: the incident's own opening
                wasn't kept.</small
              >
            }
          } @else {
            Unknown
          }
        </dd>
        @if (i.updated_at && relative(i.updated_at)) {
          <dt>Last changed</dt>
          <dd>
            <time
              [attr.datetime]="iso(i.updated_at)"
              [attr.title]="absolute(i.updated_at)"
              >{{ relative(i.updated_at) }}</time
            >
          </dd>
        }
        <dt>Workflow</dt>
        <dd>
          {{
            i.workflow ? i.workflow.name + " " + i.workflow.version : "Unknown"
          }}
        </dd>
        <dt>Step</dt>
        <dd>{{ i.node_id || "Not tied to a step" }}</dd>
        @if (i.instance_key && i.instance_key !== i.node_id) {
          <dt>Step instance</dt>
          <dd>
            <code class="mono-id">{{ i.instance_key }}</code>
          </dd>
        }
        @if (i.generation !== null) {
          <dt>Attempt</dt>
          <dd>{{ i.generation }}</dd>
        }
        <dt>Run</dt>
        <dd class="run-fact">
          <button
            type="button"
            class="tertiary sm"
            (click)="openRun.emit(i.run_id)"
          >
            Run {{ short(i.run_id) }}
          </button>
          @if (i.run_status) {
            <span
              class="status-pill"
              [attr.data-tone]="runTone(i.run_status)"
              >{{ runLabel(i.run_status) }}</span
            >
          }
        </dd>
      </dl>
    }
    @if (i.resolution; as resolution) {
      <h3>Resolution</h3>
      <dl class="facts">
        <dt>Decision</dt>
        <dd>{{ decision(i) }}</dd>
        @if (resolution.workflow_run_id; as resolver) {
          <dt>By</dt>
          <dd>
            <button
              type="button"
              class="text-link"
              (click)="openRun.emit(resolver)"
            >
              Resolved by workflow run {{ short(resolver) }}
            </button>
          </dd>
        }
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
    @if (offers().length) {
      <section class="offers" aria-labelledby="incident-offers-title">
        <h3 id="incident-offers-title">Resolve this incident</h3>
        @for (item of offers(); track item.action) {
          @let read = readOf(item.action);
          <div class="offer">
            <button
              type="button"
              [class.danger]="item.action === 'force_end'"
              [attr.data-offer]="item.action"
              [attr.aria-busy]="read?.reading ? 'true' : null"
              [attr.aria-describedby]="'incident-offer-' + item.action"
              [disabled]="!!item.missing"
              (click)="decide.emit(item.action)"
            >
              @if (read?.reading) {
                <weave-icon name="spinner" [size]="16" class="spin" />Reading
                the incident…
              } @else {
                {{ item.label }}
              }
            </button>
            <div [id]="'incident-offer-' + item.action">
              <p>{{ item.help }}</p>
              @if (item.missing) {
                <p class="why">
                  <weave-icon name="lock" [size]="16" />{{ item.missing }}
                </p>
              }
            </div>
            @if (read?.problem; as problem) {
              <div class="notice" role="alert">
                <weave-icon name="warning" [size]="16" />
                <div>
                  <p>{{ problem.message }}</p>
                  @if (problem.code) {
                    <p class="support-code">Support code: {{ problem.code }}</p>
                  }
                </div>
              </div>
            }
          </div>
        }
      </section>
    } @else if (i.status === "active" && canResolve()) {
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
        @if (i.priority === "high") {
          @if (i.origin_code !== i.code) {
            <dt>First reported as</dt>
            <dd>
              <code>{{ i.origin_code }}</code>
            </dd>
          }
          @if (i.instance_key && i.instance_key !== i.node_id) {
            <dt>Step instance</dt>
            <dd>
              <code class="mono-id">{{ i.instance_key }}</code>
            </dd>
          }
          @if (i.generation !== null) {
            <dt>Attempt</dt>
            <dd>{{ i.generation }}</dd>
          }
          @if (i.updated_at && relative(i.updated_at)) {
            <dt>Last changed</dt>
            <dd>
              <time
                [attr.datetime]="iso(i.updated_at)"
                [attr.title]="absolute(i.updated_at)"
                >{{ relative(i.updated_at) }}</time
              >
            </dd>
          }
        }
        <dt>Revision</dt>
        <dd>{{ i.revision }}</dd>
      </dl>
    </details>`,
})
export class IncidentDrawer {
  host = input.required<App>();
  incident = input.required<IncidentView>();
  events = input.required<RecentEvents>();
  /** The related incident being read for this one, if any. */
  partner = input<PartnerRead | null>(null);
  /** The decision whose incident is being read before its dialog opens, if any. */
  offerRead = input<OfferRead | null>(null);
  resolve = output<void>();
  /** A decision on the transaction in doubt the incident holds. */
  decide = output<TransactionAction>();
  /** A run to open: the incident's, a called step's or a resolving workflow's. */
  openRun = output<string>();
  /** A related incident to open. */
  openLink = output<IncidentLink>();
  /** Reads the related incident again without waiting. */
  tryPartner = output<void>();
  reloadEvents = output<void>();
  closed = output<void>();
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  canResolve() {
    return this.host().can("incident.resolve", this.incident().id);
  }
  /** The platform asks for each capability across the environment: a grant for this incident alone isn't enough. */
  offers() {
    return transactionActions(this.incident(), (capability) =>
      this.host().can(capability),
    );
  }
  readOf(action: TransactionAction): OfferRead | null {
    const read = this.offerRead();
    return read?.action === action ? read : null;
  }
  label(incident: IncidentView) {
    return incidentStatusLabel(incident.status);
  }
  tone(incident: IncidentView) {
    return toneAttribute(incidentTone(incident.status));
  }
  decision(incident: IncidentView) {
    return decisionText(incident);
  }
  copy(key: string) {
    void copyOperationKey(this.host(), key);
  }
  /** A time that isn't a date shows nothing; the template says Unknown. */
  relative(value: string) {
    return timeRelative(value, "");
  }
  /** When it opened, with an estimate marked as one. */
  opened(incident: IncidentView) {
    return openedText(incident, (value) => this.relative(value));
  }
  runLabel(status: string) {
    return statusLabel("run", status);
  }
  runTone(status: string) {
    return toneAttribute(statusTone("run", status));
  }
}
