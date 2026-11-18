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
// A trigger choice's Test event pane: what the trigger can do here, and a
// sample its run starts with. A webhook listens for a test event where Studio
// is connected and test calls are on; anywhere else, and for Kafka, email and
// app event triggers, the person pastes a sample instead. The sample becomes
// the workflow's test input, as the input form's Test event does.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import { connectionsPort } from "../connections/connections-port";
import type { StepDetailsHost } from "../ndv/step-details-host";
import { listenBlocked, type ListenState } from "./test-listener";
import { productionUrl, secretCommand } from "./webhook-help";
import type { FormSession } from "../ndv/params/form-session";
import type { Json } from "../ndv/registry";
import type { TriggerIntent } from "../state/canvas-sidecar";

const LOCAL_NOTE =
  "Listening needs a platform. Paste a sample payload instead.";

/** What a pasted sample is called, by trigger. */
const SAMPLE_NOUNS: Partial<Record<TriggerIntent["kind"], string>> = {
  webhook: "payload",
  broker: "message",
  email: "email",
  provider: "event",
  schedule: "run input",
};

@Component({
  selector: "weave-trigger-event-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `<header class="sd-pane-head">
      <h3 class="sd-pane-title">Test event</h3>
    </header>
    <div class="sd-pane-body">
      <div class="pane-content trigger-event">
        @if (note(); as text) {
          <p class="pane-empty-text">{{ text }}</p>
        }
        @if (canListen()) {
          @let state = listenState();
          @switch (state?.kind) {
            @case ("starting") {
              <p class="trigger-event-status" role="status">
                <span class="loading-spinner small"></span>Getting a test URL…
              </p>
            }
            @case ("listening") {
              @if (state?.kind === "listening") {
                <p class="trigger-event-status" role="status">
                  <span class="trigger-event-dot" aria-hidden="true"></span
                  >Listening for test event
                </p>
                <label class="trigger-event-label" [attr.for]="urlId()"
                  >Make a POST request to:</label
                >
                <div class="trigger-event-copy">
                  <input
                    class="trigger-event-url"
                    readonly
                    [id]="urlId()"
                    [value]="state.url"
                  />
                  <button
                    type="button"
                    class="pane-generate"
                    (click)="copy(state.url, 'the test URL')"
                  >
                    Copy URL
                  </button>
                </div>
                @if (testSecretCommand(); as setCommand) {
                  <p class="trigger-event-label">
                    Set the test secret's value on a local platform with:
                  </p>
                  <div class="trigger-event-copy">
                    <code
                      class="trigger-event-code"
                      aria-label="Set the test secret"
                      >{{ setCommand }}</code
                    >
                    <button
                      type="button"
                      class="pane-generate"
                      (click)="copy(setCommand, 'the command')"
                    >
                      Copy
                    </button>
                  </div>
                }
                <pre
                  class="trigger-event-code trigger-event-curl"
                  aria-label="Signed cURL"
                  >{{ state.curl }}</pre
                >
                <div class="trigger-event-actions">
                  <button
                    type="button"
                    class="pane-generate"
                    (click)="copy(state.curl, 'the signed cURL')"
                  >
                    Copy cURL
                  </button>
                </div>
              }
            }
            @default {
              @if (state?.kind === "failed") {
                <p class="pane-empty-text trigger-event-problem" role="alert">
                  {{ state.message }}
                </p>
              } @else if (state?.kind === "received") {
                <p class="trigger-event-status" role="status">
                  Received a test event. Its run is open in Runs.
                </p>
              } @else {
                <!-- Listen and Stop listening are the header's: each shows once. -->
                <p class="pane-empty-text">
                  Select Listen for test event to get a test URL for this
                  webhook.
                </p>
              }
            }
          }
        }
        @if (production(); as url) {
          <label class="trigger-event-label" [attr.for]="productionId()"
            >Production URL</label
          >
          <div class="trigger-event-copy">
            <input
              class="trigger-event-url"
              readonly
              [id]="productionId()"
              [value]="url"
              [attr.aria-describedby]="productionId() + '-hint'"
            />
            <button
              type="button"
              class="pane-generate"
              (click)="copy(url, 'the production URL')"
            >
              Copy URL
            </button>
          </div>
          <p class="pane-empty-text" [id]="productionId() + '-hint'">
            Starts runs of the active version, signed with the production
            secret.
          </p>
        } @else if (unactivated(); as environment) {
          <p class="pane-empty-text">
            The production URL appears once you activate this workflow in
            {{ environment }}.
          </p>
        }
        @if (pasting()) {
          <label class="trigger-event-label" [attr.for]="fieldId()">{{
            label()
          }}</label>
          <textarea
            class="trigger-event-sample"
            rows="6"
            spellcheck="false"
            [id]="fieldId()"
            [value]="draft()"
            (input)="draft.set($any($event.target).value)"
          ></textarea>
          @if (problem()) {
            <p class="pane-empty-text trigger-event-problem" role="alert">
              {{ problem() }}
            </p>
          }
          <div class="trigger-event-actions">
            <button type="button" class="pane-generate" (click)="use()">
              Use this sample
            </button>
            <button type="button" class="pane-generate" (click)="cancel()">
              Cancel
            </button>
          </div>
        } @else {
          <button type="button" class="pane-generate" (click)="paste()">
            {{ offer() }}
          </button>
        }
        @if (sample() !== undefined) {
          <pre class="pane-json" aria-label="Test event">{{ shown() }}</pre>
        }
      </div>
    </div>`,
  styles: `
    .trigger-event {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: var(--space-3);
    }
    .trigger-event-label {
      font: var(--type-label);
    }
    .trigger-event-sample {
      box-sizing: border-box;
      width: 100%;
      min-height: calc(var(--sd-control) * 3);
      padding: var(--space-2) var(--space-3);
      font: var(--type-mono);
      color: var(--text);
      background: var(--surface);
      border: 1px solid var(--field-border);
      border-radius: var(--radius-sm);
      resize: vertical;
    }
    .trigger-event-actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .trigger-event-actions .pane-generate {
      margin-bottom: 0;
    }
    .trigger-event-problem {
      color: var(--danger-text, var(--danger));
    }
    .trigger-event-status {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      margin: 0;
      font: var(--type-label);
    }
    .trigger-event-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--live);
    }
    .trigger-event-copy {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
      width: 100%;
    }
    .trigger-event-url {
      flex: 1 1 16rem;
      min-width: 0;
      font: var(--type-mono);
    }
    .trigger-event-copy .pane-generate {
      margin-bottom: 0;
    }
    /* Commands sit in a sunken block, as Studio shows code elsewhere. */
    .trigger-event-code {
      box-sizing: border-box;
      display: block;
      margin: 0;
      padding: var(--space-2) var(--space-3);
      font: var(--type-mono);
      color: var(--text);
      background: var(--sunken);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .trigger-event-copy .trigger-event-code {
      flex: 1 1 16rem;
      min-width: 0;
    }
    .trigger-event-curl {
      width: 100%;
    }
  `,
})
export class TriggerEventPane {
  readonly session = input.required<FormSession>();
  readonly trigger = input.required<TriggerIntent>();
  /** The editor: where a webhook listens for its test event. */
  readonly host = input<StepDetailsHost | null>(null);
  readonly urlId = computed(() => `trigger-test-url-${this.trigger().id}`);
  readonly productionId = computed(
    () => `trigger-production-url-${this.trigger().id}`,
  );
  /** The created webhook's production URL in the environment shown, or null before it is activated there. */
  production(): string | null {
    const host = this.host();
    const trigger = this.trigger();
    if (trigger.kind !== "webhook" || !host?.profile) return null;
    const port = connectionsPort(host);
    const resources = trigger["resources"];
    const id =
      port && resources && typeof resources === "object"
        ? (resources as Record<string, unknown>)[port.environmentId]
        : null;
    return typeof id === "string" && id
      ? productionUrl(host.profile.baseUrl, id)
      : null;
  }
  /** The environment a webhook isn't activated in yet, when Studio is connected to one. */
  unactivated(): string | null {
    const host = this.host();
    const port = host ? connectionsPort(host) : null;
    return this.trigger().kind === "webhook" && port && !this.production()
      ? port.environment.name
      : null;
  }
  /** The command that sets the test secret's value on a local platform, or null. */
  testSecretCommand(): string | null {
    const handle = this.trigger()["testSecretRef"];
    return typeof handle === "string" ? secretCommand(handle) : null;
  }
  /** Why this webhook can't listen here, or null when it can. */
  private blocked(): string | null {
    const host = this.host();
    return listenBlocked(this.session().controller.environmentOf(), (role) =>
      host ? host.can(role) : false,
    );
  }
  /** A webhook connected to an environment with test calls on listens for its test event. */
  canListen(): boolean {
    return (
      this.trigger().kind === "webhook" &&
      !!this.host() &&
      // Reading a run, the sample is that run's input: it never goes into a cURL.
      !this.session().controller.viewingRuns() &&
      !this.blocked()
    );
  }
  /** What listening is doing for this trigger, or null. */
  listenState(): ListenState | null {
    const host = this.host();
    // Drawing the pane makes nothing: the listener is read once made.
    const state = host
      ? (connectionsPort(host)?.madeListener()?.state() ?? null)
      : null;
    return state?.triggerId === this.trigger().id ? state : null;
  }
  async copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      this.session().announce(`Copied ${what}.`);
    } catch {
      this.session().announce("Studio couldn't reach the clipboard.");
    }
  }
  readonly pasting = signal(false);
  readonly draft = signal("");
  readonly problem = signal("");

  private readonly noun = computed(
    () => SAMPLE_NOUNS[this.trigger().kind] ?? "payload",
  );
  readonly fieldId = computed(() => `trigger-sample-${this.trigger().id}`);
  readonly label = computed(() => `Sample ${this.noun()} (JSON)`);

  /** What the trigger can't do here, in one line; null when there's nothing to say. */
  note(): string | null {
    const kind = this.trigger().kind;
    if (kind === "webhook") return this.host() ? this.blocked() : LOCAL_NOTE;
    if (kind === "schedule")
      return "A schedule starts each run with its run input.";
    return null;
  }
  /** The pane's own button: Paste sample, or the trigger's Paste a sample … */
  offer(): string {
    const kind = this.trigger().kind;
    return kind === "webhook" || kind === "schedule"
      ? "Paste sample"
      : `Paste a sample ${this.noun()}`;
  }
  sample(): Json | undefined {
    return this.session().controller.testEvent();
  }
  shown(): string {
    return JSON.stringify(this.sample(), null, 2);
  }

  paste() {
    const current = this.sample();
    this.draft.set(
      current === undefined ? "" : JSON.stringify(current, null, 2),
    );
    this.problem.set("");
    this.pasting.set(true);
  }
  cancel() {
    this.pasting.set(false);
    this.problem.set("");
  }
  async use() {
    const session = this.session();
    let value: Json;
    try {
      value = JSON.parse(this.draft()) as Json;
    } catch {
      this.problem.set('Paste JSON, such as {"customerId": "123"}.');
      return;
    }
    const refused = await session.controller.setTestEvent(value);
    if (!session.isCurrent()) return;
    if (refused) {
      this.problem.set(refused);
      return;
    }
    this.pasting.set(false);
    this.problem.set("");
    session.announce("Pasted a sample test event.");
  }
}
