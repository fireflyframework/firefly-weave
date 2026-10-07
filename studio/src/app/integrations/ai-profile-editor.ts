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
  Component,
  ChangeDetectorRef,
  OnChanges,
  OnDestroy,
  SimpleChanges,
  input,
  inject,
  output,
} from "@angular/core";
import { TaskForm } from "../task-form";
import type { ProfileValidation } from "../api";
import type { Schema } from "../task-schema";
import { missingData } from "../forms/core/form-model";
import { isRecord, type Data } from "../task-schema";
import {
  aiProfileSections,
  mergeProfileSection,
  profileSectionData,
  type ProfileSection,
} from "./ai-profile-presentation";

/** Uses the host's canonical contract for both workflow and assistant profiles. */
@Component({
  selector: "weave-ai-profile-editor",
  standalone: true,
  imports: [TaskForm],
  template: `
    <p class="hint">
      Choose a provider and model. For Azure OpenAI, enter the deployment name.
      Use a model approved for this environment. Credentials belong to the
      connection in the next step.
    </p>
    <weave-task-form
      [schema]="basic.schema"
      [initialData]="basicInitial"
      (dataChange)="change('basic', $event)"
      (validityChange)="setValidity('basic', $event)"
    />
    <details class="advanced">
      <summary>
        <span>Advanced model settings</span>
        @if (!advancedValid) {
          <span class="error"> · Needs attention</span>
        }
      </summary>
      <p class="hint">
        Optional generation controls, reasoning strategy and execution limits.
        Leave optional values empty to use the provider's behavior. Available
        options depend on the selected model.
      </p>
      <weave-task-form
        [schema]="advanced.schema"
        [initialData]="advancedInitial"
        (dataChange)="change('advanced', $event)"
        (validityChange)="setValidity('advanced', $event)"
      />
    </details>
    @if (checking) {
      <p class="hint" role="status">Checking model settings…</p>
    }
    @for (message of validationMessages; track $index) {
      <p class="error" role="alert">{{ message }}</p>
    }
    @if (validationFailed) {
      <button type="button" (click)="validate()">Retry model validation</button>
    }
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
    .advanced {
      border-top: 1px solid var(--line);
    }
    summary {
      cursor: pointer;
      padding: 12px 0;
    }
    .advanced > .hint {
      margin-bottom: 12px;
    }
  `,
})
export class AiProfileEditor implements OnChanges, OnDestroy {
  schema = input.required<Schema>();
  initialData = input.required<Record<string, unknown>>();
  dataChange = output<Record<string, unknown>>();
  validityChange = output<boolean>();
  validateProfile = input<
    ((profile: Data) => Promise<ProfileValidation>) | null
  >(null);
  basic: ProfileSection = { schema: {}, paths: [] };
  advanced: ProfileSection = { schema: {}, paths: [] };
  basicInitial: Data = {};
  advancedInitial: Data = {};
  basicValid = true;
  advancedValid = true;
  checking = false;
  validationFailed = false;
  validationMessages: string[] = [];
  private values: Data = {};
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private alive = true;
  private cdr = inject(ChangeDetectorRef);

  ngOnChanges(changes: SimpleChanges) {
    if (changes["schema"] || changes["initialData"]) {
      const sections = aiProfileSections(this.schema());
      this.basic = sections.basic;
      this.advanced = sections.advanced;
      this.values = structuredClone(this.initialData());
      this.basicInitial = profileSectionData(this.values, this.basic);
      this.advancedInitial = profileSectionData(this.values, this.advanced);
      this.basicValid = this.advancedValid = true;
    }
    queueMicrotask(() => {
      if (this.alive) this.validate();
    });
  }
  ngOnDestroy() {
    this.alive = false;
    this.generation++;
    clearTimeout(this.timer);
  }
  change(section: "basic" | "advanced", data: Data) {
    this.values = mergeProfileSection(this.values, data, this[section]);
    this.dataChange.emit(structuredClone(this.values));
    this.validate();
  }
  setValidity(section: "basic" | "advanced", valid: boolean) {
    if (section === "basic") this.basicValid = valid;
    else this.advancedValid = valid;
    this.validate();
  }
  validate() {
    const generation = ++this.generation;
    clearTimeout(this.timer);
    this.validationMessages = [];
    this.validationFailed = false;
    this.checking = false;
    if (
      !this.basicValid ||
      !this.advancedValid ||
      missingData(this.schema(), this.values).length
    ) {
      this.validityChange.emit(false);
      return;
    }
    const validate = this.validateProfile();
    if (!validate) {
      this.validityChange.emit(true);
      return;
    }
    this.checking = true;
    this.validityChange.emit(false);
    const profile = isRecord(this.values["profile"])
      ? this.values["profile"]
      : this.values;
    this.timer = setTimeout(async () => {
      try {
        const result = await validate(structuredClone(profile));
        if (!this.alive || generation !== this.generation) return;
        this.validationMessages = result.issues.map((issue) => issue.message);
        this.validityChange.emit(result.valid);
      } catch {
        if (!this.alive || generation !== this.generation) return;
        this.validationFailed = true;
        this.validationMessages = [
          "Model settings could not be validated. Try again.",
        ];
        this.validityChange.emit(false);
      } finally {
        if (this.alive && generation === this.generation) {
          this.checking = false;
          this.cdr.markForCheck();
        }
      }
    }, 200);
  }
}
