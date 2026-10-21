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
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
  signal,
  untracked,
} from "@angular/core";
import { Icon } from "../../../icon";
import { TaskForm } from "../../../task-form";
import type { Schema } from "../../../task-schema";
import { canonicalJson } from "../../../forms/core/json";
import { resolveSchema } from "../../../forms/core/resolve";
import type { FormSession } from "../params/form-session";
import type { Json } from "../registry";
import { sampleFromSchema } from "./sample";

interface SampleForm {
  session: FormSession;
  signature: string;
  schema: Schema;
  root: unknown;
  initial: Record<string, unknown>;
  wrapped: boolean;
  current: () => boolean;
}
@Component({
  selector: "weave-test-event-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, TaskForm],
  template: `<header class="sd-pane-head">
      <h3 class="sd-pane-title">Test event</h3>
    </header>
    <div class="pane-sub">
      <span class="pane-count">{{ kept() }}</span>
    </div>
    <div class="sd-pane-body">
      <div class="pane-content">
        <button type="button" class="pane-generate" (click)="generate()">
          <weave-icon name="refresh" [size]="16" />Generate sample
        </button>
        @if (problem()) {
          <p class="pane-empty-text" role="alert">{{ problem() }}</p>
        }
        @for (form of forms(); track form) {
          <weave-task-form
            choiceIcon="chevronDown"
            [schema]="form.schema"
            [schemaRoot]="form.root"
            [initialData]="form.initial"
            (dataChange)="changed($event, form)"
          />
        }
      </div>
    </div>`,
})
export class TestEventPane {
  session = input.required<FormSession>();
  readonly problem = signal("");
  private readonly lifetime = inject(DestroyRef);
  private form: SampleForm | null = null;
  /** Where the test input is kept: with the workflow, or only while it is open (no account owns it). */
  kept(): string {
    return this.session().controller.testData.place().kind === "memory"
      ? "Test input is kept until you close this workflow."
      : "Test input is saved with this workflow.";
  }
  private schema(): unknown {
    return this.session().host.model.definition.spec["inputSchema"] ?? {};
  }
  forms(): SampleForm[] {
    const session = this.session();
    const signature = canonicalJson(this.schema());
    if (
      !this.form ||
      this.form.session !== session ||
      this.form.signature !== signature ||
      !this.form.current()
    ) {
      const raw = structuredClone(this.schema());
      const resolved = resolveSchema(raw);
      const wrapped =
        resolved.schema["type"] !== "object" && !resolved.schema["properties"];
      const event = session.controller.testEvent();
      const initial = wrapped
        ? event === undefined
          ? {}
          : { value: event }
        : event && typeof event === "object" && !Array.isArray(event)
          ? event
          : {};
      this.form = {
        session,
        signature,
        schema: (wrapped
          ? { type: "object", properties: { value: raw } }
          : raw) as Schema,
        root: raw,
        initial,
        wrapped,
        current: session.controller.testEventOwner(),
      };
      untracked(() => this.problem.set(""));
    }
    return [this.form];
  }
  private owns(form: SampleForm): boolean {
    return (
      !this.lifetime.destroyed &&
      this.form === form &&
      this.session() === form.session &&
      form.session.isCurrent() &&
      form.current() &&
      canonicalJson(this.schema()) === form.signature
    );
  }
  async generate(): Promise<void> {
    const form = this.forms()[0];
    if (!this.owns(form)) return;
    try {
      const sample = sampleFromSchema(this.schema());
      if (!this.owns(form)) return;
      // The form on show is replaced: its own late writes (defaults) don't count.
      this.form = null;
      const next = this.forms()[0];
      const refused = await form.session.controller.setTestEvent(
        sample as Json,
        "schema",
      );
      if (!this.owns(next)) return;
      if (refused) {
        this.problem.set(refused);
        return;
      }
      this.form = null;
      this.forms();
      this.problem.set("");
      form.session.announce("Generated a sample test event.");
    } catch (error) {
      if (this.owns(form)) this.problem.set((error as Error).message);
    }
  }
  async changed(data: Record<string, unknown>, form: SampleForm) {
    if (!this.owns(form)) return;
    const refused = await form.session.controller.setTestEvent(
      (form.wrapped ? data["value"] : data) as Json | undefined,
    );
    if (this.owns(form)) this.problem.set(refused ?? "");
  }
}
