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
  initial: Record<string, unknown>;
  wrapped: boolean;
  current: () => boolean;
}
@Component({
  selector: "weave-test-event-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, TaskForm],
  template: `<div class="pane-sub">
      <span class="pane-count"
        >Sample input stays in memory while this workflow is open.</span
      >
    </div>
    <div class="pane-content">
      <button type="button" class="pane-generate" (click)="generate()">
        <weave-icon name="refresh" [size]="16" />Generate sample
      </button>
      @if (problem()) {
        <p class="pane-empty-text" role="alert">{{ problem() }}</p>
      }
      @for (form of forms(); track form) {
        <weave-task-form
          [schema]="form.schema"
          [initialData]="form.initial"
          (dataChange)="changed($event, form)"
        />
      }
    </div>`,
})
export class TestEventPane {
  session = input.required<FormSession>();
  readonly problem = signal("");
  private readonly lifetime = inject(DestroyRef);
  private form: SampleForm | null = null;
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
  generate(): void {
    const form = this.forms()[0];
    if (!this.owns(form)) return;
    try {
      const sample = sampleFromSchema(this.schema());
      if (!this.owns(form)) return;
      form.session.controller.setTestEvent(sample);
      this.form = null;
      this.forms();
      this.problem.set("");
      form.session.announce("Generated a sample test event.");
    } catch (error) {
      if (this.owns(form)) this.problem.set((error as Error).message);
    }
  }
  changed(data: Record<string, unknown>, form: SampleForm): void {
    if (!this.owns(form)) return;
    form.session.controller.setTestEvent(
      (form.wrapped ? data["value"] : data) as Json | undefined,
    );
  }
}
