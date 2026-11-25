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
// Timeout, waits and deadlines), On error with Result on error, Notes,
// About this step. A section that doesn't apply to the step isn't shown;
// Notes and About always are.
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
import { OnErrorSection } from "./on-error-section";
import { RetrySection } from "./retry-section";

@Component({
  selector: "weave-settings-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [
    ParamField,
    RetrySection,
    OnErrorSection,
    NotesSection,
    AboutSection,
  ],
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
        } @else if (entry.spec.id === "onError") {
          <weave-on-error-section
            class="settings-section"
            [session]="session"
            [entry]="entry"
            [result]="resultOnError(state.fields)"
          />
        } @else if (entry.spec.id !== "resultOnError") {
          <weave-param-field
            class="settings-section"
            [session]="session"
            [spec]="entry.spec"
            [entry]="entry"
          />
        }
      }
      <weave-notes-section class="settings-section" [session]="session" />
      <weave-about-section class="settings-section" [session]="session" />
    </div>`,
})
export class SettingsTab {
  session = input.required<FormSession>();
  /** Keeps a field's component while its descriptor stays the same. */
  fieldKey(entry: FieldEntry): object {
    return this.session().fieldKey(entry);
  }
  /** Result on error, shown inside On error while the step continues with error output. */
  resultOnError(fields: readonly FieldEntry[]): FieldEntry | null {
    return fields.find((entry) => entry.spec.id === "resultOnError") ?? null;
  }
}
