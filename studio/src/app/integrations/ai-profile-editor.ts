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
import { Component, input, output } from "@angular/core";
import { TaskForm } from "../task-form";
import type { Schema } from "../task-schema";

/** Uses the host's canonical contract for both workflow and assistant profiles. */
@Component({
  selector: "weave-ai-profile-editor",
  standalone: true,
  imports: [TaskForm],
  template: `
    <p class="hint">
      Choose a provider and model. For Azure OpenAI, enter the deployment name.
      Generation and reasoning limits apply to each AI request; credentials
      belong to the connection in the next step.
    </p>
    <weave-task-form
      [schema]="schema()"
      [initialData]="initialData()"
      (dataChange)="dataChange.emit($event)"
      (validityChange)="validityChange.emit($event)"
    />
  `,
  styles: `
    :host {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .hint {
      margin: 0;
    }
  `,
})
export class AiProfileEditor {
  schema = input.required<Schema>();
  initialData = input.required<Record<string, unknown>>();
  dataChange = output<Record<string, unknown>>();
  validityChange = output<boolean>();
}
