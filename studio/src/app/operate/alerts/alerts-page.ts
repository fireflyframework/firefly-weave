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
// Operate › Alerts: what fires now and what resolved in the last 7 days, the
// rules that watch this environment's operations signals, and the email
// channels their notifications go to. The tab lives in the address
// (?tab=rules); recipients never do. The page reads every 30 s while it is
// visible; each tab loads, fails and recovers on its own. People with
// alert.manage acknowledge alerts, edit rules and channels; the platform
// authorizes every change.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  ElementRef,
  Injector,
  Input,
  OnDestroy,
  afterNextRender,
  computed,
  inject,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Router } from "@angular/router";
import type { App } from "../../app";
import { describeError } from "../../errors";
import { absoluteTime, shortId } from "../../format";
import { Icon } from "../../icon";
import { platformConnections } from "../../editor/connections/connections-api";
import { environmentOf, scopeKeyOf } from "../operate-list";
import { RefreshStatus } from "../refresh-status";
import {
  listProjectMembers,
  listTargetNames,
  type Alert,
  type AlertChannel,
  type AlertPreset,
  type AlertRule,
  type AlertsTransport,
  type Recipient,
} from "./alerts-api";
import { AlertChannelForm, type MailConnection } from "./alert-channel-form";
import { AlertRuleEditor } from "./alert-rule-editor";
import {
  alertLine,
  alertsPath,
  alertsTab,
  alertsTabs,
  conditionText,
  domainOf,
  nameParts,
  notificationTone,
  recipientProblem,
  resolvedText,
  severityWords,
  signalName,
  signalWords,
  spanText,
  startedText,
  stateLine,
  type AlertsTab,
  type StateLine,
} from "./alerts-model";
import { AlertsStore } from "./alerts-store";

const tabLabels: Record<AlertsTab, string> = {
  firing: "Firing and recent",
  rules: "Rules",
  channels: "Channels",
};
/** What the editor opens on: a saved rule, a preset, or a blank rule. */
export type RuleTarget =
  | { kind: "new" }
  | { kind: "preset"; preset: AlertPreset }
  | { kind: "rule"; rule: AlertRule };

@Component({
  selector: "weave-alerts-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    Icon,
    NgTemplateOutlet,
    RefreshStatus,
    AlertRuleEditor,
    AlertChannelForm,
  ],
  styleUrls: ["../operate.css", "./alerts.css"],
  host: { class: "alerts-page" },
  template: `<div class="operate-heading">
      <div>
        <h1>Alerts</h1>
        <p class="subtitle">
          Rules that watch this environment's operations signals, and where
          their notifications go.
        </p>
      </div>
      @if (readable()) {
        <div class="actions">
          <weave-refresh-status
            [updatedAt]="store.updatedAt()"
            [busy]="store.poller.busy"
            (refresh)="refreshNow()"
          />
        </div>
      }
    </div>
    @if (!host.profile) {
      <div class="empty-state">
        <h2>Alerts appear here once you connect</h2>
        <p>Alert rules watch a platform's operations signals.</p>
        <button class="primary" (click)="host.openWizard('server')">
          Connect to a platform
        </button>
      </div>
    } @else if (host.signInEnded || host.platformSignedOut) {
      <div class="empty-state">
        <h2>Sign in to see alerts</h2>
        <p>
          {{
            host.signInEnded
              ? "Studio shows them as soon as you're signed in again."
              : "You're signed out of " + host.profile.name + "."
          }}
        </p>
        <button
          type="button"
          class="primary"
          (click)="host.signInPlatform(host.profile.name, false)"
        >
          Sign in
        </button>
      </div>
    } @else if (!host.identity) {
      <div class="skeleton" role="status">
        <span class="sr-only">Loading alerts…</span>
        @for (row of [0, 1, 2]; track row) {
          <span aria-hidden="true"><i></i><i></i><i></i></span>
        }
      </div>
    } @else if (!readable()) {
      <div class="empty alerts-access">
        <span class="ring"><weave-icon name="lock" [size]="20" /></span>
        <h2>You need <code>alert.read</code> in {{ environmentName() }}.</h2>
        <p>Ask an administrator in Settings › People and access.</p>
      </div>
    } @else {
      @if (store.evaluatorDown()) {
        <div class="notice alerts-notice" role="status">
          <weave-icon name="warning" [size]="16" /><span
            >Alert rules aren't being checked right now. Check Operate ›
            Overview.</span
          >
        </div>
      }
      @if (editing(); as target) {
        <weave-alert-rule-editor
          [target]="target"
          [store]="store"
          [channels]="channelList()"
          [workflowNames]="workflowNames()"
          [targets]="targetNames()"
          [environment]="environmentName()"
          [canPreview]="host.can('status.read')"
          (done)="closeEditor($event)"
        />
      } @else {
        <div
          class="alerts-tabs"
          role="tablist"
          aria-label="Alerts sections"
          (keydown)="tabKey($event)"
        >
          @for (item of tabs; track item) {
            <button
              type="button"
              role="tab"
              [id]="'alerts-tab-' + item"
              [attr.aria-controls]="'alerts-panel-' + item"
              [attr.aria-selected]="tab() === item"
              [attr.tabindex]="tab() === item ? 0 : -1"
              (click)="openTab(item)"
            >
              {{ labels[item] }}
              @if (counts()[item] !== null) {
                <span class="count">{{ counts()[item] }}</span>
              }
            </button>
          }
        </div>
        @switch (tab()) {
          @case ("firing") {
            <div
              role="tabpanel"
              id="alerts-panel-firing"
              aria-labelledby="alerts-tab-firing"
            >
              @let a = store.alerts();
              <section aria-labelledby="alerts-firing-title">
                <div class="section-head">
                  <h2 id="alerts-firing-title">Firing</h2>
                  @if (a.kind === "ready" && a.value.firing.length) {
                    <span class="cap"
                      >{{ alertCount(a.value.firingCount) }} · rules are checked
                      every 30 seconds</span
                    >
                  }
                </div>
                @switch (a.kind) {
                  @case ("ready") {
                    @if (a.value.firing.length) {
                      <div
                        class="records firing-list"
                        role="table"
                        aria-label="Firing alerts"
                      >
                        <div class="rec head" role="row">
                          <span role="columnheader">Severity</span
                          ><span role="columnheader">Rule</span
                          ><span role="columnheader">Started</span
                          ><span role="columnheader">Notifications</span
                          ><span role="columnheader"
                            ><span class="sr-only">Actions</span></span
                          >
                        </div>
                        @for (alert of a.value.firing; track alert.id) {
                          <div
                            class="rec"
                            role="row"
                            tabindex="-1"
                            [id]="'alert-' + alert.id"
                          >
                            <span role="cell"
                              ><span
                                class="sev"
                                [class]="'tone-' + severity(alert).tone"
                                ><weave-icon
                                  [name]="severity(alert).icon"
                                  [size]="16"
                                />{{ severity(alert).label }}</span
                              ></span
                            >
                            <span role="cell" class="rmain wide"
                              ><b>{{ alert.rule_name }}</b
                              ><span class="muted">{{ line(alert) }}</span
                              ><span role="status">
                                @if (alert.acknowledged_at) {
                                  <span class="line ack"
                                    ><weave-icon
                                      name="ok"
                                      class="tone-success"
                                      [size]="16"
                                    /><span
                                      >Acknowledged by
                                      {{ person(alert.acknowledged_by, false) }}
                                      · repeat notifications stopped until it
                                      resolves.</span
                                    ></span
                                  >
                                }
                              </span></span
                            >
                            <span role="cell"
                              ><span class="lbl">Started</span
                              ><time
                                [attr.datetime]="alert.started_at"
                                [title]="absolute(alert.started_at)"
                                >{{ started(alert.started_at) }}</time
                              ><br /><span class="subtle"
                                >firing for {{ firingFor(alert) }}</span
                              ></span
                            >
                            <span role="cell" class="lines wide">
                              @for (
                                note of notifications(alert);
                                track note.channel
                              ) {
                                <span class="line"
                                  ><weave-icon
                                    [name]="note.line.icon"
                                    [class]="'tone-' + note.line.tone"
                                    [size]="16"
                                  /><span
                                    ><b>{{ note.channel }}:</b>
                                    {{ note.line.text }}</span
                                  ></span
                                >
                              } @empty {
                                <span class="muted"
                                  >No channel: it shows here and in the
                                  navigation badge.</span
                                >
                              }
                            </span>
                            <span role="cell" class="acts">
                              @if (canManage() && !alert.acknowledged_at) {
                                <button
                                  type="button"
                                  [attr.aria-disabled]="
                                    acknowledging() === alert.id ? 'true' : null
                                  "
                                  [attr.aria-describedby]="
                                    'alert-' + alert.id + '-name'
                                  "
                                  (click)="acknowledge(alert)"
                                >
                                  Acknowledge
                                </button>
                                <span
                                  class="sr-only"
                                  [id]="'alert-' + alert.id + '-name'"
                                  >{{ alert.rule_name }}</span
                                >
                              }
                            </span>
                          </div>
                        }
                      </div>
                      @if (ackError()) {
                        <p class="problem" role="alert">
                          <weave-icon name="failCircle" [size]="16" />{{
                            ackError()
                          }}
                        </p>
                      }
                    } @else {
                      <div class="empty">
                        <span class="ring"
                          ><weave-icon name="ok" [size]="20"
                        /></span>
                        <h3>No alerts are firing.</h3>
                        <p>Rules are checked every 30 seconds.</p>
                      </div>
                    }
                  }
                  @default {
                    <ng-container
                      [ngTemplateOutlet]="sectionState"
                      [ngTemplateOutletContext]="{ $implicit: a }"
                    />
                  }
                }
              </section>
              @if (a.kind === "ready") {
                <section aria-labelledby="alerts-recent-title">
                  <div class="section-head">
                    <h2 id="alerts-recent-title">Recent</h2>
                    <span class="cap">Resolved in the last 7 days</span>
                  </div>
                  @if (a.value.recent.length) {
                    <div
                      class="records recent-list"
                      role="table"
                      aria-label="Recently resolved alerts"
                    >
                      <div class="rec head" role="row">
                        <span role="columnheader">Severity</span
                        ><span role="columnheader">Rule</span
                        ><span role="columnheader">Started</span
                        ><span role="columnheader">How it ended</span
                        ><span role="columnheader"></span>
                      </div>
                      @for (alert of a.value.recent; track alert.id) {
                        <div class="rec" role="row">
                          <span role="cell"
                            ><span
                              class="sev"
                              [class]="'tone-' + severity(alert).tone"
                              ><weave-icon
                                [name]="severity(alert).icon"
                                [size]="16"
                              />{{ severity(alert).label }}</span
                            ></span
                          >
                          <span role="cell" class="rmain wide"
                            ><b>{{ alert.rule_name }}</b
                            ><span class="muted"
                              >Resolved after {{ lasted(alert) }}</span
                            ></span
                          >
                          <span role="cell"
                            ><span class="lbl">Started</span
                            ><time
                              [attr.datetime]="alert.started_at"
                              [title]="absolute(alert.started_at)"
                              >{{ started(alert.started_at) }}</time
                            ></span
                          >
                          <span role="cell" class="wide muted">{{
                            ended(alert)
                          }}</span>
                          <span role="cell"></span>
                        </div>
                      }
                    </div>
                  } @else {
                    <p class="hint">No alerts resolved in the last 7 days.</p>
                  }
                </section>
              }
            </div>
          }
          @case ("rules") {
            <div
              role="tabpanel"
              id="alerts-panel-rules"
              aria-labelledby="alerts-tab-rules"
            >
              @let r = store.rules();
              @if (r.kind === "ready") {
                @if (r.value.length) {
                  <div class="toolbar">
                    @if (canManage()) {
                      <button
                        type="button"
                        class="primary"
                        (click)="openEditor({ kind: 'new' })"
                      >
                        <weave-icon name="plus" [size]="16" />Add rule
                      </button>
                      <button
                        type="button"
                        aria-controls="alerts-presets"
                        [attr.aria-expanded]="presetsOpen()"
                        (click)="presetsOpen.set(!presetsOpen())"
                      >
                        Suggested rules
                      </button>
                    }
                    <span class="cap"
                      >{{ r.value.length }} of 100 rules in
                      {{ environmentName() }}</span
                    >
                  </div>
                  <ng-container [ngTemplateOutlet]="presetList" />
                  <div
                    class="records rules-list"
                    role="table"
                    aria-label="Alert rules"
                  >
                    <div class="rec head" role="row">
                      <span role="columnheader">Rule</span
                      ><span role="columnheader">Condition</span
                      ><span role="columnheader">Severity</span
                      ><span role="columnheader">Sends to</span
                      ><span role="columnheader">Now</span
                      ><span role="columnheader"
                        ><span class="sr-only">Actions</span></span
                      >
                    </div>
                    @for (rule of r.value; track rule.id) {
                      @let now = ruleNow(rule);
                      <div class="rec" role="row">
                        <span role="cell" class="rmain wide"
                          ><b>{{ rule.name }}</b
                          ><span class="muted"
                            ><ng-container
                              [ngTemplateOutlet]="named"
                              [ngTemplateOutletContext]="{
                                $implicit: ruleSignal(rule),
                                names: ruleNames(rule),
                              }"
                          /></span>
                          @if (rule.preset_id) {
                            <span class="tag">Suggested by Weave</span>
                          }
                        </span>
                        <span role="cell" class="wide"
                          ><span class="lbl">Condition</span
                          >{{ condition(rule) }}</span
                        >
                        <span role="cell"
                          ><span
                            class="sev"
                            [class]="'tone-' + severity(rule).tone"
                            ><weave-icon
                              [name]="severity(rule).icon"
                              [size]="16"
                            />{{ severity(rule).label }}</span
                          ></span
                        >
                        <span role="cell" class="wide"
                          ><span class="lbl">Sends to</span
                          >{{ sendsTo(rule) }}</span
                        >
                        <span role="cell" class="wide line"
                          ><weave-icon
                            [name]="now.icon"
                            [class]="'tone-' + now.tone"
                            [size]="16" /><span
                            ><ng-container
                              [ngTemplateOutlet]="named"
                              [ngTemplateOutletContext]="{
                                $implicit: now.text,
                                names: ruleNames(rule),
                              }" /></span
                        ></span>
                        <span role="cell" class="acts">
                          @if (canManage()) {
                            <label class="switch"
                              ><input
                                type="checkbox"
                                role="switch"
                                [checked]="rule.enabled"
                                [attr.aria-label]="'Enabled: ' + rule.name"
                                [attr.aria-disabled]="
                                  switching() === rule.id ? 'true' : null
                                "
                                (click)="toggle(rule, $event)"
                            /></label>
                            <button
                              type="button"
                              class="sm"
                              [attr.aria-label]="'Edit ' + rule.name"
                              (click)="openEditor({ kind: 'rule', rule })"
                            >
                              Edit
                            </button>
                          }
                        </span>
                      </div>
                    }
                  </div>
                  @if (ruleError()) {
                    <p class="problem" role="alert">
                      <weave-icon name="failCircle" [size]="16" />{{
                        ruleError()
                      }}
                    </p>
                  }
                } @else {
                  <div class="empty">
                    <span class="ring"
                      ><weave-icon name="alerts" [size]="20"
                    /></span>
                    <h3>No alert rules yet.</h3>
                    <p>Add a rule, or start from a suggested one.</p>
                    @if (canManage()) {
                      <div class="acts">
                        <button
                          type="button"
                          class="primary"
                          (click)="openEditor({ kind: 'new' })"
                        >
                          <weave-icon name="plus" [size]="16" />Add rule
                        </button>
                        <button
                          type="button"
                          aria-controls="alerts-presets"
                          [attr.aria-expanded]="presetsOpen()"
                          (click)="presetsOpen.set(!presetsOpen())"
                        >
                          Suggested rules
                        </button>
                      </div>
                    }
                  </div>
                  <ng-container [ngTemplateOutlet]="presetList" />
                }
              } @else {
                <ng-container
                  [ngTemplateOutlet]="sectionState"
                  [ngTemplateOutletContext]="{ $implicit: r }"
                />
              }
            </div>
          }
          @case ("channels") {
            <div
              role="tabpanel"
              id="alerts-panel-channels"
              aria-labelledby="alerts-tab-channels"
            >
              @let c = store.channels();
              @if (c.kind === "ready") {
                <div class="toolbar">
                  @if (canManage()) {
                    <button
                      type="button"
                      class="primary"
                      (click)="openChannel(null, $event)"
                    >
                      <weave-icon name="plus" [size]="16" />Add channel
                    </button>
                  }
                  <span class="cap"
                    >{{ c.value.length }} of 20 channels in
                    {{ environmentName() }}</span
                  >
                </div>
                @if (c.value.length) {
                  <div
                    class="records channels-list"
                    role="table"
                    aria-label="Email channels"
                  >
                    <div class="rec head" role="row">
                      <span role="columnheader">Channel</span
                      ><span role="columnheader">Used by</span
                      ><span role="columnheader">Recipients</span
                      ><span role="columnheader"
                        ><span class="sr-only">Actions</span></span
                      >
                    </div>
                    @for (channel of c.value; track channel.id) {
                      <div class="rec" role="row">
                        <span role="cell" class="rmain wide"
                          ><b>{{ channel.name }}</b
                          ><span class="muted"
                            ><ng-container
                              [ngTemplateOutlet]="named"
                              [ngTemplateOutletContext]="{
                                $implicit: sentAs(channel),
                                names: [connectionName(channel)],
                              }" /></span
                        ></span>
                        <span role="cell" class="wide"
                          ><span class="lbl">Used by</span
                          >{{ usedBy(channel) }}</span
                        >
                        <span role="cell" class="lines wide">
                          @if (channel.status === "owner_unauthorized") {
                            <span class="line tone-warning"
                              ><weave-icon name="warning" [size]="16" /><span
                                >Mail can't go out as
                                {{ person(channel.owner_id, false) }} anymore.
                                Edit the channel to send as you.</span
                              ></span
                            >
                          }
                          @for (
                            recipient of recipients(channel);
                            track recipient.key
                          ) {
                            <span
                              class="line"
                              [class.tone-warning]="recipient.problem"
                              ><weave-icon
                                [name]="recipient.icon"
                                [class.subtle]="!recipient.problem"
                                [size]="16" /><span
                                ><ng-container
                                  [ngTemplateOutlet]="named"
                                  [ngTemplateOutletContext]="{
                                    $implicit: recipient.text,
                                    names: recipient.names,
                                  }" /></span
                            ></span>
                          }
                        </span>
                        <span role="cell" class="acts">
                          @if (canManage()) {
                            <button
                              type="button"
                              class="sm"
                              [attr.aria-label]="'Edit ' + channel.name"
                              (click)="openChannel(channel, $event)"
                            >
                              Edit
                            </button>
                          }
                        </span>
                      </div>
                    }
                  </div>
                } @else {
                  <div class="empty">
                    <span class="ring"
                      ><weave-icon name="email" [size]="20"
                    /></span>
                    <h3>No email channels yet.</h3>
                    <p>
                      Without a channel, firing alerts show on this page and in
                      the navigation badge only.
                    </p>
                  </div>
                }
                @if (store.webhooks() !== null) {
                  <p class="webhooks">
                    <weave-icon name="webhook" [size]="16" /><span
                      >{{ webhookText() }} Manage them with
                      <code>weave subscriptions</code>.</span
                    >
                  </p>
                }
              } @else {
                <ng-container
                  [ngTemplateOutlet]="sectionState"
                  [ngTemplateOutletContext]="{ $implicit: c }"
                />
              }
            </div>
          }
        }
      }
      @if (sheet(); as open) {
        <weave-alert-channel-form
          [channel]="open.channel"
          [store]="store"
          [rules]="ruleList()"
          [settings]="settingsValue()"
          [connections]="connections()"
          [members]="members()"
          [me]="host.identity.principal_id"
          [environment]="environmentName()"
          [personName]="personNameFn"
          (done)="closeChannel()"
        />
      }
    }
    <ng-template #sectionState let-section>
      @switch (section.kind) {
        @case ("error") {
          <div class="notice alerts-notice" data-tone="danger" role="alert">
            <weave-icon name="failCircle" [size]="16" />
            <p>
              {{
                section.error.code
                  ? section.error.code + ": " + section.error.message
                  : section.error.message
              }}
            </p>
            <button type="button" class="sm" (click)="refreshNow()">
              Try again
            </button>
          </div>
        }
        @case ("refused") {
          <div class="empty">
            <span class="ring"><weave-icon name="lock" [size]="20" /></span>
            <h3>
              You need <code>alert.read</code> in {{ environmentName() }}.
            </h3>
            <p>Ask an administrator in Settings › People and access.</p>
          </div>
        }
        @default {
          <div class="skeleton" role="status">
            <span class="sr-only">Loading…</span>
            @for (row of [0, 1, 2]; track row) {
              <span aria-hidden="true"><i></i><i></i><i></i></span>
            }
          </div>
        }
      }
    </ng-template>
    <ng-template #named let-text let-names="names">
      @for (part of parts(text, names); track $index) {
        @if (part.name) {
          <span class="nb">{{ part.text }}</span>
        } @else {
          <!-- No space of its own: "address: why" keeps its colon close. -->
          <ng-container>{{ part.text }}</ng-container>
        }
      }
    </ng-template>
    <ng-template #presetList>
      @if (presetsOpen()) {
        <ul class="presets" id="alerts-presets" aria-label="Suggested rules">
          @for (preset of suggested(); track preset.id) {
            <li>
              <span
                ><b>{{ preset.name }}</b
                ><span class="muted">{{ condition(preset.rule) }}</span></span
              >
              @if (preset.installed_rule_id) {
                <span class="muted">Added</span>
              } @else {
                <button
                  type="button"
                  class="sm"
                  [attr.aria-label]="'Start from ' + preset.name"
                  (click)="openEditor({ kind: 'preset', preset })"
                >
                  Start from this
                </button>
              }
            </li>
          } @empty {
            <li class="muted">This platform suggests no rules.</li>
          }
        </ul>
      }
    </ng-template>`,
})
export class AlertsPage implements DoCheck, OnDestroy {
  @Input({ required: true }) host!: App;
  private router = inject(Router);
  private injector = inject(Injector);
  private element = inject(ElementRef<HTMLElement>);
  readonly tabs = alertsTabs;
  readonly labels = tabLabels;
  readonly store = new AlertsStore(
    {
      api: () => this.host.api as unknown as AlertsTransport,
      scope: () => scopeKeyOf(this.host),
    },
    {
      paused: () => !!this.sheet(),
      onFailure: (error) => this.signInGone(error),
    },
  );
  readonly tab = signal<AlertsTab>(alertsTab(location.search));
  readonly editing = signal<RuleTarget | null>(null);
  readonly sheet = signal<{ channel: AlertChannel | null } | null>(null);
  readonly presetsOpen = signal(false);
  readonly acknowledging = signal("");
  readonly ackError = signal("");
  readonly switching = signal("");
  readonly ruleError = signal("");
  /** Target names by ID, and the people and mail connections the channel sheet offers. */
  readonly targetNames = signal<Map<string, string>>(new Map());
  readonly connections = signal<MailConnection[]>([]);
  readonly members = signal<string[]>([]);
  private opener: HTMLElement | null = null;
  private scopeKey = "";
  private search = location.search;
  /** Ticks every 30 s with the reads, so "firing for" stays current. */
  private readonly now = computed(() => {
    this.store.updatedAt();
    return new Date();
  });
  readonly counts = computed<Record<AlertsTab, number | null>>(() => {
    const alerts = this.store.alerts();
    const rules = this.store.rules();
    const channels = this.store.channels();
    return {
      firing: alerts.kind === "ready" ? alerts.value.firingCount : null,
      rules: rules.kind === "ready" ? rules.value.length : null,
      channels: channels.kind === "ready" ? channels.value.length : null,
    };
  });
  readonly ruleList = computed(() => {
    const rules = this.store.rules();
    return rules.kind === "ready" ? rules.value : [];
  });
  readonly channelList = computed(() => {
    const channels = this.store.channels();
    return channels.kind === "ready" ? channels.value : [];
  });
  readonly settingsValue = computed(() => {
    const settings = this.store.settings();
    return settings.kind === "ready" ? settings.value : null;
  });
  readonly suggested = computed(() =>
    this.store.presets().filter((preset) => preset.install === "suggested"),
  );
  readonly personNameFn = (id: string, sentenceStart: boolean) =>
    this.person(id, sentenceStart);

  /** With the person known, only with alert.read anywhere in the workspace. */
  readable() {
    return !!this.host.identity && this.host.canAnywhere("alert.read");
  }
  canManage() {
    return this.host.can("alert.manage");
  }
  environmentName() {
    return this.host.workspaceNames?.environment || "this environment";
  }

  ngDoCheck() {
    // Back and Forward change the tab without this page.
    if (
      location.pathname === "/operate/alerts" &&
      location.search !== this.search
    ) {
      this.search = location.search;
      this.tab.set(alertsTab(location.search));
    }
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    // Another platform, environment or person: nothing read before stays.
    this.scopeKey = key;
    this.store.poller.stop();
    this.store.forget();
    this.editing.set(null);
    this.sheet.set(null);
    this.targetNames.set(new Map());
    this.connections.set([]);
    this.members.set([]);
    if (environmentOf(this.host) && this.readable()) {
      this.store.poller.start(true);
      void this.readNames(key);
    }
  }
  ngOnDestroy() {
    this.store.poller.stop();
    this.store.forget();
  }
  refreshNow() {
    void this.store.poller.refresh();
  }
  /** An ended sign-in is the shell's to explain, with its own Sign in again. */
  private signInGone(error: unknown) {
    const plain = describeError(error);
    if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
      this.host.fail(error);
  }
  /**
   * Target names for rules that name one, the people a channel can list and
   * the mail connections this person may send from. Each is a convenience:
   * without it, the page names a target or person by its ID.
   */
  private async readNames(key: string) {
    const api = this.host.api as unknown as AlertsTransport;
    const current = () => key === scopeKeyOf(this.host);
    const projectId = this.host.profile?.projectId ?? "";
    const [targets, members, mail] = await Promise.allSettled([
      this.host.canAnywhere("deployment.read")
        ? listTargetNames(api)
        : Promise.resolve(new Map<string, string>()),
      this.host.canManageMembers
        ? listProjectMembers(api, projectId)
        : Promise.resolve([]),
      platformConnections(this.host.api).bindableNamed("weave-email"),
    ]);
    if (!current()) return;
    if (targets.status === "fulfilled") this.targetNames.set(targets.value);
    if (members.status === "fulfilled")
      this.members.set(members.value.map((member) => member.id));
    if (mail.status === "fulfilled")
      this.connections.set(
        mail.value
          .filter((item) => item.enabled)
          .map((item) => ({ revisionId: item.revisionId, name: item.name })),
      );
  }

  // ---------------------------------------------------------------- tabs

  openTab(tab: AlertsTab) {
    this.tab.set(tab);
    this.presetsOpen.set(false);
    const path = alertsPath(tab);
    this.search = path.includes("?") ? path.slice(path.indexOf("?")) : "";
    if (location.pathname + location.search !== path)
      void this.router.navigateByUrl(path);
  }
  /** Arrow keys, Home and End move between the tabs (WAI-ARIA tabs). */
  tabKey(event: KeyboardEvent) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const index = this.tabs.indexOf(this.tab());
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? this.tabs.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + this.tabs.length) %
            this.tabs.length;
    const tab = this.tabs[next];
    this.openTab(tab);
    afterNextRender(
      () =>
        (
          this.element.nativeElement.querySelector(
            `#alerts-tab-${tab}`,
          ) as HTMLElement | null
        )?.focus(),
      { injector: this.injector },
    );
  }

  // ---------------------------------------------------------------- words

  alertCount(count: number) {
    return `${count} ${count === 1 ? "alert" : "alerts"}`;
  }
  severity(item: { severity: Alert["severity"] }) {
    return severityWords[item.severity];
  }
  line(alert: Alert) {
    return alertLine(alert);
  }
  started(at: string) {
    return startedText(at, this.now());
  }
  absolute(at: string) {
    return absoluteTime(at);
  }
  firingFor(alert: Alert) {
    return spanText(this.now().getTime() - Date.parse(alert.started_at));
  }
  lasted(alert: Alert) {
    return spanText(
      Date.parse(alert.resolved_at ?? alert.started_at) -
        Date.parse(alert.started_at),
    );
  }
  ended(alert: Alert) {
    return resolvedText(alert.resolved_reason);
  }
  /** The newest notification per channel, by channel name. */
  notifications(alert: Alert) {
    const seen = new Set<string>();
    const names = new Map(
      this.channelList().map((channel) => [channel.id, channel.name]),
    );
    return alert.notifications
      .filter(
        (note) =>
          note.kind !== "test" &&
          !seen.has(note.channel_id) &&
          !!seen.add(note.channel_id),
      )
      .map((note) => ({
        channel: names.get(note.channel_id) ?? "A removed channel",
        line: notificationTone(note),
      }));
  }
  condition(rule: AlertRule | AlertPreset["rule"]) {
    return conditionText(rule);
  }
  /** "Run failure rate", with the workflow, task type or target it narrows to. */
  ruleSignal(rule: AlertRule) {
    const key = signalWords(rule.signal)?.selector;
    const narrowed = key ? rule.selector[key] : undefined;
    const name =
      key === "target_id" && narrowed
        ? this.targetName(narrowed) || `target ${shortId(narrowed)}`
        : narrowed;
    return name
      ? `${signalName(rule.signal)}, ${name}`
      : signalName(rule.signal);
  }
  sendsTo(rule: AlertRule) {
    if (!rule.channel_ids.length) return "In-app only";
    const names = new Map(
      this.channelList().map((channel) => [channel.id, channel.name]),
    );
    return rule.channel_ids
      .map((id) => names.get(id) ?? "a removed channel")
      .join(", ");
  }
  ruleNow(rule: AlertRule): StateLine {
    if (!rule.enabled)
      return { text: "Switched off", tone: "subtle", icon: "off" };
    const alerts = this.store.alerts();
    const alert =
      alerts.kind === "ready"
        ? alerts.value.firing.find((item) => item.id === rule.state.alert_id)
        : null;
    return stateLine(rule, {
      now: this.now(),
      alert,
      target: (id) => this.targetName(id),
    });
  }
  targetName(id: string) {
    return this.targetNames().get(id) ?? "";
  }
  /** "you" (or "You" at a sentence's start) for the viewer, else the person's name. */
  person(id: string | null, sentenceStart: boolean) {
    if (id && id === this.host.identity?.principal_id)
      return sentenceStart ? "You" : "you";
    const label = id ? this.host.accountLabel(id) : "";
    return label.startsWith("Account ")
      ? `Person ${shortId(id ?? "")}`
      : label || "someone";
  }
  sentAs(channel: AlertChannel) {
    const from = this.connections().find(
      (item) => item.revisionId === channel.connection_revision_id,
    );
    return `Email · sent as ${this.person(channel.owner_id, false)}, from ${from?.name ?? "their email connection"}`;
  }
  connectionName(channel: AlertChannel) {
    return (
      this.connections().find(
        (item) => item.revisionId === channel.connection_revision_id,
      )?.name ?? ""
    );
  }
  /** The names a rule's lines mention: its workflow, task type or target. */
  ruleNames(rule: AlertRule): string[] {
    const key = signalWords(rule.signal)?.selector;
    const narrowed = key ? rule.selector[key] : undefined;
    if (!narrowed) return [];
    return key === "target_id"
      ? [this.targetName(narrowed) || `target ${shortId(narrowed)}`]
      : [narrowed];
  }
  parts(text: string, names: string[]) {
    return nameParts(text, names);
  }
  usedBy(channel: AlertChannel) {
    const rules = this.ruleList().filter((rule) =>
      rule.channel_ids.includes(channel.id),
    ).length;
    const parts = rules ? [`${rules} ${rules === 1 ? "rule" : "rules"}`] : [];
    if (this.settingsValue()?.approval_channel_ids.includes(channel.id))
      parts.push("approval notifications");
    return parts.join(" · ") || "Nothing yet";
  }
  /** Each recipient as the channel row lists it, with any reason it's skipped. */
  recipients(channel: AlertChannel) {
    const states = new Map(
      channel.recipient_states.map((item) => [item.index, item.state]),
    );
    const environment = this.environmentName();
    const settings = this.store.settings();
    // Delivery skips an address whose domain an administrator removed.
    const allowed =
      settings.kind === "ready"
        ? new Set(settings.value.recipient_domains)
        : null;
    return channel.recipients.map((recipient: Recipient, index) => {
      const read = states.get(index) ?? "ok";
      const state =
        read === "ok" &&
        "address" in recipient &&
        allowed &&
        !allowed.has(domainOf(recipient.address))
          ? "domain_not_allowed"
          : read;
      const key = "member" in recipient ? recipient.member : recipient.address;
      if ("member" in recipient) {
        const you = recipient.member === this.host.identity?.principal_id;
        const name = this.person(recipient.member, true);
        const text =
          state === "without_address"
            ? you
              ? "You haven't set an alert address. Set one in Settings › Preferences."
              : `${name} hasn't set an alert address. Ask them to set one in Settings › Preferences.`
            : state === "not_member"
              ? `${name} isn't a project member anymore, so the channel skips them.`
              : name;
        return {
          key,
          text,
          names: [] as string[],
          icon: state === "ok" ? "user" : "warning",
          problem: state !== "ok",
        };
      }
      const text =
        state === "domain_not_allowed"
          ? `${recipient.address}: ${recipientProblem("domain_not_allowed", recipient.address, environment)}`
          : state === "without_consent"
            ? `${recipient.address} has no opt-in in ${environment}, so the channel skips it.`
            : `${recipient.address} · outside address`;
      return {
        key,
        text,
        names: [recipient.address],
        icon: state === "ok" ? "email" : "warning",
        problem: state !== "ok",
      };
    });
  }
  webhookText() {
    const count = this.store.webhooks() ?? 0;
    return count === 1
      ? "1 webhook subscription receives alert changes."
      : `${count} webhook subscriptions receive alert changes.`;
  }
  /** Workflow names this person has seen: published versions and rules. */
  workflowNames() {
    const names = new Set<string>();
    for (const version of this.host.workflowVersions.values())
      if (version.name) names.add(version.name);
    for (const rule of this.ruleList())
      if (rule.selector.workflow) names.add(rule.selector.workflow);
    return [...names].sort();
  }

  // ---------------------------------------------------------------- actions

  /** Acknowledges, then keeps focus on the row, which says so. */
  async acknowledge(alert: Alert) {
    if (this.acknowledging()) return;
    this.acknowledging.set(alert.id);
    this.ackError.set("");
    const result = await this.store.acknowledge(alert.id);
    this.acknowledging.set("");
    if (result === "failed")
      this.ackError.set(
        `Studio couldn't acknowledge “${alert.rule_name}”. Try again.`,
      );
    if (result === "applied")
      afterNextRender(
        () =>
          (
            this.element.nativeElement.querySelector(
              `#alert-${CSS.escape(alert.id)}`,
            ) as HTMLElement | null
          )?.focus(),
        { injector: this.injector },
      );
  }
  /** The switch shows the platform's answer, not the click. */
  async toggle(rule: AlertRule, event: Event) {
    event.preventDefault();
    if (this.switching()) return;
    this.switching.set(rule.id);
    this.ruleError.set("");
    try {
      await this.store.setEnabled(rule, !rule.enabled);
    } catch (error) {
      this.ruleError.set(
        `Studio couldn't switch “${rule.name}” ${rule.enabled ? "off" : "on"}. ${describeError(error).message}`,
      );
    } finally {
      this.switching.set("");
    }
  }
  openEditor(target: RuleTarget) {
    this.presetsOpen.set(false);
    this.editing.set(target);
  }
  closeEditor(saved: AlertRule | null) {
    this.editing.set(null);
    if (this.tab() !== "rules") this.openTab("rules");
    afterNextRender(
      () => {
        const root = this.element.nativeElement;
        const button = saved
          ? (root.querySelector(
              `button[aria-label="Edit ${CSS.escape(saved.name)}"]`,
            ) as HTMLElement | null)
          : null;
        (
          button ??
          (root.querySelector("#alerts-tab-rules") as HTMLElement | null)
        )?.focus();
      },
      { injector: this.injector },
    );
  }
  openChannel(channel: AlertChannel | null, event: Event) {
    this.opener = event.currentTarget as HTMLElement;
    this.sheet.set({ channel });
  }
  closeChannel() {
    this.sheet.set(null);
    const opener = this.opener;
    this.opener = null;
    afterNextRender(() => opener?.focus(), { injector: this.injector });
  }
}
