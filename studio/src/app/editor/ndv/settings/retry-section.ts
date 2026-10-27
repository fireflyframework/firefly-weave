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
// Retry on fail: a switch that lets the action run again after a temporary
// failure, then its tries and waits. On asks for three tries and off for
// one, keeping the waits; only the switch turns retry off, and the fields
// stay while focus is in them. Retry belongs to the action: an action this
// workflow owns keeps it, a published one shows it read-only with Open
// action to read it, and an action that changes data never retries.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import { stringify } from "yaml";
import { Modal } from "../../../dialog";
import { canonicalJson } from "../../../forms/core/json";
import { Icon } from "../../../icon";
import { isFrozen, recipeOf } from "../owned/owned-actions";
import type { FieldEntry } from "../params/form-model";
import type { FormSession } from "../params/form-session";
import { ParamField } from "../params/param-field";
import { ResourceViewerLayer } from "../params/resource-field";
import { fixed } from "../params/value-io";
import { allSteps } from "../step-walk";
import {
  retryProblem,
  retryShown,
  retrySwitched,
  savedInText,
} from "./settings-rules";

let sequence = 0;

@Component({
  selector: "weave-retry-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Modal, ParamField, ResourceViewerLayer],
  host: { "[attr.data-param]": "'retry'" },
  template: `@let reason = this.reason();
    @let line = this.line();
    @let saved = this.savedIn();
    <div class="param settings-retry" [class.is-readonly]="!!reason">
      <div class="param-control settings-switch-row">
        <div class="settings-switch-text">
          <span class="param-label" [id]="id + '-label'">{{
            spec().label
          }}</span>
          @if (spec().description) {
            <span class="settings-description" [id]="id + '-description'">{{
              spec().description
            }}</span>
          }
        </div>
        <button
          type="button"
          role="switch"
          class="param-switch"
          [attr.aria-checked]="on()"
          [attr.aria-labelledby]="id + '-label'"
          [attr.aria-describedby]="describedBy()"
          [disabled]="!!reason"
          (click)="toggle()"
        >
          <span class="param-switch-track" aria-hidden="true"></span>
        </button>
      </div>
      @if (fieldsShown()) {
        <div
          class="settings-retry-grid"
          (focusin)="editing.set(session())"
          (focusout)="left($event)"
        >
          @for (child of children(); track child.spec.id) {
            <weave-param-field
              [session]="session()"
              [spec]="child.spec"
              [entry]="child"
            />
          }
        </div>
      }
      @if (line) {
        <p
          class="param-line settings-retry-line"
          [id]="id + '-line'"
          [attr.data-kind]="line.kind"
          [attr.role]="line.kind === 'error' ? 'alert' : null"
        >
          @if (line.kind === "error") {
            <weave-icon name="warning" [size]="16" />
          }
          {{ line.text }}
          @if (line.kind === "reason" && published(); as uses) {
            <button
              type="button"
              class="text-link param-fix"
              (click)="open(uses)"
            >
              Open action
            </button>
            @if (canChange()) {
              <button
                type="button"
                class="text-link param-fix"
                (click)="changeInNewVersion()"
              >
                Change in a new version
              </button>
            }
          }
        </p>
      }
      @if (saved) {
        <p class="param-line settings-saved" [id]="id + '-saved'">
          <weave-icon name="info" [size]="16" />{{ saved }}
        </p>
      }
      @if (draftText(); as text) {
        <p class="param-line settings-saved" [id]="id + '-saved'">
          <weave-icon name="info" [size]="16" />{{ text }}
          <button
            type="button"
            class="text-link param-fix"
            (click)="publishDraft()"
          >
            Publish action
          </button>
        </p>
      }
      @if (problem(); as text) {
        <p class="param-line" data-kind="error" role="alert">
          <weave-icon name="warning" [size]="16" />{{ text }}
        </p>
      }
    </div>
    @if (viewer(); as view) {
      <weave-modal
        class="param-resource-viewer"
        weaveResourceViewerLayer
        [heading]="'View ' + view.uses"
        closeLabel="Close"
        [wide]="true"
        (dismiss)="close()"
      >
        <pre
          class="param-viewer"
          tabindex="0"
          [attr.aria-label]="view.uses + ' as YAML'"
          >{{ view.yaml }}</pre
        >
      </weave-modal>
    }`,
})
export class RetrySection {
  session = input.required<FormSession>();
  entry = input.required<FieldEntry>();
  readonly spec = computed(() => this.entry().spec);
  readonly id = `sd-retry-${++sequence}`;
  /** The form whose tries and waits have focus: they stay until it leaves. */
  readonly editing = signal<FormSession | null>(null);
  private view: {
    uses: string;
    yaml: string;
    current: () => boolean;
  } | null = null;

  private step() {
    const session = this.session();
    return session.controller.step(session.target);
  }
  reason(): string | null {
    return this.session().readOnly(this.entry());
  }
  private value(): unknown {
    const value = this.session().read(this.spec());
    return value.mode === "fixed" ? value.value : undefined;
  }
  /** On when the action may run more than once; never for an action that changes data. */
  on(): boolean {
    const step = this.step();
    return (
      !!step &&
      retryShown(step, this.session().controller.kindContext(), this.value())
    );
  }
  /** The tries and waits: while retry is on, and while focus is in them. */
  fieldsShown(): boolean {
    return this.on() || this.editing() === this.session();
  }
  left(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (!next || !(event.currentTarget as HTMLElement).contains(next))
      this.editing.set(null);
  }
  toggle() {
    this.session().write(
      this.spec(),
      fixed({ ...retrySwitched(this.value(), !this.on()) }),
    );
  }
  /** Max tries, Wait between tries and Longest wait; read-only with the section. */
  children(): FieldEntry[] {
    const reason = this.reason();
    return this.session()
      .children(this.spec())
      .shown.map((child) => ({
        ...child,
        readOnly: child.readOnly ?? reason,
      }));
  }
  /** The reason it can't change, a problem with the waits, or how the waits grow. */
  line(): { kind: "reason" | "error" | "hint"; text: string } | null {
    const reason = this.reason();
    if (reason) return { kind: "reason", text: reason };
    if (!this.on()) return null;
    const problem = retryProblem(this.value());
    if (problem) return { kind: "error", text: problem };
    const hint = this.spec().hint;
    return hint ? { kind: "hint", text: hint } : null;
  }
  /** Where an owned action keeps retry and timeout, and who shares it. */
  savedIn(): string | null {
    const step = this.step();
    const session = this.session();
    const recipe = step
      ? recipeOf(step, session.controller.kindContext())
      : null;
    if (!step || !recipe) return null;
    const uses = String(step["uses"] ?? "");
    const users = allSteps(session.host.model.definition.spec.steps).filter(
      (other) => other.kind === "action" && other["uses"] === uses,
    ).length;
    return savedInText(uses, recipe.kind, users, isFrozen(recipe));
  }
  describedBy(): string {
    return [
      this.spec().description ? `${this.id}-description` : "",
      this.line() ? `${this.id}-line` : "",
      this.savedIn() || this.draftText() ? `${this.id}-saved` : "",
    ]
      .filter(Boolean)
      .join(" ");
  }
  /** The published action whose retry this is, when Studio has its document. */
  published(): string | null {
    const step = this.step();
    const context = this.session().controller.kindContext();
    if (!step || recipeOf(step, context)) return null;
    const uses = String(step["uses"] ?? "");
    return uses && context.actionContract(uses) ? uses : null;
  }
  /** A published action that isn't a worker's: its retry and timeout change in a new version. */
  canChange(): boolean {
    const session = this.session();
    const step = this.step();
    if (!step || session.controller.readOnlyReason(session.target))
      return false;
    const context = session.controller.kindContext();
    const document = context.actionContract(String(step["uses"] ?? "")) as {
      spec?: { implementation?: { kind?: unknown } };
    } | null;
    return document?.spec?.implementation?.kind !== "worker";
  }
  readonly problem = signal<string | null>(null);
  async changeInNewVersion() {
    const session = this.session();
    this.problem.set(null);
    await session.controller.startActionDraft(session.target);
    session.host.refreshView();
  }
  /** Where a draft of the action's next version is kept, until Publish action. */
  draftText(): string | null {
    const step = this.step();
    if (!step) return null;
    const draft = this.session().controller.actionDraft(
      String(step["uses"] ?? ""),
    );
    return draft
      ? `Saved in ${draft.name} ${draft.version} on this computer. Publish it before publishing this workflow.`
      : null;
  }
  async publishDraft() {
    const session = this.session();
    const problem = await session.controller.publishActionDraft(session.target);
    if (session.isCurrent()) this.problem.set(problem);
  }
  open(uses: string) {
    const session = this.session();
    const document = session.controller.kindContext().actionContract(uses);
    if (!document) return;
    const original = canonicalJson(document);
    this.view = {
      uses,
      yaml: stringify(document),
      current: () =>
        this.session() === session &&
        session.isCurrent() &&
        canonicalJson(session.controller.kindContext().actionContract(uses)) ===
          original,
    };
  }
  viewer() {
    if (this.view && !this.view.current()) this.view = null;
    return this.view;
  }
  close() {
    this.view = null;
  }
}
