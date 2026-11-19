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
// What a trigger choice's Parameters show above its fields: a schedule's
// calendar builder, who may call a workflow Called by a workflow, and for an
// email trigger the question asked when the
// workflow's input doesn't take the received message. The message's schema
// comes from the Studio host, which serves the platform's own.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from "@angular/core";
import type { StudioApi } from "../../api";
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import type { FormSession } from "../ndv/params/form-session";
import type { TriggerIntent } from "../state/canvas-sidecar";
import { CalledByTrigger } from "./called-by-trigger";
import { ScheduleBuilder } from "./schedule-builder";
import { inputFit } from "./trigger-forms";

const schemas = new WeakMap<object, Promise<JsonObject | null>>();

/** The received email message's schema, read once per Studio connection; null when the host can't say. */
function emailMessageSchema(
  api: Pick<StudioApi, "request">,
): Promise<JsonObject | null> {
  let kept = schemas.get(api);
  if (!kept) {
    kept = api
      .request<unknown>("/studio/contracts/email-message")
      .then((value) => (isJsonObject(value) ? value : null))
      .catch(() => null);
    schemas.set(api, kept);
  }
  return kept;
}

@Component({
  selector: "weave-trigger-setup",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [CalledByTrigger, ScheduleBuilder],
  template: `@if (trigger().kind === "schedule") {
      <weave-schedule-builder [session]="session()" [trigger]="trigger()" />
    } @else if (trigger().kind === "called") {
      <weave-called-by-trigger [session]="session()" />
    }
    @if (question(); as asked) {
      <!-- A question waiting for an answer: a warning notice, Replace first. -->
      <div class="notice trigger-question" role="group" aria-label="Input">
        <p>{{ asked }}</p>
        <div class="trigger-question-actions">
          <button type="button" class="sm primary" (click)="replace()">
            Replace
          </button>
          <button type="button" class="sm" (click)="dismissed.set(true)">
            Cancel
          </button>
        </div>
      </div>
    }`,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      gap: var(--sd-field-gap, var(--space-4));
    }
    :host:not(:empty) {
      margin-bottom: var(--sd-field-gap, var(--space-4));
    }
    .trigger-question p {
      margin: 0 0 var(--space-2);
    }
    .trigger-question-actions {
      display: flex;
      flex-wrap: wrap;
      gap: var(--space-2);
    }
  `,
})
export class TriggerSetup {
  readonly session = input.required<FormSession>();
  readonly trigger = input.required<TriggerIntent>();
  readonly dismissed = signal(false);
  private readonly message = signal<JsonObject | null>(null);
  private asked: object | null = null;

  /** The question for an email trigger whose workflow input doesn't take a received message. */
  question(): string | null {
    if (this.trigger().kind !== "email" || this.dismissed()) return null;
    const api = this.session().host.api;
    if (this.asked !== api) {
      this.asked = api;
      void emailMessageSchema(api).then((schema) => this.message.set(schema));
    }
    const message = this.message();
    if (!message) return null;
    const workflowInput =
      this.session().host.model.definition.spec["inputSchema"];
    const fit = inputFit(
      "email",
      isJsonObject(workflowInput) ? workflowInput : {},
      message,
    );
    return fit.kind === "fits"
      ? null
      : "The email message doesn't fit this workflow's input. Replace the input with the email message?";
  }
  replace() {
    const message = this.message();
    if (!message) return;
    const session = this.session();
    const kept = session.commit(
      [
        {
          scope: "workflow",
          path: ["spec", "inputSchema"],
          value: structuredClone(message),
        },
      ],
      "input",
    );
    if (kept) session.announce("Replaced the input with the email message.");
  }
}
