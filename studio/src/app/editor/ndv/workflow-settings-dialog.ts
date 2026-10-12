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
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  ViewEncapsulation,
  afterNextRender,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { DialogService, Modal } from "../../dialog";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import { leavingToast } from "./issues";
import { FormSession } from "./params/form-session";
import { ParameterForm } from "./params/param-form";
import { allSteps, slotsOf, usedBy, type Slot } from "./params/slots";
import type { StepDetailsController } from "./step-details-controller";
import type { StepDetailsHost } from "./step-details-host";
import { openRequest } from "./step-details-service";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** A control a person types in, where Escape first lets go of it. */
const isTyping = (element: Element | null): boolean =>
  !!element?.matches(
    'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]), textarea, select',
  );

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
    (dismiss)="close()"
  >
    <div class="sd-form-scope wf-settings" (keydown)="key($event)">
      <weave-parameter-form [session]="session()" name="parameters" />
      <section class="wf-section" aria-labelledby="wf-slots">
        <h3 id="wf-slots" class="param-label" tabindex="-1">
          Connection slots
        </h3>
        @if (slots(); as list) {
          @if (list.length) {
            <ul class="wf-list">
              @for (slot of list; track slot.name) {
                @let users = slotUsers(slot.name);
                <li class="wf-row" [attr.data-slot]="slot.name">
                  <span class="wf-text">
                    <span class="wf-title"
                      ><code class="wf-part">{{ slot.name }}</code
                      >&ngsp;<span class="wf-part"
                        >·&nbsp;{{ slot.connector }}</span
                      ></span
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
                        &ngsp;<span class="wf-part"
                          >·&nbsp;{{ profile.model }}</span
                        >
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
export class WorkflowSettingsDialog implements AfterViewInit {
  host = input.required<StepDetailsHost>();
  controller = input.required<StepDetailsController>();
  closed = output<void>();
  readonly announcement = signal("");
  readonly usedBy = usedBy;
  private readonly dialogs = inject(DialogService);
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private cached: FormSession | null = null;

  /** Opens on the first field's control, never on a tool of its label row. */
  ngAfterViewInit() {
    const field = this.element.nativeElement.querySelector<HTMLElement>(
      ".param-form > [data-param] .param-control",
    );
    (field && this.session().firstControl(field))?.focus();
  }
  /**
   * Closes, naming in a toast what isn't applied yet here and on the steps
   * it was left on, as step details do on Close.
   */
  close() {
    const kept = leavingToast(this.session().heldOnLeaving());
    const host = this.host();
    this.closed.emit();
    if (kept) host.notify(kept);
  }
  /** Escape in a text box lets go of it first; a second Escape closes. */
  key(event: KeyboardEvent) {
    const target = event.target as HTMLElement;
    if (event.key !== "Escape" || event.defaultPrevented || !isTyping(target))
      return;
    event.preventDefault();
    event.stopPropagation();
    target.blur();
    target.closest<HTMLElement>(".modal-panel")?.focus({ preventScroll: true });
  }

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
    const locked = this.controller().readOnlyReason("$workflow");
    return [
      {
        label: "Rename slot",
        disabled: !!locked,
        detail: locked ?? undefined,
        run: () => void this.renameSlot(slot.name),
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
        run: () => this.removeSlot(slot.name),
      },
    ];
  }
  /**
   * A renamed slot's row is a new one: focus goes to its menu. Without a
   * rename, the prompt gives focus back to the menu that opened it.
   */
  private async renameSlot(name: string) {
    const renamed = await this.session().renameWorkflowSlot(name);
    if (renamed) this.focusSlot(renamed);
  }
  /** Focus moves to the next slot's menu, else the one before, else the heading. */
  private removeSlot(name: string) {
    const names = this.slots().map((slot) => slot.name);
    const at = names.indexOf(name);
    if (!this.session().removeWorkflowSlot(name)) return;
    const rest = names.filter((other) => other !== name);
    this.focusSlot(rest[at] ?? rest[at - 1] ?? null);
  }
  private focusSlot(name: string | null) {
    afterNextRender(
      () => {
        if (this.lifetime.destroyed) return;
        const root = this.element.nativeElement;
        (name
          ? root.querySelector<HTMLElement>(
              `[data-slot="${CSS.escape(name)}"] .row-menu-toggle`,
            )
          : root.querySelector<HTMLElement>("#wf-slots")
        )?.focus();
      },
      { injector: this.injector },
    );
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
      run: () => void this.openStep(id),
    }));
  }
  /** Leaves for a step's details, naming what isn't applied, as Close does. */
  private async openStep(id: string) {
    const kept = leavingToast(this.session().heldOnLeaving());
    const host = this.host();
    if ((await host.openStepDetails(id)) && kept) host.notify(kept);
  }
}
