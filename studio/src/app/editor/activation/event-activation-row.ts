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
// A platform event trigger's row in the activation dialog: what Studio does
// and what it means, or after the activation what happened, with the row's
// own controls (Stop it, Retry, or the two answers before a stop that would
// skip waiting events).
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../icon";
import type { EventRowView } from "./event-rows";

export type EventRowAnswer = "retry" | "keep" | "stop";

@Component({
  selector: "weave-event-activation-row",
  standalone: true,
  imports: [Icon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { "[attr.data-tone]": "view().tone" },
  template: `
    @if (view().icon; as icon) {
      <weave-icon class="row-icon" [name]="icon" size="16" />
    }
    <div class="row-text" [id]="textId()">
      <div class="row-lead">{{ view().lead }}</div>
      @if (view().detail; as detail) {
        <div class="row-detail">{{ detail }}</div>
      }
      @if (view().resolves; as resolves) {
        <div class="row-resolves">
          <weave-icon name="lock" size="16" /><span>{{ resolves }}</span>
        </div>
      }
      @if (view().compare; as compare) {
        <dl class="row-compare">
          <dt>Now</dt>
          <dd>{{ compare.now }}</dd>
          <dt>{{ compare.version }}</dt>
          <dd [class.wider]="compare.wider">
            @if (compare.wider) {
              <weave-icon name="warning" size="16" />
            }
            <span>{{ compare.next }}</span>
          </dd>
        </dl>
      }
    </div>
    @if (view().stop !== null) {
      <label class="activation-stop-it">
        <input
          type="checkbox"
          [checked]="view().stop"
          [attr.aria-describedby]="textId()"
          (change)="stop.emit($any($event.target).checked)"
        />Stop it
      </label>
    }
    @if (view().action; as action) {
      <div class="row-actions">
        @if (action === "choose") {
          <button
            type="button"
            class="sm"
            data-ask
            [attr.aria-disabled]="busy() || null"
            [attr.aria-describedby]="textId()"
            (click)="say('keep')"
          >
            Keep it for now
          </button>
          <button
            type="button"
            class="sm"
            [attr.aria-disabled]="busy() || null"
            [attr.aria-describedby]="textId()"
            (click)="say('stop')"
          >
            Stop now
          </button>
        } @else {
          <button
            type="button"
            class="sm"
            [attr.aria-disabled]="busy() || null"
            [attr.aria-describedby]="textId()"
            (click)="say('retry')"
          >
            {{ action === "stop" ? "Stop it" : "Retry" }}
          </button>
        }
      </div>
    }
  `,
  styles: `
    :host {
      display: flex;
      flex: 1;
      align-items: flex-start;
      gap: var(--space-3);
      min-width: 0;
      color: var(--text);
    }
    :host([data-tone="muted"]) .row-lead,
    :host([data-tone="muted"]) .row-icon {
      color: var(--muted);
    }
    :host([data-tone="warning"]) .row-lead,
    :host([data-tone="warning"]) .row-icon {
      color: var(--warning-ink);
    }
    :host([data-tone="danger"]) .row-lead,
    :host([data-tone="danger"]) .row-icon {
      color: var(--danger-ink);
    }
    :host([data-tone="success"]) .row-lead,
    :host([data-tone="success"]) .row-icon {
      color: var(--success-ink);
    }
    .row-icon {
      flex: none;
      margin-top: 2px;
    }
    .row-text {
      display: flex;
      flex: 1;
      flex-direction: column;
      gap: var(--space-1);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .row-lead {
      font: var(--type-body);
    }
    .row-detail {
      color: var(--muted);
      font: var(--type-small);
    }
    .row-resolves,
    .row-compare dd {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      min-width: 0;
      font: var(--type-small);
    }
    .row-resolves weave-icon,
    .row-compare weave-icon {
      flex: none;
      margin-top: 1px;
    }
    .row-resolves weave-icon {
      color: var(--muted);
    }
    .row-compare {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 2px var(--space-3);
      margin: var(--space-1) 0 0;
    }
    .row-compare dt {
      margin: 0;
      color: var(--muted);
      font: var(--type-caption);
      line-height: 20px;
    }
    .row-compare dd {
      margin: 0;
    }
    .row-compare dd.wider {
      color: var(--warning-ink);
    }
    .activation-stop-it,
    .row-actions {
      display: inline-flex;
      flex: none;
      align-items: center;
      align-self: center;
      gap: var(--space-2);
    }
    .activation-stop-it {
      min-height: var(--control-touch, 44px);
      margin: 0;
      font: var(--type-body);
      white-space: nowrap;
    }
    .activation-stop-it input {
      flex: none;
      width: 16px;
      height: 16px;
      margin: 0;
    }
    .row-actions button {
      min-height: 32px;
    }
    /* On a phone the controls go under the text, the answers side by side. */
    @media (max-width: 767px) {
      :host {
        flex-wrap: wrap;
      }
      .row-icon + .row-text {
        flex-basis: calc(100% - 16px - var(--space-3));
      }
      .row-text {
        flex-basis: 100%;
      }
      .activation-stop-it,
      .row-actions {
        flex-basis: 100%;
      }
      .row-icon ~ .activation-stop-it,
      .row-icon ~ .row-actions {
        padding-left: calc(16px + var(--space-3));
      }
      .row-actions button {
        flex: 1;
      }
      .row-compare {
        grid-template-columns: 1fr;
        row-gap: 0;
      }
      .row-compare dd {
        margin-bottom: 6px;
      }
    }
    @media (max-width: 767px), (pointer: coarse) {
      .row-actions button {
        min-height: var(--control-touch, 44px);
      }
    }
  `,
})
export class EventActivationRow {
  readonly view = input.required<EventRowView>();
  /** The ID of the row's text, which describes its controls. */
  readonly textId = input.required<string>();
  /** True while something runs: the answers stay in place but do nothing. */
  readonly busy = input(false);
  /** The person ticked or unticked Stop it. */
  readonly stop = output<boolean>();
  readonly answer = output<EventRowAnswer>();

  say(answer: EventRowAnswer) {
    if (!this.busy()) this.answer.emit(answer);
  }
}
