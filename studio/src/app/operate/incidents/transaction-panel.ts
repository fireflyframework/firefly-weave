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
// The drawer of an incident that holds a transaction: why it is in doubt, the
// related incidents and which to resolve first, its facts with the business
// key, and the record of its steps, one row per step at every drawer width.
// The panel draws what the page gives it; the page reads related incidents.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { TRANSACTION_CODE_TEXT } from "../../editor/transactions/transaction-words";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import {
  statusLabel,
  statusTone,
  toneAttribute,
  type Tone,
} from "../../status-labels";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { HELD_RUN } from "../transactions/transaction-contracts";
import {
  recordBadge,
  recordRows,
  type RecordRow,
} from "../transactions/transaction-record";
import { TransactionRecordTable } from "../transactions/transaction-record-table";
import { linkPlace } from "./incident-cases";
import {
  incidentStatusLabel,
  incidentTone,
  openedText,
  transactionStep,
  type IncidentLink,
  type IncidentView,
} from "./incidents-model";

/** A related incident on its way from incidents.read, or why it isn't open. */
export interface PartnerRead {
  /** The incident being read. */
  id: string;
  reading: boolean;
  /** Why the last read failed, also while it is read again. */
  problem: {
    message: string;
    code: string;
    /** It reads again by itself: Try now reads at once. */
    again: boolean;
  } | null;
}

interface PanelLink {
  link: IncidentLink;
  /** "add in run 9a01c7d2". */
  place: string;
  status: string;
  tone: Tone;
}
interface PanelLinks {
  title: "Waits on" | "Held by";
  hint: string | null;
  /** More called runs than it names wait, or their links are withheld. */
  more: string | null;
  items: PanelLink[];
}
export interface PanelView {
  /** Why it is in doubt; null once it is decided. */
  sentence: string | null;
  links: PanelLinks[];
  businessKey: string;
  /** The transaction's record; null when the incident names no steps. */
  record: {
    title: string;
    badge: { text: string; tone: Tone };
    rows: RecordRow[];
  } | null;
  /** What stands in for the record of a run held whole, while it is. */
  held: string | null;
}

/** What stands in for the record of a run held whole. */
const HELD_TEXT =
  "Its transactions are held as they are. Open the run to see each one.";
const MORE_TEXT = "More called runs wait in doubt. Open the run to see them.";

const panelLink = (link: IncidentLink): PanelLink => ({
  link,
  place: linkPlace(link),
  status: incidentStatusLabel(link.status),
  tone: incidentTone(link.status),
});

/** What the panel shows; null for an incident with no transaction. */
export function panelView(incident: IncidentView): PanelView | null {
  if (incident.priority !== "high") return null;
  const links: PanelLinks[] = [];
  const waitsOn = (incident.waits_on ?? []).map(panelLink);
  if (waitsOn.length || incident.waits_on_more)
    links.push({
      title: "Waits on",
      hint: waitsOn.length
        ? "Resolve these first: this run waits for them."
        : null,
      more: incident.waits_on_more ? MORE_TEXT : null,
      items: waitsOn,
    });
  if (incident.held_by)
    links.push({
      title: "Held by",
      hint: "That run waits for this one: resolve this incident first.",
      more: null,
      items: [panelLink(incident.held_by)],
    });
  const transaction = incident.transaction?.participants.length
    ? incident.transaction
    : null;
  const active = incident.status === "active";
  return {
    sentence: active ? (TRANSACTION_CODE_TEXT[incident.code] ?? null) : null,
    links,
    businessKey: incident.transaction?.business_key ?? "None",
    record: transaction && {
      title: transaction.instance_key,
      badge: recordBadge(transaction),
      rows: recordRows(transaction, incident.run_id),
    },
    held:
      active && !transaction && incident.code === HELD_RUN ? HELD_TEXT : null,
  };
}

@Component({
  selector: "weave-transaction-panel",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, TransactionRecordTable],
  styleUrl: "./transaction-panel.css",
  template: `@let i = incident();
    @if (view(); as v) {
      <div class="txn-panel">
        @if (v.sentence) {
          <p class="notice" data-tone="danger">
            <weave-icon name="failCircle" [size]="16" />{{ v.sentence }}
          </p>
        }
        @for (section of v.links; track section.title) {
          @let slug =
            section.title === "Waits on" ? "txn-waits-on" : "txn-held-by";
          <section class="txn-links" [attr.aria-labelledby]="slug">
            <h3 [id]="slug">{{ section.title }}</h3>
            @for (item of section.items; track item.link.id) {
              @let read = partnerOf(item.link);
              <div class="txn-link">
                <span class="link-words"
                  ><strong>{{ item.link.code }}</strong
                  ><span>{{ item.place }}</span></span
                ><span class="link-open"
                  ><span
                    class="status-pill"
                    [attr.data-tone]="tone(item.tone)"
                    >{{ item.status }}</span
                  ><button
                    type="button"
                    class="sm"
                    [attr.data-incident]="item.link.id"
                    [attr.aria-label]="
                      'Open incident ' + item.link.code + ', ' + item.place
                    "
                    [attr.aria-busy]="read?.reading ? 'true' : null"
                    [attr.aria-disabled]="read?.reading ? 'true' : null"
                    (click)="openLink.emit(item.link)"
                  >
                    @if (read?.reading) {
                      <weave-icon name="spinner" [size]="16" class="spin" />
                    }
                    Open
                  </button></span
                >
              </div>
              @if (read?.problem; as problem) {
                <div class="notice" role="alert">
                  <weave-icon name="warning" [size]="16" />
                  <div>
                    <p>{{ problem.message }}</p>
                    @if (problem.code) {
                      <p class="support-code">
                        Support code: {{ problem.code }}
                      </p>
                    }
                    @if (problem.again) {
                      <button
                        type="button"
                        class="sm"
                        [attr.aria-busy]="read?.reading ? 'true' : null"
                        [attr.aria-disabled]="read?.reading ? 'true' : null"
                        (click)="tryNow.emit()"
                      >
                        Try now
                      </button>
                    }
                  </div>
                </div>
              }
            }
            @if (section.more) {
              <p class="hint">{{ section.more }}</p>
            }
            @if (section.hint) {
              <p class="hint">{{ section.hint }}</p>
            }
          </section>
        }
        <dl class="txn-facts">
          <dt>Opened</dt>
          <dd class="opened">
            @if (i.opened_at && relative(i.opened_at)) {
              <time
                [attr.datetime]="iso(i.opened_at)"
                [attr.title]="absolute(i.opened_at)"
                >{{ opened() }}</time
              >
            } @else {
              Unknown
            }
          </dd>
          <dt>Workflow</dt>
          <dd>
            {{
              i.workflow
                ? i.workflow.name + " " + i.workflow.version
                : "Unknown"
            }}
          </dd>
          <dt>Step</dt>
          <dd>{{ step() }}</dd>
          <dt>Business key</dt>
          <dd class="business-key">{{ v.businessKey }}</dd>
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
        @if (v.record; as record) {
          <section class="txn-steps" aria-labelledby="txn-record-title">
            <div class="record-title">
              <h3 id="txn-record-title">
                Transaction <code>{{ record.title }}</code>
              </h3>
              <span
                class="status-pill"
                [attr.data-tone]="tone(record.badge.tone)"
                >{{ record.badge.text }}</span
              >
            </div>
            <weave-transaction-record
              layout="rows"
              [rows]="record.rows"
              [transaction]="record.title"
              (openRun)="openRun.emit($event)"
              (copyKey)="copyKey.emit($event)"
            />
          </section>
        } @else if (v.held) {
          <p class="hint">{{ v.held }}</p>
        }
      </div>
    }`,
})
export class TransactionPanel {
  incident = input.required<IncidentView>();
  /** The related incident being read, if any. */
  partner = input<PartnerRead | null>(null);
  openLink = output<IncidentLink>();
  /** Reads the related incident again without waiting. */
  tryNow = output<void>();
  /** The incident's run, or a called step's. */
  openRun = output<string>();
  copyKey = output<string>();
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly view = computed(() => panelView(this.incident()));
  partnerOf(link: IncidentLink): PartnerRead | null {
    const read = this.partner();
    return read?.id === link.id ? read : null;
  }
  step() {
    return transactionStep(this.incident()) ?? "Not tied to a step";
  }
  tone(tone: Tone) {
    return toneAttribute(tone);
  }
  relative(value: string) {
    return timeRelative(value, "");
  }
  /** When it opened, with an estimate marked as one. */
  opened() {
    return openedText(this.incident(), (value) => this.relative(value));
  }
  runLabel(status: string) {
    return statusLabel("run", status);
  }
  runTone(status: string) {
    return toneAttribute(statusTone("run", status));
  }
}
