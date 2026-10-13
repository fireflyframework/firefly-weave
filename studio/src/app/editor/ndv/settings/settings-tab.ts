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
// The Settings tab, in a fixed order: the kind's settings (Retry on fail,
// Timeout, waits and deadlines), On error, Notes, About this step. A section
// that doesn't apply to the step isn't shown; Notes and About always are.
import {
  ChangeDetectionStrategy,
  Component,
  ViewEncapsulation,
  input,
} from "@angular/core";
import type { FieldEntry } from "../params/form-model";
import type { FormSession } from "../params/form-session";
import { ParamField } from "../params/param-field";
import { AboutSection } from "./about-section";
import { NotesSection } from "./notes-section";
import { RetrySection } from "./retry-section";
import { STOP_ONLY_REASON, onErrorText } from "./settings-rules";

let sequence = 0;

@Component({
  selector: "weave-settings-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [ParamField, RetrySection, NotesSection, AboutSection],
  styleUrl: "./settings.css",
  template: `@let session = this.session();
    @let state = session.state("settings");
    <div class="param-form settings-form">
      @for (entry of state.fields; track fieldKey(entry)) {
        @if (entry.spec.id === "retry") {
          <weave-retry-section
            class="settings-section"
            [session]="session"
            [entry]="entry"
          />
        } @else {
          <weave-param-field
            class="settings-section"
            [session]="session"
            [spec]="entry.spec"
            [entry]="entry"
          />
        }
      }
      @if (onError(); as reason) {
        <div class="settings-section" data-param="onError">
          <div class="param settings-on-error">
            <div class="param-label-row">
              <span class="param-label" [id]="id + '-label'">On error</span>
            </div>
            <div
              class="param-control settings-choices"
              role="radiogroup"
              [attr.aria-labelledby]="id + '-label'"
              [attr.aria-describedby]="
                reason === stopOnly ? null : id + '-line'
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
            @if (reason !== stopOnly) {
              <p class="param-line" data-kind="reason" [id]="id + '-line'">
                {{ reason }}
              </p>
            }
          </div>
        </div>
      }
      <weave-notes-section class="settings-section" [session]="session" />
      <weave-about-section class="settings-section" [session]="session" />
    </div>`,
})
export class SettingsTab {
  session = input.required<FormSession>();
  readonly id = `sd-on-error-${++sequence}`;
  readonly stopOnly = STOP_ONLY_REASON;
  /** Keeps a field's component while its descriptor stays the same. */
  fieldKey(entry: FieldEntry): object {
    return this.session().fieldKey(entry);
  }
  /** Why On error stays "Stop the run" for this step, or null when it has none. */
  onError(): string | null {
    const session = this.session();
    const step = session.controller.step(session.target);
    return step ? onErrorText(step.kind) : null;
  }
}
