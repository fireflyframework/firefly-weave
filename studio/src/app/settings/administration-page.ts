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
// Settings › Administration: the platform's version, build and uptime, its
// compatibility with Run check, who may receive alert mail (allowed domains
// and outside recipients' opt-ins), where approval notifications go,
// retention plans, telemetry export, and how long operations records are
// kept. Each section asks for its own capability and shows what it needs
// when the person lacks it; the others stay usable. Every read and change
// lands only in the environment and for the person it was made for.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  ElementRef,
  Injector,
  OnDestroy,
  type WritableSignal,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { ApiError } from "../api";
import type { App } from "../app";
import { Modal } from "../dialog";
import { describeError, type PlainError } from "../errors";
import { shortId } from "../format";
import { Icon } from "../icon";
import {
  listChannels,
  listConsents,
  readAlertSettings,
  recordConsent,
  revokeConsent,
  saveAlertSettings,
  type AlertChannel,
  type AlertSettings,
  type AlertsTransport,
  type RecipientConsent,
} from "../operate/alerts/alerts-api";
import { recipientProblem, startedText } from "../operate/alerts/alerts-model";
import { environmentOf, scopeKeyOf } from "../operate/operate-list";
import {
  applyRetentionPlan,
  makeRetentionPlan,
  readCompatibilityReport,
  readRetentionPeriods,
  readSummaryPlatform,
  readTelemetrySettings,
  runCompatibilityCheck,
  type Compatibility,
  type Period,
  type PlatformFacts,
  type RetentionPlan,
  type Telemetry,
} from "./administration-api";
import {
  buildText,
  compatibilityText,
  domainProblem,
  findingText,
  periodRows,
  planWords,
  telemetryText,
  uptimeText,
} from "./administration-model";

type Section<T> =
  | { kind: "loading" }
  | { kind: "ready"; value: T }
  | { kind: "error"; error: PlainError }
  | { kind: "access"; capability: string };
interface Alerting {
  settings: AlertSettings;
  consents: RecipientConsent[];
  channels: AlertChannel[];
}
const loading = { kind: "loading" } as const;
const STATEMENT_LIMIT = 200;
const mailbox = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

@Component({
  selector: "weave-administration-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Modal, NgTemplateOutlet],
  styleUrl: "./administration.css",
  template: `<p class="intro">
      The platform at <code>{{ platformHost() }}</code
      >, environment {{ environment() }}. Each section asks for its own access.
    </p>
    <div class="cards">
      <section class="card" aria-labelledby="admin-platform">
        <div class="ch"><h2 id="admin-platform">Platform</h2></div>
        @let p = platform();
        @switch (p.kind) {
          @case ("ready") {
            <dl>
              <dt>Version</dt>
              <dd>{{ p.value.version }}</dd>
              <dt>Build</dt>
              <dd class="mono" [title]="p.value.build ?? ''">
                {{ build(p.value) }}
              </dd>
              <dt>API uptime</dt>
              <dd>{{ uptime(p.value) }}</dd>
            </dl>
          }
          @default {
            <ng-container
              *ngTemplateOutlet="state; context: { $implicit: p }"
            />
          }
        }
      </section>

      <section class="card" aria-labelledby="admin-compatibility">
        @let c = compatibility();
        <div class="ch">
          <h2 id="admin-compatibility">Compatibility</h2>
          @if (c.kind === "ready") {
            <span
              class="pill"
              [attr.data-tone]="compatibilityWords(c.value).tone"
              >{{ compatibilityWords(c.value).text }}</span
            >
          }
        </div>
        @switch (c.kind) {
          @case ("ready") {
            <dl>
              <dt>Checked</dt>
              <dd>{{ checkedText(c.value) }}</dd>
            </dl>
            @if (c.value.findings.length) {
              <ul class="findings">
                @for (finding of c.value.findings.slice(0, 10); track $index) {
                  <li>{{ findingWords(finding) }}</li>
                }
              </ul>
            }
            @if (can("compatibility.check")) {
              <div class="acts">
                <button
                  type="button"
                  [attr.aria-disabled]="checking() ? 'true' : null"
                  (click)="runCheck()"
                >
                  {{ checking() ? "Checking…" : "Run check" }}
                </button>
              </div>
            } @else {
              <p class="access">
                <weave-icon name="lock" [size]="16" /><span
                  >Running a check needs <code>compatibility.check</code> in
                  {{ environment() }}. Ask an administrator in Settings › People
                  and access.</span
                >
              </p>
            }
            <p class="hint">
              Run check reads runs, activations, workers and connections and
              lists what this version can't serve. It changes nothing.
            </p>
            <p class="hint" role="status">{{ checkNote() }}</p>
            @if (checkError()) {
              <p class="problem" role="alert">
                <weave-icon name="failCircle" [size]="16" />{{ checkError() }}
              </p>
            }
          }
          @default {
            <ng-container
              *ngTemplateOutlet="state; context: { $implicit: c }"
            />
          }
        }
      </section>

      <section class="card" aria-labelledby="admin-alert-email">
        <div class="ch"><h2 id="admin-alert-email">Alert email</h2></div>
        @let a = alerting();
        @switch (a.kind) {
          @case ("ready") {
            <div class="field" role="group" aria-labelledby="admin-domains">
              <span class="field-label" id="admin-domains"
                >Allowed recipient domains</span
              >
              <div class="chips">
                @for (
                  domain of a.value.settings.recipient_domains;
                  track domain
                ) {
                  <span class="chip"
                    ><span>{{ domain }}</span>
                    @if (canChange()) {
                      <button
                        type="button"
                        [attr.aria-label]="'Remove ' + domain"
                        [attr.aria-disabled]="saving() ? 'true' : null"
                        (click)="removeDomain(domain)"
                      >
                        <weave-icon name="close" [size]="16" />
                      </button>
                    }
                  </span>
                } @empty {
                  <span class="muted">No domains are allowed yet.</span>
                }
                @if (canChange() && domainDraft() === null) {
                  <button type="button" class="sm" (click)="openDomain()">
                    <weave-icon name="plus" [size]="16" />Add domain
                  </button>
                }
              </div>
              @if (domainDraft() !== null) {
                <div class="add-domain">
                  <input
                    id="admin-domain"
                    aria-label="Domain to allow"
                    placeholder="example.org"
                    autocomplete="off"
                    spellcheck="false"
                    [value]="domainDraft()"
                    [attr.aria-invalid]="domainError() ? 'true' : null"
                    [attr.aria-describedby]="
                      domainError() ? 'admin-domain-problem' : null
                    "
                    (input)="
                      domainDraft.set(value($event)); domainError.set('')
                    "
                    (keydown.enter)="addDomain()"
                    (keydown.escape)="domainDraft.set(null)"
                  />
                  <button
                    type="button"
                    class="primary"
                    [attr.aria-disabled]="saving() ? 'true' : null"
                    (click)="addDomain()"
                  >
                    Allow
                  </button>
                  <button
                    type="button"
                    class="tertiary"
                    (click)="domainDraft.set(null); domainError.set('')"
                  >
                    Cancel
                  </button>
                </div>
                @if (domainError()) {
                  <p class="problem" id="admin-domain-problem">
                    <weave-icon name="failCircle" [size]="16" />{{
                      domainError()
                    }}
                  </p>
                }
              }
              <p class="hint">
                Outside addresses must use one of these domains and have an
                opt-in. Project members don't need either.
              </p>
            </div>
            <div class="field" role="group" aria-labelledby="admin-optins">
              <span class="field-label" id="admin-optins">Opt-ins</span>
              @if (activeConsents().length) {
                <div class="mini optins" role="table" aria-label="Opt-ins">
                  <div class="h" role="row">
                    <span role="columnheader">Address</span
                    ><span role="columnheader">How they agreed</span
                    ><span role="columnheader">Recorded</span
                    ><span role="columnheader"
                      ><span class="sr-only">Actions</span></span
                    >
                  </div>
                  @for (consent of activeConsents(); track consent.id) {
                    <div role="row">
                      <span role="cell"
                        ><span class="nb">{{ consent.address }}</span></span
                      >
                      <span role="cell" class="muted">{{
                        consent.statement
                      }}</span>
                      @let when = recorded(consent);
                      <span role="cell" class="muted"
                        ><span class="nb">{{ when.who }}</span
                        ><span class="day">{{ when.day }}</span></span
                      >
                      <span role="cell">
                        @if (canChange()) {
                          <button
                            type="button"
                            class="sm"
                            [id]="'revoke-' + consent.id"
                            [attr.aria-label]="'Revoke ' + consent.address"
                            (click)="askRevoke(consent)"
                          >
                            Revoke
                          </button>
                        }
                      </span>
                    </div>
                  }
                </div>
              } @else {
                <p class="hint">
                  No outside address has an opt-in in {{ environment() }}.
                </p>
              }
              @if (revoking(); as consent) {
                <div
                  class="confirm"
                  role="alertdialog"
                  aria-labelledby="admin-revoke-text"
                  (keydown.escape)="cancelRevoke()"
                >
                  <p id="admin-revoke-text">
                    <weave-icon name="warning" [size]="16" /><span>{{
                      revokeText(consent)
                    }}</span>
                  </p>
                  <div class="acts">
                    <button
                      type="button"
                      class="sm"
                      id="admin-revoke-cancel"
                      (click)="cancelRevoke()"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      class="sm danger"
                      [attr.aria-disabled]="saving() ? 'true' : null"
                      (click)="revoke(consent)"
                    >
                      Revoke
                    </button>
                  </div>
                </div>
              }
              @if (canChange()) {
                <div class="acts">
                  <button type="button" class="sm" (click)="openRecord()">
                    <weave-icon name="plus" [size]="16" />Record an opt-in
                  </button>
                </div>
              }
            </div>
            @if (!canChange()) {
              <p class="access">
                <weave-icon name="lock" [size]="16" /><span
                  >Changing domains and opt-ins needs
                  <code>alert.manage</code> and
                  <code>environment.manage</code> in {{ environment() }}. Ask an
                  administrator in Settings › People and access.</span
                >
              </p>
            }
            <p class="hint" role="status">{{ alertingNote() }}</p>
            @if (alertingError()) {
              <p class="problem" role="alert">
                <weave-icon name="failCircle" [size]="16" />{{
                  alertingError()
                }}
              </p>
            }
          }
          @default {
            <ng-container
              *ngTemplateOutlet="state; context: { $implicit: a }"
            />
          }
        }
      </section>

      <section class="card" aria-labelledby="admin-approvals">
        <div class="ch">
          <h2 id="admin-approvals">Approval notifications</h2>
        </div>
        @if (a.kind === "ready") {
          @if (approvalsDraft(); as draft) {
            <fieldset class="field">
              <legend class="field-label">
                Mail change requests awaiting approval to
              </legend>
              <div class="checks">
                @for (channel of a.value.channels; track channel.id) {
                  <label class="checkbox-field"
                    ><input
                      type="checkbox"
                      [checked]="draft.includes(channel.id)"
                      (change)="toggleApproval(channel.id, checked($event))"
                    />{{ channel.name }}</label
                  >
                } @empty {
                  <p class="hint">
                    No email channels yet: add one in Operate › Alerts.
                  </p>
                }
              </div>
            </fieldset>
            <div class="acts">
              <button
                type="button"
                class="primary sm"
                [attr.aria-disabled]="saving() ? 'true' : null"
                (click)="saveApprovals()"
              >
                Save
              </button>
              <button
                type="button"
                class="tertiary sm"
                (click)="approvalsDraft.set(null)"
              >
                Cancel
              </button>
            </div>
          } @else {
            <dl>
              <dt>Change requests awaiting approval</dt>
              <dd>{{ approvalNames(a.value) }}</dd>
            </dl>
            @if (canChange()) {
              <div class="acts">
                <button
                  type="button"
                  class="sm"
                  aria-label="Edit approval notifications"
                  (click)="editApprovals(a.value)"
                >
                  Edit
                </button>
              </div>
            } @else {
              <p class="access">
                <weave-icon name="lock" [size]="16" /><span
                  >Changing approval notifications needs
                  <code>alert.manage</code> and
                  <code>environment.manage</code> in {{ environment() }}. Ask an
                  administrator in Settings › People and access.</span
                >
              </p>
            }
          }
          <p class="hint">
            A change request that needs approval mails these channels within 30
            seconds.
          </p>
        } @else {
          <ng-container *ngTemplateOutlet="state; context: { $implicit: a }" />
        }
      </section>

      <section class="card" aria-labelledby="admin-retention">
        <div class="ch">
          <h2 id="admin-retention">Retention</h2>
          @if (plan(); as current) {
            <span class="cap">{{ planText(current).expires }}</span>
          }
        </div>
        @if (!can("retention.plan")) {
          <p class="access">
            <weave-icon name="lock" [size]="16" /><span
              >You need <code>retention.plan</code> in {{ environment() }}. Ask
              an administrator in Settings › People and access.</span
            >
          </p>
        } @else if (plan(); as current) {
          @let words = planText(current);
          <dl>
            <dt>Can be deleted</dt>
            <dd>{{ words.deletableText }}</dd>
            @if (words.keptText) {
              <dt>Kept</dt>
              <dd>{{ words.keptText }}</dd>
            }
          </dl>
          @if (confirmApply()) {
            <div
              class="confirm"
              role="alertdialog"
              aria-labelledby="admin-apply-text"
              (keydown.escape)="cancelApply()"
            >
              <p id="admin-apply-text">
                <weave-icon name="warning" [size]="16" /><span>{{
                  words.confirm
                }}</span>
              </p>
              <div class="acts">
                <button
                  type="button"
                  class="sm"
                  id="admin-apply-cancel"
                  (click)="cancelApply()"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  class="sm danger"
                  [attr.aria-disabled]="applying() ? 'true' : null"
                  (click)="apply(current)"
                >
                  {{ words.apply }}
                </button>
              </div>
            </div>
          } @else {
            <div class="acts">
              @if (words.deletable && can("retention.apply")) {
                <button
                  type="button"
                  class="danger"
                  id="admin-apply"
                  (click)="askApply()"
                >
                  Apply
                </button>
              }
              <button type="button" class="tertiary" (click)="discardPlan()">
                Discard plan
              </button>
            </div>
            @if (words.deletable && !can("retention.apply")) {
              <p class="access">
                <weave-icon name="lock" [size]="16" /><span
                  >Applying a plan needs <code>retention.apply</code> in
                  {{ environment() }}.</span
                >
              </p>
            }
          }
        } @else {
          <p class="hint">
            A plan lists expired debug sessions that can be deleted. Nothing is
            deleted until you apply it.
          </p>
          <div class="acts">
            <button
              type="button"
              id="admin-make-plan"
              [attr.aria-disabled]="planning() ? 'true' : null"
              (click)="makePlan()"
            >
              Make a plan
            </button>
          </div>
        }
        <p class="done" role="status">
          @if (retentionNote()) {
            <weave-icon name="ok" [size]="16" />{{ retentionNote() }}
          }
        </p>
        @if (retentionError()) {
          <p class="problem" role="alert">
            <weave-icon name="failCircle" [size]="16" />{{ retentionError() }}
          </p>
        }
      </section>

      <section class="card" aria-labelledby="admin-telemetry">
        @let t = telemetry();
        <div class="ch">
          <h2 id="admin-telemetry">Telemetry export</h2>
          @if (t.kind === "ready") {
            <span class="pill">{{ t.value.enabled ? "On" : "Off" }}</span>
          }
        </div>
        @switch (t.kind) {
          @case ("ready") {
            <p class="hint">
              {{ telemetryWords(t.value) }} · configured with
              <code>WEAVE_TELEMETRY</code>. Studio only shows this setting; the
              platform's environment sets it.
            </p>
          }
          @default {
            <ng-container
              *ngTemplateOutlet="state; context: { $implicit: t }"
            />
          }
        }
      </section>

      <section class="card full" aria-labelledby="admin-periods">
        <div class="ch">
          <h2 id="admin-periods">Operations data retention</h2>
          <span class="cap"
            >How long {{ environment() }} keeps each record</span
          >
        </div>
        @let r = periods();
        @switch (r.kind) {
          @case ("ready") {
            <div
              class="mini periods"
              role="table"
              aria-label="Retention periods"
            >
              <div class="h" role="row">
                <span role="columnheader">Record</span
                ><span role="columnheader">Kept for</span>
              </div>
              @for (row of periodList(r.value); track row[0]) {
                <div role="row">
                  <span role="cell">{{ row[0] }}</span
                  ><span role="cell">{{ row[1] }}</span>
                </div>
              }
            </div>
          }
          @default {
            <ng-container
              *ngTemplateOutlet="state; context: { $implicit: r }"
            />
          }
        }
      </section>
    </div>

    @if (recordOpen()) {
      <weave-modal
        heading="Record an opt-in"
        closeLabel="Close"
        (dismiss)="closeRecord()"
      >
        <form class="form record" novalidate (submit)="record($event)">
          <div class="field">
            <label for="optin-address">Address</label>
            <input
              id="optin-address"
              type="email"
              autocomplete="off"
              spellcheck="false"
              [value]="recordAddress()"
              [attr.aria-invalid]="recordAddressError() ? 'true' : null"
              [attr.aria-describedby]="
                recordAddressError() ? 'optin-address-problem' : null
              "
              (input)="
                recordAddress.set(value($event)); recordAddressError.set('')
              "
            />
            @if (recordAddressError()) {
              <p class="problem" id="optin-address-problem">
                <weave-icon name="failCircle" [size]="16" />{{
                  recordAddressError()
                }}
              </p>
            }
          </div>
          <div class="field">
            <label for="optin-statement">How did they agree?</label>
            <textarea
              id="optin-statement"
              rows="3"
              [attr.maxlength]="statementLimit"
              aria-describedby="optin-statement-hint"
              [value]="recordStatement()"
              [attr.aria-invalid]="recordStatementError() ? 'true' : null"
              (input)="
                recordStatement.set(value($event)); recordStatementError.set('')
              "
            ></textarea>
            <p class="hint" id="optin-statement-hint">
              Up to 200 characters. It's kept with the opt-in and shown to
              administrators.
            </p>
            @if (recordStatementError()) {
              <p class="problem">
                <weave-icon name="failCircle" [size]="16" />{{
                  recordStatementError()
                }}
              </p>
            }
          </div>
          <p class="hint">
            Mail to this address can start at the next send. You can revoke the
            opt-in at any time, and mail stops at the next send.
          </p>
          @if (recordError()) {
            <p class="problem" role="alert">
              <weave-icon name="failCircle" [size]="16" />{{ recordError() }}
            </p>
          }
          <div class="dialog-actions">
            <button
              type="submit"
              class="primary"
              [attr.aria-disabled]="saving() ? 'true' : null"
            >
              Record opt-in
            </button>
            <button type="button" class="tertiary" (click)="closeRecord()">
              Cancel
            </button>
          </div>
        </form>
      </weave-modal>
    }

    <ng-template #state let-section>
      @switch (section.kind) {
        @case ("access") {
          <p class="access">
            <weave-icon name="lock" [size]="16" /><span
              >You need <code>{{ section.capability }}</code> in
              {{ environment() }}. Ask an administrator in Settings › People and
              access.</span
            >
          </p>
        }
        @case ("error") {
          <p class="problem" role="alert">
            <weave-icon name="failCircle" [size]="16" />{{
              section.error.code
                ? section.error.code + ": " + section.error.message
                : section.error.message
            }}
          </p>
          <div class="acts">
            <button type="button" class="sm" (click)="reload()">
              Try again
            </button>
          </div>
        }
        @default {
          <p class="hint" role="status">Loading…</p>
        }
      }
    </ng-template>`,
  styles: `
    .form {
      display: flex;
      flex-direction: column;
      gap: var(--space-4);
    }
    .record textarea {
      resize: vertical;
    }
  `,
})
export class AdministrationPage implements DoCheck, OnDestroy {
  host = input.required<App>();
  private element = inject(ElementRef<HTMLElement>);
  private injector = inject(Injector);
  readonly statementLimit = STATEMENT_LIMIT;
  readonly platform = signal<Section<PlatformFacts>>(loading);
  readonly compatibility = signal<Section<Compatibility>>(loading);
  readonly alerting = signal<Section<Alerting>>(loading);
  readonly telemetry = signal<Section<Telemetry>>(loading);
  readonly periods = signal<Section<Period[]>>(loading);
  readonly now = signal(Date.now());
  readonly checking = signal(false);
  readonly checkNote = signal("");
  readonly checkError = signal("");
  readonly plan = signal<RetentionPlan | null>(null);
  readonly planning = signal(false);
  readonly confirmApply = signal(false);
  /** Apply is on its way: a second click sends nothing. */
  readonly applying = signal(false);
  /**
   * A change to alert email or approvals is on its way: the next one waits,
   * so it goes out with the revision the first one answers.
   */
  readonly saving = signal(false);
  readonly retentionNote = signal("");
  readonly retentionError = signal("");
  readonly domainDraft = signal<string | null>(null);
  readonly domainError = signal("");
  readonly alertingNote = signal("");
  readonly alertingError = signal("");
  readonly revoking = signal<RecipientConsent | null>(null);
  readonly approvalsDraft = signal<string[] | null>(null);
  readonly recordOpen = signal(false);
  readonly recordAddress = signal("");
  readonly recordStatement = signal("");
  readonly recordAddressError = signal("");
  readonly recordStatementError = signal("");
  readonly recordError = signal("");
  private recordKey = "";
  private scopeKey = "";
  private readonly ticker = setInterval(() => this.now.set(Date.now()), 30_000);

  readonly activeConsents = computed(() => {
    const section = this.alerting();
    return section.kind === "ready"
      ? section.value.consents.filter((consent) => !consent.revoked_at)
      : [];
  });

  private get api(): AlertsTransport {
    return this.host().api;
  }
  can(capability: string) {
    return this.host().can(capability);
  }
  canChange() {
    return this.can("alert.manage") && this.can("environment.manage");
  }
  environment() {
    return this.host().workspaceNames?.environment || "this environment";
  }
  platformHost() {
    const base = this.host().profile?.baseUrl ?? "";
    try {
      return new URL(base).host || base;
    } catch {
      return base;
    }
  }

  ngDoCheck() {
    const host = this.host();
    const key = scopeKeyOf(host);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.reset();
    if (environmentOf(host) && host.identity) void this.reload();
  }
  ngOnDestroy() {
    clearInterval(this.ticker);
    this.scopeKey = "";
  }
  /** Another environment or person: what was read and typed for the last one goes. */
  private reset() {
    this.platform.set(loading);
    this.compatibility.set(loading);
    this.alerting.set(loading);
    this.telemetry.set(loading);
    this.periods.set(loading);
    this.plan.set(null);
    this.confirmApply.set(false);
    this.domainDraft.set(null);
    this.revoking.set(null);
    this.approvalsDraft.set(null);
    this.recordOpen.set(false);
    for (const note of [
      this.checkNote,
      this.checkError,
      this.retentionNote,
      this.retentionError,
      this.domainError,
      this.alertingNote,
      this.alertingError,
    ])
      note.set("");
  }

  /** A read or change whose answer lands only while the view is the same. */
  private async guarded<T>(
    run: () => Promise<T>,
  ): Promise<{ ok: true; value: T } | { ok: false; error: unknown } | null> {
    const key = this.scopeKey;
    try {
      const value = await run();
      return key === this.scopeKey ? { ok: true, value } : null;
    } catch (error) {
      if (key !== this.scopeKey) return null;
      const plain = describeError(error);
      if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
        this.host().fail(error);
      return { ok: false, error };
    }
  }
  private async section<T>(
    target: WritableSignal<Section<T>>,
    capability: string,
    read: () => Promise<T>,
  ) {
    if (!this.can(capability)) {
      target.set({ kind: "access", capability });
      return;
    }
    target.set(loading);
    const result = await this.guarded(read);
    if (!result) return;
    if (result.ok) target.set({ kind: "ready", value: result.value });
    else {
      const plain = describeError(result.error);
      target.set(
        plain.status === 403
          ? { kind: "access", capability }
          : { kind: "error", error: plain },
      );
    }
  }
  async reload() {
    const api = this.api;
    await Promise.all([
      this.section(this.platform, "status.read", () =>
        readSummaryPlatform(api),
      ),
      this.section(this.compatibility, "status.read", () =>
        readCompatibilityReport(api),
      ),
      this.section(this.alerting, "alert.read", async () => {
        const [settings, consents, channels] = await Promise.all([
          readAlertSettings(api),
          listConsents(api),
          listChannels(api),
        ]);
        return { settings, consents, channels };
      }),
      this.section(this.telemetry, "status.read", () =>
        readTelemetrySettings(api),
      ),
      this.section(this.periods, "status.read", () =>
        readRetentionPeriods(api),
      ),
    ]);
  }

  // ---------------------------------------------------------------- words

  build(platform: PlatformFacts) {
    return buildText(platform.build);
  }
  uptime(platform: PlatformFacts) {
    return uptimeText(platform, this.now());
  }
  compatibilityWords(report: Compatibility) {
    return compatibilityText(report);
  }
  checkedText(report: Compatibility) {
    if (!report.checkedAt) return "Not checked yet";
    return `${startedText(report.checkedAt, new Date(this.now()))}, ${report.inspected.toLocaleString("en-US")} records`;
  }
  findingWords(finding: Compatibility["findings"][number]) {
    return findingText(finding);
  }
  planText(plan: RetentionPlan) {
    return planWords(plan);
  }
  telemetryWords(telemetry: Telemetry) {
    return telemetryText(telemetry);
  }
  periodList(periods: Period[]) {
    return periodRows(periods);
  }
  person(id: string) {
    if (id === this.host().identity?.principal_id) return "You";
    const label = this.host().accountLabel(id);
    return label.startsWith("Account ") ? `Person ${shortId(id)}` : label;
  }
  recorded(consent: RecipientConsent) {
    const day = new Intl.DateTimeFormat("en-GB", {
      day: "numeric",
      month: "short",
    }).format(new Date(consent.created_at));
    return { who: this.person(consent.created_by), day };
  }
  /** What revoking stops, and which channels keep their other recipients. */
  revokeText(consent: RecipientConsent) {
    const section = this.alerting();
    const channels =
      section.kind === "ready"
        ? section.value.channels
            .filter((channel) =>
              channel.recipients.some(
                (item) => "address" in item && item.address === consent.address,
              ),
            )
            .map((channel) => channel.name)
        : [];
    const keep = channels.length
      ? `; ${channels.join(" and ")} ${channels.length === 1 ? "keeps its" : "keep their"} other recipients.`
      : ".";
    return `Revoke the opt-in for ${consent.address}? Mail to this address stops at the next send${keep}`;
  }
  approvalNames(alerting: Alerting) {
    const names = new Map(
      alerting.channels.map((channel) => [channel.id, channel.name]),
    );
    const chosen = alerting.settings.approval_channel_ids.map(
      (id) => names.get(id) ?? "a removed channel",
    );
    return chosen.length ? chosen.join(", ") : "No channel";
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLTextAreaElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  private focus(selector: string) {
    afterNextRender(
      () =>
        (
          this.element.nativeElement.querySelector(
            selector,
          ) as HTMLElement | null
        )?.focus(),
      { injector: this.injector },
    );
  }

  // ---------------------------------------------------------------- compatibility

  async runCheck() {
    if (this.checking()) return;
    this.checking.set(true);
    this.checkNote.set("");
    this.checkError.set("");
    const result = await this.guarded(() => runCompatibilityCheck(this.api));
    this.checking.set(false);
    if (!result) return;
    if (result.ok) {
      this.compatibility.set({ kind: "ready", value: result.value });
      const count = result.value.findings.length;
      this.checkNote.set(
        `Checked just now: ${count} ${count === 1 ? "finding" : "findings"}.`,
      );
    } else this.checkError.set(this.plainText(result.error));
  }
  private plainText(error: unknown) {
    const plain = describeError(error);
    return plain.code ? `${plain.code}: ${plain.message}` : plain.message;
  }

  // ---------------------------------------------------------------- retention

  async makePlan() {
    if (this.planning()) return;
    this.planning.set(true);
    this.retentionNote.set("");
    this.retentionError.set("");
    const result = await this.guarded(() => makeRetentionPlan(this.api));
    this.planning.set(false);
    if (!result) return;
    if (result.ok) this.plan.set(result.value);
    else this.retentionError.set(this.plainText(result.error));
  }
  discardPlan() {
    this.plan.set(null);
    this.confirmApply.set(false);
    this.focus("#admin-make-plan");
  }
  askApply() {
    this.confirmApply.set(true);
    this.focus("#admin-apply-cancel");
  }
  cancelApply() {
    this.confirmApply.set(false);
    this.focus("#admin-apply");
  }
  async apply(plan: RetentionPlan) {
    if (this.applying()) return;
    this.applying.set(true);
    this.retentionError.set("");
    const result = await this.guarded(() =>
      applyRetentionPlan(this.api, plan.id),
    );
    this.applying.set(false);
    if (!result) return;
    this.confirmApply.set(false);
    if (result.ok) {
      const { deleted, blocked } = result.value;
      this.plan.set(null);
      this.retentionNote.set(
        `Deleted ${deleted} debug ${deleted === 1 ? "session" : "sessions"}.` +
          (blocked
            ? ` ${blocked} couldn't be deleted because something started using ${blocked === 1 ? "it" : "them"}.`
            : ""),
      );
      this.focus("#admin-make-plan");
    } else this.retentionError.set(this.plainText(result.error));
  }

  // ---------------------------------------------------------------- alert email

  /** Saves domains or approval channels at the revision shown. */
  private async saveSettings(
    change: (settings: AlertSettings) => AlertSettings,
    note: string,
  ): Promise<boolean> {
    const section = this.alerting();
    if (section.kind !== "ready" || this.saving()) return false;
    const next = change(section.value.settings);
    this.alertingNote.set("");
    this.alertingError.set("");
    this.saving.set(true);
    const result = await this.guarded(() =>
      saveAlertSettings(
        this.api,
        {
          recipient_domains: next.recipient_domains,
          approval_channel_ids: next.approval_channel_ids,
        },
        section.value.settings.revision,
      ),
    );
    this.saving.set(false);
    if (!result) return false;
    const now = this.alerting();
    if (result.ok && now.kind === "ready") {
      this.alerting.set({
        kind: "ready",
        value: { ...now.value, settings: result.value },
      });
      this.alertingNote.set(note);
      return true;
    }
    if (!result.ok) this.alertingError.set(this.plainText(result.error));
    return false;
  }
  openDomain() {
    this.domainDraft.set("");
    this.focus("#admin-domain");
  }
  async addDomain() {
    const section = this.alerting();
    if (section.kind !== "ready" || this.saving()) return;
    const checked = domainProblem(
      this.domainDraft() ?? "",
      section.value.settings.recipient_domains,
    );
    if ("problem" in checked) {
      this.domainError.set(checked.problem);
      return;
    }
    const saved = await this.saveSettings(
      (settings) => ({
        ...settings,
        recipient_domains: [...settings.recipient_domains, checked.domain],
      }),
      `${checked.domain} is allowed.`,
    );
    if (saved) this.domainDraft.set(null);
  }
  removeDomain(domain: string) {
    void this.saveSettings(
      (settings) => ({
        ...settings,
        recipient_domains: settings.recipient_domains.filter(
          (item) => item !== domain,
        ),
      }),
      `${domain} is no longer allowed.`,
    );
  }
  editApprovals(alerting: Alerting) {
    this.approvalsDraft.set([...alerting.settings.approval_channel_ids]);
  }
  toggleApproval(id: string, on: boolean) {
    const draft = (this.approvalsDraft() ?? []).filter((item) => item !== id);
    this.approvalsDraft.set(on ? [...draft, id] : draft);
  }
  async saveApprovals() {
    const draft = this.approvalsDraft();
    if (!draft) return;
    if (draft.length > 5) {
      this.alertingError.set("Choose up to 5 channels.");
      return;
    }
    const saved = await this.saveSettings(
      (settings) => ({ ...settings, approval_channel_ids: draft }),
      "Approval notifications saved.",
    );
    if (saved) this.approvalsDraft.set(null);
  }
  askRevoke(consent: RecipientConsent) {
    this.revoking.set(consent);
    this.focus("#admin-revoke-cancel");
  }
  cancelRevoke() {
    const consent = this.revoking();
    this.revoking.set(null);
    if (consent) this.focus(`#revoke-${CSS.escape(consent.id)}`);
  }
  async revoke(consent: RecipientConsent) {
    if (this.saving()) return;
    this.saving.set(true);
    this.alertingError.set("");
    const result = await this.guarded(() =>
      revokeConsent(this.api, consent.id),
    );
    this.saving.set(false);
    if (!result) return;
    this.revoking.set(null);
    const now = this.alerting();
    if (result.ok && now.kind === "ready") {
      this.alerting.set({
        kind: "ready",
        value: {
          ...now.value,
          consents: now.value.consents.map((item) =>
            item.id === consent.id ? result.value : item,
          ),
        },
      });
      this.alertingNote.set(
        `Revoked the opt-in for ${consent.address}. Mail to it stops at the next send.`,
      );
    } else if (!result.ok) this.alertingError.set(this.plainText(result.error));
  }
  openRecord() {
    this.recordAddress.set("");
    this.recordStatement.set("");
    this.recordAddressError.set("");
    this.recordStatementError.set("");
    this.recordError.set("");
    this.recordKey = crypto.randomUUID();
    this.recordOpen.set(true);
    this.focus("#optin-address");
  }
  closeRecord() {
    this.recordOpen.set(false);
  }
  async record(event: Event) {
    event.preventDefault();
    if (this.saving()) return;
    const address = this.recordAddress().trim();
    const statement = this.recordStatement().trim();
    this.recordAddressError.set(
      mailbox.test(address)
        ? ""
        : "Enter an email address, such as oncall@example.org.",
    );
    this.recordStatementError.set(
      !statement
        ? "Say how they agreed to receive alert mail."
        : statement.length > STATEMENT_LIMIT
          ? `Keep it to ${STATEMENT_LIMIT} characters or fewer.`
          : "",
    );
    if (this.recordAddressError() || this.recordStatementError()) {
      this.focus('[aria-invalid="true"]');
      return;
    }
    this.recordError.set("");
    const key = this.recordKey;
    this.saving.set(true);
    const result = await this.guarded(() =>
      recordConsent(this.api, { address, statement }, key),
    );
    this.saving.set(false);
    if (!result) return;
    if (result.ok) {
      const now = this.alerting();
      if (now.kind === "ready")
        this.alerting.set({
          kind: "ready",
          value: {
            ...now.value,
            consents: [...now.value.consents, result.value],
          },
        });
      this.alertingNote.set(`Recorded the opt-in for ${address}.`);
      this.recordOpen.set(false);
      return;
    }
    const plain = describeError(result.error);
    const detail =
      result.error instanceof ApiError &&
      result.error.detail &&
      typeof result.error.detail === "object"
        ? (result.error.detail as { result?: Record<string, unknown> }).result
        : undefined;
    if (
      plain.code === "WV-ALERT-RECIPIENT" &&
      typeof detail?.["reason"] === "string"
    ) {
      this.recordAddressError.set(
        recipientProblem(detail["reason"], address, this.environment()),
      );
      this.focus("#optin-address");
    } else if (plain.status === 409)
      this.recordAddressError.set(
        `${address} already has an opt-in in ${this.environment()}.`,
      );
    else this.recordError.set(this.plainText(result.error));
  }
}
