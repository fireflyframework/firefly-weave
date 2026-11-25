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
// On error: the choice of what a step's failure does, with the chosen
// value's description under it and what the step's kind adds (when it
// applies, what it can't change). Continue with error output shows Result
// on error under it. A choice writes the setting and its error path as one
// undo step; leaving Continue with error output removes the path, and a
// toast names the steps that went with it. Where the step or the platform
// can't continue after errors, "Stop the run" stays read-only with why.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Select, type SelectOption } from "../../../forms/ui/select";
import { Icon } from "../../../icon";
import { memo } from "../../memo";
import type { FieldEntry } from "../params/form-model";
import type { FormSession } from "../params/form-session";
import { ParamField } from "../params/param-field";
import {
  onErrorChoices,
  onErrorRow,
  removedPathToast,
  writeOnError,
  type OnErrorChoice,
  type OnErrorRow,
} from "./on-error";
import { ERROR_PATH } from "../../step-blocks";
import { STOP_ONLY_REASON } from "./settings-rules";

let sequence = 0;
const NO_CHOICES: readonly OnErrorChoice[] = [];

@Component({
  selector: "weave-on-error-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, ParamField, Select],
  host: { "[attr.data-param]": "'onError'" },
  template: `@let row = this.row();
    @if (row?.kind === "choices") {
      @let line = this.line();
      <div class="param settings-on-error" [class.is-readonly]="!!reason()">
        <div class="param-label-row">
          <label class="param-label" [for]="id">On error</label>
        </div>
        <div class="param-control">
          <weave-select
            class="param-select"
            icon="chevronDown"
            label="On error"
            sheetTitle="On error"
            [hideLabel]="true"
            [controlId]="id"
            [options]="options()"
            [value]="row.value"
            [disabled]="!!reason()"
            [invalid]="line?.kind === 'error'"
            [describedBy]="describedBy(row)"
            (choose)="choose($event)"
          />
        </div>
        <p
          class="param-line settings-on-error-description"
          [id]="id + '-description'"
        >
          {{ description(row) }}
        </p>
        @for (hint of row.hints; track hint) {
          <p
            class="param-line settings-on-error-hint"
            [id]="id + '-hint-' + $index"
          >
            <weave-icon name="info" [size]="16" />{{ hint }}
          </p>
        }
        @if (line) {
          <p
            class="param-line"
            [id]="id + '-line'"
            [attr.data-kind]="line.kind"
            [attr.role]="line.kind === 'error' ? 'alert' : null"
          >
            @if (line.kind !== "reason") {
              <weave-icon name="warning" [size]="16" />
            }
            {{ line.text }}
          </p>
        }
        @if (result(); as entry) {
          <div class="settings-on-error-result">
            <weave-param-field
              [session]="session()"
              [spec]="entry.spec"
              [entry]="entry"
            />
          </div>
        }
      </div>
    } @else if (row) {
      <div class="param settings-on-error">
        <div class="param-label-row">
          <span class="param-label" [id]="id + '-label'">On error</span>
        </div>
        <div
          class="param-control settings-choices"
          role="radiogroup"
          [attr.aria-labelledby]="id + '-label'"
          [attr.aria-describedby]="
            row.reason === stopOnly ? null : id + '-line'
          "
        >
          <label class="settings-choice">
            <input
              type="radio"
              [name]="id"
              checked
              disabled
              [attr.aria-labelledby]="id + '-stop'"
              [attr.aria-describedby]="id + '-stop-description'"
            />
            <span class="settings-choice-text">
              <span class="settings-choice-label" [id]="id + '-stop'"
                >Stop the run</span
              >
              <span
                class="settings-choice-description"
                [id]="id + '-stop-description'"
                >{{ stopOnly }}</span
              >
            </span>
          </label>
        </div>
        @if (row.reason !== stopOnly) {
          <p class="param-line" data-kind="reason" [id]="id + '-line'">
            {{ row.reason }}
          </p>
        }
      </div>
    }`,
})
export class OnErrorSection {
  session = input.required<FormSession>();
  entry = input.required<FieldEntry>();
  /** Result on error, while the step continues with error output. */
  result = input<FieldEntry | null>(null);
  readonly id = `sd-on-error-${++sequence}`;
  readonly stopOnly = STOP_ONLY_REASON;

  private step() {
    const session = this.session();
    return session.controller.step(session.target);
  }
  private choices(): readonly OnErrorChoice[] {
    const step = this.step();
    return step ? onErrorChoices(step) : NO_CHOICES;
  }
  /** The same options while the step's kind is: an open list keeps its place. */
  private readonly optionsOf = memo((choices: readonly OnErrorChoice[]) =>
    choices.map(
      ({ value, label, description }): SelectOption => ({
        value,
        label,
        description,
      }),
    ),
  );
  options(): SelectOption[] {
    return this.optionsOf(this.choices());
  }
  row(): OnErrorRow {
    const step = this.step();
    return step
      ? onErrorRow(step, this.session().controller.onErrorContext())
      : null;
  }
  /** Why the choice can't change now (a run is shown, a simulation runs). */
  reason(): string | null {
    return this.session().readOnly(this.entry());
  }
  /** What the chosen value does, said right under it. */
  description(row: Extract<OnErrorRow, { kind: "choices" }>): string {
    return (
      this.choices().find((choice) => choice.value === row.value)
        ?.description ?? ""
    );
  }
  /** Why it can't change, or what the compiler found at the setting. */
  line(): { kind: string; text: string } | null {
    const reason = this.reason();
    if (reason) return { kind: "reason", text: reason };
    const line = this.session().line(this.entry().spec, this.entry());
    return line.kind === "error" || line.kind === "warning" ? line : null;
  }
  describedBy(row: Extract<OnErrorRow, { kind: "choices" }>): string {
    return [
      `${this.id}-description`,
      ...row.hints.map((_, index) => `${this.id}-hint-${index}`),
      this.line() ? `${this.id}-line` : "",
    ]
      .filter(Boolean)
      .join(" ");
  }
  /**
   * Writes the choice and what goes with it as one undo step. A path that
   * held steps goes with a toast that names them and offers Undo.
   */
  choose(value: string) {
    const session = this.session();
    const step = this.step();
    const choice = this.choices().find((item) => item.value === value);
    if (!step || !choice || this.reason()) return;
    const write = writeOnError(step, choice.value);
    const changes = write.patch.map((edit) => ({
      scope: "step" as const,
      path: edit.path,
      value: edit.value,
    }));
    if (!session.commit(changes, "onError")) return;
    // A new error path's result starts null; the data picker is open on it,
    // since later steps usually read what the path made.
    const added = write.patch.some((edit) => edit.path[0] === ERROR_PATH);
    const result = session
      .state("settings")
      .fields.find((entry) => entry.spec.id === "resultOnError");
    if (added && result) void session.setMode(result.spec, "mapped");
    const toast = removedPathToast(write.removed, write.names);
    if (!toast) return;
    const host = session.host;
    const model = host.model;
    const revision = model.revision;
    const opened = model.opened;
    host.notify(
      toast.text,
      {
        label: "Undo",
        run: () =>
          void host.undoIfUnchanged(
            () =>
              host.model === model &&
              model.opened === opened &&
              model.revision === revision,
          ),
      },
      "neutral",
      toast.detail,
    );
  }
}
