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
  ElementRef,
  Injector,
  OnInit,
  ViewContainerRef,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { ApiError, StudioApi } from "./api";
import { Modal } from "./dialog";
import { describeError } from "./errors";
import { preferredConnection } from "./integrations/connection-choice";
import type { ActivationPinsPicker } from "./integrations/activation-requirements";

export interface ActivationBindings {
  connection_revision_ids: Record<string, string>;
  /** Published connector version ID → release ID, one per connector the workflow calls. */
  connector_release_ids: Record<string, string>;
  worker_release_ids: Record<string, string>;
  assignment_binding_ids: Record<string, string>;
}
export interface ActivationSlot {
  name: string;
  connector: string;
  required: boolean;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Environments rarely hold many connection revisions; stop paging after this many.
const maximumPages = 4;

/**
 * Collects the environment bindings an activation needs: one connection
 * revision per workflow connection slot (chosen from the environment's
 * connections), one release per connector the workflow calls, worker
 * releases and human task assignment bindings. The release and assignment
 * pickers load lazily; with `versionId` they read the published version's
 * export and preselect unique matches. It stays open while the activation
 * runs and shows a failure inside, so the person can correct the bindings;
 * the shell closes it on success.
 */
@Component({
  selector: "weave-activation-dialog",
  standalone: true,
  imports: [Modal],
  template: `<weave-modal
    [heading]="'Activate ' + workflowName()"
    [wide]="true"
    describedBy="activation-summary"
    (dismiss)="dismiss()"
  >
    <form class="dialog-form activation-form" (submit)="submit($event)">
      <p id="activation-summary" class="dialog-message">
        Activate <strong>{{ workflowName() }}</strong> in
        <strong>{{ environmentLabel() }}</strong
        >. New runs use this version; runs already in progress keep theirs.
      </p>
      <h3>Connection slots</h3>
      <p class="hint">
        A connection slot is a named placeholder. When you activate a version,
        you choose a real connection for each slot in that environment.
      </p>
      @if (!activeSlots().length) {
        <p class="hint">This workflow has no connection slots.</p>
      } @else {
        @switch (state()) {
          @case ("loading") {
            <p class="dialog-status" role="status">
              <span class="loading-spinner small"></span>Loading the connections
              in this environment…
            </p>
          }
          @case ("forbidden") {
            <p class="notice">
              Your account cannot list connections in this environment. Ask an
              administrator for connection access, or enter each connection
              revision ID they give you.
            </p>
          }
          @case ("error") {
            <div class="notice error-notice" role="alert">
              <p>{{ error()?.message }}</p>
              @if (error()?.code) {
                <small class="support-code"
                  >Support code: {{ error()?.code }}</small
                >
              }
              <button type="button" (click)="load()">Try again</button>
            </div>
          }
          @case ("ready") {
            @if (!connections().length) {
              <p class="notice">
                This environment has no connections yet. Create one with your
                platform administrator, then try again.
              </p>
            }
          }
        }
        <div class="binding-list">
          @for (slot of activeSlots(); track slot.name) {
            <div class="binding-row slot-row">
              <p class="slot-need" [id]="'slot-need-' + slot.name">
                <strong>{{ slot.name }}</strong> needs a
                <span class="tag">{{ connectorText(slot.connector) }}</span>
                connection{{ slot.required ? "" : " (optional)" }}
              </p>
              <label
                class="slot-label"
                [id]="'slot-label-' + slot.name"
                [for]="'slot-' + slot.name"
                >Connection</label
              >
              @if (state() === "ready") {
                <select
                  [id]="'slot-' + slot.name"
                  [attr.aria-labelledby]="
                    'slot-need-' + slot.name + ' slot-label-' + slot.name
                  "
                  [required]="slot.required"
                  (change)="choose(slot.name, $event)"
                >
                  <option value="" [selected]="!choices[slot.name]">
                    {{
                      slot.required ? "Choose a connection" : "No connection"
                    }}
                  </option>
                  @for (connection of compatible(slot); track connection.id) {
                    <option
                      [value]="connection.id"
                      [selected]="choices[slot.name] === connection.id"
                    >
                      {{ connection.name }} · revision {{ connection.revision }}
                    </option>
                  }
                </select>
                @if (!compatible(slot).length) {
                  <p class="hint">
                    No connection here uses
                    {{ connectorText(slot.connector) }}.
                  </p>
                }
              } @else if (state() !== "loading") {
                <input
                  [id]="'slot-' + slot.name"
                  [attr.aria-labelledby]="
                    'slot-need-' + slot.name + ' slot-label-' + slot.name
                  "
                  autocomplete="off"
                  placeholder="Connection revision ID"
                  [required]="slot.required"
                  [value]="choices[slot.name] ?? ''"
                  (input)="choose(slot.name, $event)"
                />
              }
            </div>
          }
        </div>
      }
      <ng-container #pins />
      @if (pinsLoading()) {
        <p class="dialog-status" role="status">
          <span class="loading-spinner small"></span>Finding the releases and
          assignments this version needs…
        </p>
      }
      @if (problem) {
        <p class="error" role="alert">{{ problem }}</p>
      }
      @if (failure(); as failed) {
        <div
          class="notice error-notice activation-problem"
          role="alert"
          tabindex="-1"
        >
          <p>{{ failed.message }}</p>
          @if (failed.code) {
            <small class="support-code">Support code: {{ failed.code }}</small>
          }
        </div>
      }
      @if (running()) {
        <p class="dialog-status" role="status">
          <span class="loading-spinner small"></span>Activating…
        </p>
      }
      <div class="dialog-footer">
        <button
          type="button"
          class="tertiary"
          [disabled]="running()"
          (click)="dismiss()"
        >
          Cancel
        </button>
        <button
          type="submit"
          class="primary"
          [disabled]="
            busy() ||
            state() === 'loading' ||
            pinsLoading() ||
            !!picker()?.loading()
          "
        >
          Activate version
        </button>
      </div>
    </form>
  </weave-modal>`,
})
export class ActivationDialog implements OnInit {
  api = input.required<StudioApi>();
  workflowName = input("");
  environmentLabel = input("");
  /**
   * The published workflow version ID. With it, the pickers read the
   * version's export and pin exactly what the server checks.
   */
  versionId = input("");
  slots = input<ActivationSlot[]>([]);
  workerTaskTypes = input<string[]>([]);
  assignments = input<string[]>([]);
  busy = input(false);
  /** The activation request is running; the dialog cannot be closed meanwhile. */
  running = input(false);
  /** Why the last activation failed, in plain language with a support code. */
  failure = input<{ message: string; code: string } | null>(null);
  confirm = output<ActivationBindings>();
  cancel = output<void>();
  state = signal<"loading" | "ready" | "forbidden" | "error">("loading");
  error = signal<{ message: string; code: string } | null>(null);
  connections = signal<Record<string, unknown>[]>([]);
  choices: Record<string, string> = {};
  problem = "";
  /** The lazily created release and assignment pickers. */
  picker = signal<ActivationPinsPicker | null>(null);
  /**
   * The slots to bind: the published version's own once the pickers read its
   * export (the editor may have changed since publishing), else the editor's.
   */
  activeSlots = computed<ActivationSlot[]>(
    () => this.picker()?.exactSlots() ?? this.slots(),
  );
  private connectionsLoaded = false;
  pinsLoading = signal(true);
  private pinsHost = viewChild.required("pins", { read: ViewContainerRef });
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);
  constructor() {
    // A failure takes focus, so it is announced and Tab stays in the dialog.
    effect(() => {
      if (!this.failure()) return;
      afterNextRender(
        () =>
          this.host.nativeElement
            .querySelector<HTMLElement>(".activation-problem")
            ?.focus(),
        { injector: this.injector },
      );
    });
    // The exact slots can arrive after the editor's: load the connections
    // they need, or preselect for the new slots.
    effect(() => {
      const slots = this.activeSlots();
      untracked(() => {
        if (!slots.length || this.state() === "loading") return;
        if (!this.connectionsLoaded && this.state() === "ready")
          void this.load();
        else this.preselect();
      });
    });
  }
  ngOnInit() {
    if (this.activeSlots().length) void this.load();
    else this.state.set("ready");
    afterNextRender(() => void this.loadPicker(), { injector: this.injector });
  }
  /** Loads the pickers from their own chunk; without them, no pins are sent. */
  async loadPicker() {
    try {
      const { ActivationPinsPicker } = await import(
        "./integrations/activation-requirements"
      );
      const ref = this.pinsHost().createComponent(ActivationPinsPicker);
      ref.setInput("api", this.api());
      ref.setInput("versionId", this.versionId());
      ref.setInput("slots", this.slots());
      ref.setInput("workerTaskTypes", this.workerTaskTypes());
      ref.setInput("assignments", this.assignments());
      ref.instance.changed.subscribe(() => (this.problem = ""));
      this.picker.set(ref.instance);
    } catch {
      // Without the pickers the platform asks for any pin it needs.
    } finally {
      this.pinsLoading.set(false);
    }
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  /** "weave-http 2.0.0" for weave-http@2.0.0. */
  connectorText(connector: string) {
    return connector.replace("@", " ");
  }
  dismiss() {
    if (!this.running()) this.cancel.emit();
  }
  async load() {
    this.state.set("loading");
    this.error.set(null);
    try {
      const items: Record<string, unknown>[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < maximumPages; page++) {
        const result = await this.api().page("connections", true, cursor);
        items.push(...result.items.filter((item) => !item["unavailable"]));
        if (!result.next_cursor) break;
        cursor = result.next_cursor;
      }
      this.connections.set(items);
      this.connectionsLoaded = true;
      this.preselect();
      this.state.set("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403)
        this.state.set("forbidden");
      else {
        this.error.set(describeError(e));
        this.state.set("error");
      }
    }
  }
  /**
   * Preselects each required slot's connection: the latest revision named
   * like the slot, otherwise the only compatible one.
   */
  private preselect() {
    for (const slot of this.activeSlots()) {
      if (!slot.required || this.choices[slot.name]) continue;
      const choice = preferredConnection(slot.name, this.compatible(slot));
      if (choice) this.choices[slot.name] = choice;
    }
  }
  compatible(slot: ActivationSlot) {
    return this.connections().filter(
      (connection) => connection["connector"] === slot.connector,
    );
  }
  choose(slot: string, event: Event) {
    this.choices[slot] = this.text(event).trim();
    this.problem = "";
  }
  submit(event: Event) {
    event.preventDefault();
    if (this.busy() || this.pinsLoading() || this.picker()?.loading()) return;
    const form = event.target as HTMLFormElement;
    if (!form.reportValidity()) return;
    const connections: Record<string, string> = {};
    for (const slot of this.activeSlots()) {
      const value = this.choices[slot.name] ?? "";
      if (!value) continue;
      if (!uuid.test(value)) {
        this.problem = `The connection for ${slot.name} must be a revision ID such as 0f8fad5b-d9cb-469f-a165-70867728950e.`;
        return;
      }
      connections[slot.name] = value;
    }
    const pins = this.picker()?.collect() ?? {
      connector_release_ids: {},
      worker_release_ids: {},
      assignment_binding_ids: {},
    };
    if ("problem" in pins) {
      this.problem = pins.problem;
      return;
    }
    // The buttons are disabled while the activation runs; keep focus in the
    // dialog so Escape and Tab still reach it.
    this.host.nativeElement.querySelector<HTMLElement>(".modal-panel")?.focus();
    this.confirm.emit({ connection_revision_ids: connections, ...pins });
  }
}
