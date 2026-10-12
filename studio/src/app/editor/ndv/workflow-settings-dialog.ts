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
// Workflow settings, drawn by the same form as step details: Name and
// Version, with Stop runs after and Callable by other workflows as options.
// Below, the connection slots and AI profiles the workflow declares: who
// uses each one, Rename slot and Remove slot (once no step uses it), and
// Open for the steps that use a profile.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ViewEncapsulation,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { DialogService, Modal } from "../../dialog";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import { FormSession } from "./params/form-session";
import { ParameterForm } from "./params/param-form";
import { allSteps, slotsOf, usedBy, type Slot } from "./params/slots";
import type { StepDetailsController } from "./step-details-controller";
import type { StepDetailsHost } from "./step-details-host";
import { openRequest } from "./step-details-service";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

export interface ProfileRow {
  name: string;
  /** "ollama · qwen3:4b"; empty when the profile names neither. */
  model: string;
  steps: string[];
}

@Component({
  selector: "weave-workflow-settings-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [Modal, ParameterForm, RowMenu],
  template: `<weave-modal
    heading="Workflow settings"
    closeLabel="Close"
    [wide]="true"
    (dismiss)="closed.emit()"
  >
    <div class="sd-form-scope wf-settings">
      <weave-parameter-form [session]="session()" name="parameters" />
      <section class="wf-section" aria-labelledby="wf-slots">
        <h3 id="wf-slots" class="param-label">Connection slots</h3>
        @if (slots(); as list) {
          @if (list.length) {
            <ul class="wf-list">
              @for (slot of list; track slot.name) {
                @let users = slotUsers(slot.name);
                <li class="wf-row">
                  <span class="wf-text">
                    <span class="wf-title"
                      ><code class="wf-part">{{ slot.name }}</code> ·
                      <span class="wf-part">{{ slot.connector }}</span></span
                    >
                    <span class="wf-meta"
                      >{{ slot.required ? "Required" : "Optional" }} ·
                      {{ usedBy(users) }}</span
                    >
                  </span>
                  <weave-row-menu
                    [label]="'Actions for slot ' + slot.name"
                    [items]="slotItems(slot, users)"
                  />
                </li>
              }
            </ul>
          } @else {
            <p class="param-line" data-kind="hint">
              This workflow uses no connections.
            </p>
          }
        }
        <p class="param-line" data-kind="hint">
          Steps declare slots from their Connection field.
        </p>
      </section>
      <section class="wf-section" aria-labelledby="wf-ai">
        <h3 id="wf-ai" class="param-label">AI profiles</h3>
        @if (profiles(); as list) {
          @if (list.length) {
            <ul class="wf-list">
              @for (profile of list; track profile.name) {
                <li class="wf-row">
                  <span class="wf-text">
                    <span class="wf-title"
                      ><code class="wf-part">{{ profile.name }}</code>
                      @if (profile.model) {
                        · <span class="wf-part">{{ profile.model }}</span>
                      }
                    </span>
                    <span class="wf-meta">{{ usedBy(profile.steps) }}</span>
                  </span>
                  <weave-row-menu
                    [label]="'Actions for AI profile ' + profile.name"
                    [items]="profileItems(profile)"
                  />
                </li>
              }
            </ul>
          } @else {
            <p class="param-line" data-kind="hint">No AI profiles yet.</p>
          }
        }
        <p class="param-line" data-kind="hint">
          AI steps keep their profiles. Open a step to change its model.
        </p>
      </section>
      <!-- Inside the modal dialog, so assistive technology hears it. -->
      <p class="sr-only" aria-live="polite">{{ announcement() }}</p>
    </div>
  </weave-modal>`,
})
export class WorkflowSettingsDialog {
  host = input.required<StepDetailsHost>();
  controller = input.required<StepDetailsController>();
  closed = output<void>();
  readonly announcement = signal("");
  readonly usedBy = usedBy;
  private readonly dialogs = inject(DialogService);
  private readonly lifetime = inject(DestroyRef);
  private cached: FormSession | null = null;

  session(): FormSession {
    if (
      this.cached?.controller !== this.controller() ||
      this.cached.host !== this.host() ||
      !this.cached.isCurrent()
    )
      this.cached = new FormSession(
        this.host(),
        this.controller(),
        openRequest("$workflow"),
        {
          confirm: (options) => this.dialogs.confirm(options),
          prompt: (options) => this.dialogs.prompt(options),
          announce: (text) => {
            this.announcement.set("");
            setTimeout(() => {
              if (!this.lifetime.destroyed) this.announcement.set(text);
            });
          },
        },
        () => !this.lifetime.destroyed,
      );
    return this.cached;
  }
  slots(): Slot[] {
    return slotsOf(this.host().model.definition);
  }
  slotUsers(name: string): string[] {
    return this.session().slotUsers(name);
  }
  slotItems(slot: Slot, users: readonly string[]): RowMenuItem[] {
    const locked = this.controller().readOnlyReason();
    return [
      {
        label: "Rename slot",
        disabled: !!locked,
        detail: locked ?? undefined,
        run: () => void this.session().renameWorkflowSlot(slot.name),
      },
      {
        label: "Remove slot",
        danger: !locked && !users.length,
        disabled: !!locked || users.length > 0,
        detail:
          locked ??
          (users.length
            ? `${usedBy(users)}. Choose another slot there first.`
            : undefined),
        run: () => this.session().removeWorkflowSlot(slot.name),
      },
    ];
  }
  profiles(): ProfileRow[] {
    const definition = this.host().model.definition;
    const profiles = definition.spec["llmProfiles"];
    if (!isRecord(profiles)) return [];
    const steps = allSteps(definition);
    return Object.entries(profiles).map(([name, value]) => ({
      name,
      model: isRecord(value)
        ? [value["provider"], value["model"]]
            .filter((part) => typeof part === "string" && part)
            .join(" · ")
        : "",
      steps: steps
        .filter((step) => step["profile"] === name)
        .map((step) => step.id),
    }));
  }
  profileItems(profile: ProfileRow): RowMenuItem[] {
    if (!profile.steps.length)
      return [
        {
          label: "Open step",
          disabled: true,
          detail: "No step uses this profile yet.",
          run: () => undefined,
        },
      ];
    return profile.steps.map((id) => ({
      label: `Open ${id}`,
      run: () => void this.host().openStepDetails(id),
    }));
  }
}
