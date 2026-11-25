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
// A platform event trigger's Test event: platform events come from
// transactions and test runs start no reactions, so the person builds a
// sample of one of the trigger's events, or pastes one. It becomes the
// workflow's test input, which Execute workflow starts from.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
} from "@angular/core";
import { Select, type SelectOption } from "../../forms/ui/select";
import { Icon } from "../../icon";
import type { FormSession } from "../ndv/params/form-session";
import type { Json } from "../ndv/registry";
import {
  eventSummary,
  pastedEvent,
  replacedNote,
  SHARED_NOTE,
  type ReplacedId,
} from "./event-sample";
import { chosenEvents, readDeclaration } from "./event-trigger";
import {
  currentPlatformEvent,
  EVENT_CHOICES,
  sampleEvent,
  type PlatformEventType,
} from "./platform-event";

let sequence = 0;
const TICK_MS = 1_000;

@Component({
  selector: "weave-event-sample",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Select],
  template: `@let wait = pause();
    @if (wait !== null) {
      <div class="es-callout" data-tone="danger">
        <weave-icon name="failCircle" [size]="16" />
        <div class="es-callout-text">
          <p role="alert">
            <strong>Studio couldn't read the platform event's fields.</strong>
            Later steps can't list them until it does.
          </p>
          <button
            type="button"
            class="sm"
            [disabled]="wait > 0"
            (click)="again()"
          >
            {{ wait > 0 ? "Try again in " + wait + " s" : "Try again" }}
          </button>
        </div>
      </div>
    }
    @if (viewingRuns()) {
      @if (summary() === null) {
        <p class="pane-empty-text">This run has no platform event to show.</p>
      }
    } @else {
      <p class="pane-empty-text">
        Platform events come from transactions, so Studio can't listen for one.
        Choose an event to build a sample.
      </p>
      @if (pasting()) {
        <label class="es-label" [attr.for]="id + '-paste'"
          >Sample event (JSON)</label
        >
        <textarea
          class="es-paste"
          rows="8"
          spellcheck="false"
          [id]="id + '-paste'"
          [value]="draft()"
          [attr.aria-invalid]="problem() ? 'true' : null"
          [attr.aria-describedby]="problem() ? id + '-problem' : null"
          (input)="draft.set($any($event.target).value)"
          (keydown.escape)="escape($event)"
        ></textarea>
        @if (problem()) {
          <p class="es-problem" role="alert" [id]="id + '-problem'">
            {{ problem() }}
          </p>
        }
        <div class="es-actions">
          <button type="button" class="pane-generate" (click)="use()">
            Use this sample
          </button>
          <button type="button" class="pane-generate" (click)="cancel()">
            Cancel
          </button>
        </div>
      } @else {
        @let events = options();
        <div class="es-field">
          <weave-select
            icon="chevronDown"
            label="Event"
            [options]="events"
            [value]="chosen() ?? ''"
            [disabled]="!events.length"
            [describedBy]="id + '-hint'"
            (choose)="pick($event)"
          />
          <p class="es-hint" [id]="id + '-hint'">
            {{
              events.length
                ? "Only the events this trigger reacts to."
                : "Choose at least one event first."
            }}
          </p>
        </div>
        <div class="es-actions">
          <button
            type="button"
            class="pane-generate es-generate"
            [disabled]="!events.length"
            (click)="generate()"
          >
            <weave-icon name="refresh" [size]="16" />{{
              sample() === undefined || pasted()
                ? "Generate sample"
                : "Generate again"
            }}
          </button>
          <button
            type="button"
            class="pane-generate es-offer"
            (click)="paste()"
          >
            <weave-icon name="upload" [size]="16" />Paste a sample event
          </button>
        </div>
        @if (problem()) {
          <p class="es-problem" role="alert">{{ problem() }}</p>
        }
        @if (summary()) {
          @if (pasted()) {
            <div class="es-callout" data-tone="info" role="note">
              <weave-icon name="info" [size]="16" />
              <p>
                @if (replaced(); as note) {
                  <strong>{{ note }}</strong>
                }
                {{ shared }}
              </p>
            </div>
          } @else {
            <p class="es-ready">
              <weave-icon name="check" [size]="16" />Sample ready. Execute
              workflow starts from it.
            </p>
          }
        }
      }
    }
    @if (!pasting() && summary(); as lines) {
      <div
        class="es-summary"
        role="group"
        [attr.aria-label]="summaryName()"
        tabindex="-1"
      >
        <dl>
          @for (line of lines; track line.label) {
            <div>
              <dt>{{ line.label }}</dt>
              <dd [attr.title]="line.text">{{ line.text }}</dd>
            </div>
          }
        </dl>
      </div>
    }`,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      align-items: stretch;
      gap: var(--space-3);
    }
    p {
      margin: 0;
    }
    .es-field {
      display: grid;
      gap: var(--space-1);
    }
    .es-label {
      font: var(--type-label);
    }
    .es-hint {
      font: var(--type-small);
      color: var(--muted);
    }
    .es-paste {
      box-sizing: border-box;
      width: 100%;
      min-height: calc(var(--sd-control) * 4);
      padding: var(--space-2) var(--space-3);
      font: var(--type-mono);
      color: var(--text);
      background: var(--sunken);
      border: 1px solid var(--field-border);
      border-radius: var(--radius-sm);
      resize: vertical;
    }
    .es-actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
    .es-actions .pane-generate {
      margin-bottom: 0;
    }
    .es-problem {
      font: var(--type-small);
      color: var(--danger-ink);
      overflow-wrap: anywhere;
    }
    .es-ready {
      display: flex;
      align-items: center;
      gap: var(--space-1);
      font: var(--type-body);
    }
    .es-ready weave-icon {
      flex: none;
      color: var(--success-ink);
    }
    .es-callout {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--info-bd);
      border-radius: var(--radius-sm);
      background: var(--info-bg);
      font: var(--type-small);
      color: var(--text);
    }
    .es-callout > weave-icon {
      flex: none;
      margin-top: 1px;
      color: var(--info-ink);
    }
    .es-callout strong {
      display: block;
    }
    .es-callout[data-tone="danger"] {
      border-color: var(--danger-bd);
      background: var(--danger-bg);
    }
    .es-callout[data-tone="danger"] > weave-icon {
      color: var(--danger-ink);
    }
    .es-callout-text {
      display: grid;
      justify-items: start;
      gap: var(--space-2);
      min-width: 0;
    }
    .es-summary {
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      overflow: hidden;
    }
    /* One line per fact, not the pages' two columns. */
    .es-summary dl {
      display: flex;
      flex-direction: column;
      gap: 0;
      margin: 0;
    }
    .es-summary dl > div {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-height: 32px;
      padding: 0 var(--space-3);
      font: var(--type-small);
    }
    .es-summary dl > div + div {
      border-top: 1px solid var(--line);
    }
    .es-summary dt {
      flex: none;
      width: 92px;
      color: var(--muted);
    }
    .es-summary dd {
      min-width: 0;
      margin: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font: var(--type-label);
    }
  `,
})
export class EventSample {
  readonly session = input.required<FormSession>();
  readonly id = `event-sample-${++sequence}`;
  readonly shared = SHARED_NOTE;
  readonly pasting = signal(false);
  readonly draft = signal("");
  readonly problem = signal("");
  /** The event chosen here; another one when the trigger no longer reacts to it. */
  private readonly picked = signal<PlatformEventType | null>(null);
  /** What the last paste replaced, for the event it became. */
  private readonly swapped = signal<{
    value: Json | undefined;
    ids: ReplacedId[];
  } | null>(null);
  /** The clock the countdown to the next read reads. */
  private readonly now = signal(Date.now());
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  constructor() {
    const timer = setInterval(() => {
      if (this.unavailable() !== null) this.now.set(Date.now());
    }, TICK_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  // ------------------------------------------------------- the event's fields
  private unavailable(): number | null {
    const view = currentPlatformEvent(this.session().host.api);
    return view?.state === "unavailable" ? view.retryAt : null;
  }
  /** The seconds until Studio may ask for the fields again; null when it has them. */
  pause(): number | null {
    const retryAt = this.unavailable();
    // Drawn again each second; the time left is the clock's, not the tick's.
    this.now();
    return retryAt === null
      ? null
      : Math.max(0, Math.ceil((retryAt - Date.now()) / 1_000));
  }
  again() {
    this.now.set(Date.now());
  }

  // ------------------------------------------------------------- the sample
  viewingRuns(): boolean {
    return this.session().controller.viewingRuns();
  }
  private events(): PlatformEventType[] {
    const declared = readDeclaration(this.session().host.model.definition);
    return declared ? chosenEvents(declared) : [];
  }
  options(): SelectOption[] {
    return this.events().map((type) => ({
      value: type,
      label: EVENT_CHOICES[type].label,
    }));
  }
  chosen(): PlatformEventType | undefined {
    const events = this.events();
    const picked = this.picked();
    return picked && events.includes(picked) ? picked : events[0];
  }
  pick(value: string) {
    this.picked.set(value as PlatformEventType);
  }
  sample(): Json | undefined {
    return this.session().controller.testEvent();
  }
  pasted(): boolean {
    return this.session().controller.testEventSource() === "manual";
  }
  summary() {
    return eventSummary(this.sample());
  }
  summaryName(): string {
    return this.viewingRuns() ? "The run's event" : "The test event";
  }
  /** What the paste that made this test event replaced; "" for one pasted earlier. */
  replaced(): string {
    const last = this.swapped();
    return last && JSON.stringify(last.value) === JSON.stringify(this.sample())
      ? replacedNote(last.ids)
      : "";
  }
  private async keep(
    value: Json,
    source: "manual" | "schema",
  ): Promise<boolean> {
    const session = this.session();
    const refused = await session.controller.setTestEvent(value, source);
    if (!session.isCurrent()) return false;
    this.problem.set(refused ?? "");
    return !refused;
  }
  async generate() {
    const type = this.chosen();
    if (!type) return;
    const session = this.session();
    const declared = readDeclaration(session.host.model.definition);
    const value = sampleEvent(type, {
      source: declared?.from?.[0],
      now: new Date(),
      id: () => crypto.randomUUID(),
    }) as Json;
    if (await this.keep(value, "schema")) session.announce("Sample ready.");
  }
  paste() {
    this.problem.set("");
    this.draft.set("");
    this.pasting.set(true);
    this.focus(".es-paste");
  }
  cancel() {
    this.pasting.set(false);
    this.problem.set("");
    this.focus(".es-offer");
  }
  /** Escape leaves the paste; it would close step details. */
  escape(event: Event) {
    event.preventDefault();
    event.stopPropagation();
    this.cancel();
  }
  async use() {
    const read = pastedEvent(this.draft(), () => crypto.randomUUID());
    if ("problem" in read) {
      this.problem.set(read.problem);
      return;
    }
    const session = this.session();
    if (!(await this.keep(read.value, "manual"))) return;
    this.swapped.set({ value: this.sample(), ids: read.replaced });
    this.pasting.set(false);
    session.announce(
      [replacedNote(read.replaced), SHARED_NOTE].filter(Boolean).join(" "),
    );
    this.focus(".es-summary");
  }
  private focus(selector: string) {
    afterNextRender(
      () =>
        this.element.nativeElement
          .querySelector<HTMLElement>(selector)
          ?.focus(),
      { injector: this.injector },
    );
  }
}
