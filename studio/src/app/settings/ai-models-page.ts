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
// Settings › AI models: one place to connect models, check that AI steps can
// run, and set up Weave AI. The shell owns the AI connection sheet; this page
// asks it to open and reloads when an AI connection changes.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import type { Profile, StudioApi } from "../api";
import type { AiSheetRequest } from "../integrations/ai-connection-sheet";

/** What the page reads from the shell. */
export interface AiModelsHost {
  readonly api: StudioApi;
  readonly profile: Profile | null;
  can(capability: string, resource?: string): boolean;
  openAiConnectionDialog(request?: AiSheetRequest): void;
}

@Component({
  selector: "weave-ai-models-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `<p class="ai-intro">
      Connect a model once. Workflows and Weave AI use the connections you set
      up here.
    </p>
    <div class="action-row">
      <button
        type="button"
        (click)="host().openAiConnectionDialog({ card: 'ollama' })"
      >
        Connect Ollama
      </button>
      <button
        type="button"
        (click)="host().openAiConnectionDialog({ card: 'openai' })"
      >
        Connect OpenAI
      </button>
    </div>`,
})
export class AiModelsPage {
  host = input.required<AiModelsHost>();
}
