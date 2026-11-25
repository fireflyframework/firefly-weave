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
import { ApiError, StudioApi, type Diagnostic } from "./api";
import { Modal } from "./dialog";
import { describeError } from "./errors";
import type { ActivationPinsPicker } from "./integrations/activation-requirements";
import type { Workflow } from "./model";
import {
  connectionRows,
  type ConnectionRow,
} from "./editor/activation/activation-rows";
import type { ActivationSteps } from "./editor/activation/activation-steps";
import {
  platformConnections,
  type BindableItem,
} from "./editor/connections/connections-api";
import type { DevBinding } from "./editor/connections/dev-bindings";
import {
  connectionsPort,
  type PortHost,
} from "./editor/connections/connections-port";
import type {
  CanvasSidecar,
  TriggerIntent,
} from "./editor/state/canvas-sidecar";

export interface ActivationBindings {
  connection_revision_ids: Record<string, string>;
  /** Published connector version ID → release ID, one per connector the workflow calls. */
  connector_release_ids: Record<string, string>;
  worker_release_ids: Record<string, string>;
  assignment_binding_ids: Record<string, string>;
  /** One activation of each workflow version this workflow calls, by `name@version`. */
  workflow_activation_ids?: Record<string, string>;
}
export interface ActivationSlot {
  name: string;
  connector: string;
  required: boolean;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
/** What a set of slots needs, to tell another set from the same one passed again. */
const slotsKey = (slots: readonly ActivationSlot[]) =>
  slots
    .map((slot) => `${slot.name}\u0000${slot.connector}\u0000${slot.required}`)
    .join("\u0001");

/** An API that reads the version's export once; every other request goes through as it is. */
function onceEach(api: StudioApi): StudioApi {
  const reads = new Map<string, Promise<unknown>>();
  return new Proxy(api, {
    get(target, key) {
      if (key === "request")
        return (path: string, ...rest: unknown[]) => {
          if (rest.length || !path.endsWith("/export"))
            return (target.request as (...args: unknown[]) => unknown).call(
              target,
              path,
              ...rest,
            );
          let read = reads.get(path);
          if (!read) {
            read = target.request<unknown>(path);
            reads.set(path, read);
            read.catch(() => reads.delete(path));
          }
          return read;
        };
      const value: unknown = Reflect.get(target, key, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

@Component({
  selector: "weave-activation-dialog",
  standalone: true,
  imports: [Modal],
  template: `<weave-modal
    [heading]="(activated() ? 'Activated ' : 'Activate ') + workflowName()"
    [wide]="true"
    describedBy="activation-summary"
    (dismiss)="dismiss()"
  >
    <form class="dialog-form activation-form" (submit)="submit($event)">
      @if (activated()) {
        <!-- Done: the toast's words, while the rows below say how each trigger went. -->
        <p id="activation-summary" class="dialog-message">
          Activated in {{ environmentLabel() }}.{{
            attentionText() ? " " + attentionText() : ""
          }}
        </p>
      } @else {
        <p id="activation-summary" class="dialog-message">
          Activate <strong>{{ workflowName() }}</strong> in
          <strong>{{ environmentLabel() }}</strong
          >. New runs use this version; runs already in progress keep theirs.
        </p>
      }
      @if (automaticCount && !stopped() && !activated()) {
        <p class="automatic-choices">
          {{ automaticCount }} chosen for you ·
          <button type="button" class="text-link" (click)="toggleReview()">
            {{ reviewAutomatic() ? "Hide review" : "Review" }}
          </button>
        </p>
      }
      @if (
        !stopped() &&
        (visibleSlots().length || (activeSlots().length && state() !== "ready"))
      ) {
        <h3>Connections</h3>
        @switch (state()) {
          @case ("loading") {
            <p class="dialog-status" role="status">
              <span class="loading-spinner small"></span>Loading the connections
              in this environment…
            </p>
          }
          @case ("forbidden") {
            <p class="notice">
              You can't bind connections in {{ environmentLabel() }}. Ask an
              administrator for the connection binder role.
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
          @case ("ready") {}
        }
        <div class="binding-list">
          @for (slot of visibleSlots(); track slot.name) {
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
                  @for (
                    connection of compatible(slot);
                    track connection.revisionId
                  ) {
                    <option
                      [value]="connection.revisionId"
                      [selected]="choices[slot.name] === connection.revisionId"
                    >
                      {{ connection.name }}
                    </option>
                  }
                </select>
                @if (rowOf(slot.name); as row) {
                  @if (row.message) {
                    @let way = connectionsLink(row.message);
                    <p class="hint">
                      {{ way.before }}
                      @if (way.link) {
                        <button
                          type="button"
                          class="text-link"
                          [disabled]="stays()"
                          (click)="goToConnections()"
                        >
                          {{ way.link }}
                        </button>
                      }
                    </p>
                  } @else if (row.tested && choices[slot.name] === row.chosen) {
                    <p class="hint">{{ row.tested }}</p>
                  }
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
      <div [hidden]="!!stopped()"><ng-container #pins /></div>
      <ng-container #stepsSlot />
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
      @if (settingUp()) {
        <p class="dialog-status" role="status">
          <span class="loading-spinner small"></span>Setting up the triggers…
        </p>
      }
      @if (!activated() && missingConnection(); as missing) {
        <p class="hint activation-missing" id="activation-missing">
          {{ missing }}
        </p>
      }
      <div class="dialog-footer">
        @if (activated()) {
          <!-- The activation is done: the triggers' rows say what happened. -->
          @if (steps()?.doneKeeps(); as keeps) {
            <p class="activation-keeps">{{ keeps }}</p>
          }
          <button
            type="button"
            class="primary"
            data-done
            [disabled]="settingUp()"
            [attr.aria-disabled]="steps()?.acting() || null"
            (click)="dismiss()"
          >
            Done
          </button>
        } @else {
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
              !!picker()?.loading() ||
              !!steps()?.blocked() ||
              !!missingConnection()
            "
            [attr.aria-describedby]="
              missingConnection() ? 'activation-missing' : null
            "
          >
            Activate version
          </button>
        }
      </div>
    </form>
  </weave-modal>`,
  styles: `
    .activation-keeps {
      align-self: center;
      margin: 0 auto 0 0;
      color: var(--muted);
      font: var(--type-small);
    }
    @media (max-width: 767px) {
      .activation-keeps {
        margin: 0;
        text-align: center;
      }
    }
  `,
})
export class ActivationDialog implements OnInit {
  api = input.required<StudioApi>();
  /** The dialog's API: the release picker and the checks share one read of the version's export. */
  readonly shared = computed(() => onceEach(this.api()));
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
  /** The editor's workflow and canvas: its called workflows and trigger choices. */
  definition = input<Workflow | null>(null);
  canvas = input<CanvasSidecar | null>(null);
  environmentId = input("");
  /** The editor's last check, for a called workflow that refuses this caller. */
  diagnostics = input<readonly Diagnostic[]>([]);
  /** The editor, whose connections port knows this computer's dev bindings. */
  connectionsHost = input<PortHost | null>(null);
  confirm = output<ActivationBindings>();
  /** The activation's ID once it succeeded: the dialog then sets its triggers up. */
  activated = input("");
  /**
   * The trigger choices with their created or moved resources kept, and what
   * needs attention; null intents when there was nothing to set up. Emitted
   * once the triggers are set up, and again after each Retry.
   */
  triggers = output<{
    intents: TriggerIntent[] | null;
    attention: string | null;
    first: boolean;
  }>();
  readonly settingUp = signal(false);
  /**
   * While the activation, its triggers or a row's answer runs, nothing closes
   * the dialog or opens behind it: what was made and what is owed would be lost.
   */
  readonly stays = computed(
    () => this.running() || this.settingUp() || !!this.steps()?.acting(),
  );
  /** After the activation: "1 trigger needs attention.", or "" when every trigger went through. */
  readonly attentionText = signal("");
  private setUpFor = "";
  cancel = output<void>();
  /** "Create one in Connections.": the dialog closes and Connections opens. */
  openConnections = output<void>();
  /** "Open schedule": the dialog closes and that trigger's step details open. */
  openTrigger = output<string>();
  /** A message, and the words of it that open Connections, when it ends with them. */
  connectionsLink(message: string): { before: string; link: string | null } {
    const link = "Create one in Connections.";
    return message.endsWith(link)
      ? { before: message.slice(0, -link.length), link }
      : { before: message, link: null };
  }
  /** Why Activate waits: a required slot with no connection chosen yet; null otherwise. */
  missingConnection(): string | null {
    if (this.state() !== "ready" || this.stopped()) return null;
    const slot = this.activeSlots().find(
      (item) => item.required && !(this.choices[item.name] ?? "").trim(),
    );
    return slot ? `Choose a connection for ${slot.name} first.` : null;
  }
  state = signal<"loading" | "ready" | "forbidden" | "error">("loading");
  error = signal<{ message: string; code: string } | null>(null);
  connections = signal<BindableItem[]>([]);
  /** One row per slot: its candidates, the one chosen for the person and why. */
  rows = signal<ConnectionRow[]>([]);
  /** The checks and the called workflows and triggers steps, loaded with the dialog. */
  steps = signal<ActivationSteps | null>(null);
  stopped = computed(() => this.steps()?.stop() ?? null);
  private stepsHost = viewChild.required("stepsSlot", {
    read: ViewContainerRef,
  });
  choices: Record<string, string> = {};
  reviewAutomatic = signal(false);
  rowOf(slot: string): ConnectionRow | null {
    return this.rows().find((row) => row.slot === slot) ?? null;
  }
  private automaticSlot(slot: ActivationSlot) {
    const row = this.rowOf(slot.name);
    return (
      this.state() === "ready" &&
      !!row?.chosenBy &&
      this.choices[slot.name] === row.chosen
    );
  }
  visibleSlots() {
    return this.activeSlots().filter(
      (slot) => this.reviewAutomatic() || !this.automaticSlot(slot),
    );
  }
  get automaticCount() {
    return (
      this.activeSlots().filter((slot) => this.automaticSlot(slot)).length +
      (this.picker()?.automaticRows().length ?? 0)
    );
  }
  toggleReview() {
    this.reviewAutomatic.set(!this.reviewAutomatic());
    this.picker()?.reviewAutomatic.set(this.reviewAutomatic());
  }
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
    // Once the activation succeeds, its triggers are set up (or there is nothing to do).
    effect(() => {
      const id = this.activated();
      untracked(() => {
        if (!id || id === this.setUpFor) return;
        this.setUpFor = id;
        void this.setUp(id);
      });
    });
    // While the dialog has to stay, the steps' ways out (Open schedule) are off too.
    effect(() => {
      const steps = this.steps();
      const stays = this.stays();
      untracked(() => steps?.busy.set(stays));
    });
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
        // Other slots (the published version's) need their own connections and
        // rows; the same slots, passed again, keep what was loaded.
        if (this.state() === "ready" && slotsKey(slots) !== this.loadedSlots)
          void this.load();
        else if (!this.connectionsLoaded && this.state() === "ready")
          void this.load();
      });
    });
  }
  ngOnInit() {
    if (this.activeSlots().length) void this.load();
    else this.state.set("ready");
    afterNextRender(
      () => {
        void this.loadPicker();
        void this.loadSteps();
      },
      { injector: this.injector },
    );
  }
  private async setUp(activationId: string) {
    const steps = this.steps();
    if (!steps?.needsSync()) {
      this.triggers.emit({ intents: null, attention: null, first: true });
      return;
    }
    this.settingUp.set(true);
    try {
      await steps.sync(activationId);
    } finally {
      this.settingUp.set(false);
    }
    this.attentionText.set(steps.attention() ?? "");
    this.triggers.emit({
      intents: steps.storedIntents(),
      attention: steps.attention(),
      first: true,
    });
    this.focusNext();
    steps.retried = () => {
      this.attentionText.set(steps.attention() ?? "");
      this.triggers.emit({
        intents: steps.storedIntents(),
        attention: steps.attention(),
        first: false,
      });
      this.focusNext();
    };
  }
  /** Focus goes to the first row that asks something (its safe answer), otherwise to Done. */
  private focusNext() {
    afterNextRender(
      () =>
        (
          this.host.nativeElement.querySelector<HTMLElement>("[data-ask]") ??
          this.host.nativeElement.querySelector<HTMLElement>("[data-done]")
        )?.focus(),
      { injector: this.injector },
    );
  }
  /** Loads the checks and the called workflows and triggers steps from their own chunk. */
  async loadSteps() {
    const definition = this.definition();
    const canvas = this.canvas();
    if (!definition || !canvas) return;
    try {
      const { ActivationSteps } = await import(
        "./editor/activation/activation-steps"
      );
      const ref = this.stepsHost().createComponent(ActivationSteps);
      ref.setInput("api", this.shared());
      ref.setInput("definition", definition);
      ref.setInput("canvas", canvas);
      ref.setInput("versionId", this.versionId());
      ref.setInput(
        "environment",
        this.environmentId()
          ? {
              id: this.environmentId(),
              name: this.environmentLabel(),
              short: this.connectionsHost()?.environmentName,
            }
          : null,
      );
      ref.setInput("diagnostics", this.diagnostics());
      ref.instance.openTrigger = (id) => {
        if (!this.stays()) this.openTrigger.emit(id);
      };
      this.steps.set(ref.instance);
    } catch {
      // Without them the platform still refuses what it can't activate.
    }
  }
  /** Loads the pickers from their own chunk; without them, no pins are sent. */
  async loadPicker() {
    try {
      const { ActivationPinsPicker } = await import(
        "./integrations/activation-requirements"
      );
      const ref = this.pinsHost().createComponent(ActivationPinsPicker);
      ref.setInput("api", this.shared());
      ref.setInput("versionId", this.versionId());
      ref.setInput("slots", this.slots());
      ref.setInput("workerTaskTypes", this.workerTaskTypes());
      ref.setInput("assignments", this.assignments());
      ref.instance.changed.subscribe(() => (this.problem = ""));
      this.picker.set(ref.instance);
      ref.instance.reviewAutomatic.set(this.reviewAutomatic());
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
    if (!this.stays()) this.cancel.emit();
  }
  goToConnections() {
    if (!this.stays()) this.openConnections.emit();
  }
  /** The slots the connection rows were built for. */
  private loadedSlots = "";
  async load() {
    this.loadedSlots = slotsKey(this.activeSlots());
    this.state.set("loading");
    this.error.set(null);
    try {
      const client = platformConnections(this.api());
      const connectors = [
        ...new Set(this.activeSlots().map((slot) => slot.connector)),
      ];
      const lists = await Promise.all(
        connectors.map((connector) => client.bindable(connector)),
      );
      this.connections.set(lists.flat());
      this.connectionsLoaded = true;
      this.fill(this.connections());
      this.state.set("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        this.fill("forbidden");
        this.state.set("forbidden");
      } else {
        this.error.set(describeError(e));
        this.state.set("error");
      }
    }
  }
  /** The rows for the slots, and the choice Studio makes for each it can. */
  private fill(bindable: BindableItem[] | "forbidden") {
    const host = this.connectionsHost();
    const port = host ? connectionsPort(host) : null;
    const workflow = this.definition()?.metadata.name ?? "";
    const devBinding = (slot: string): DevBinding | null =>
      port?.binding(workflow, slot) ?? null;
    const slots = this.activeSlots();
    this.rows.set(
      connectionRows({
        environment: this.environmentLabel() || "this environment",
        slots: slots.map((slot) => ({
          name: slot.name,
          connector: slot.connector,
          required: slot.required,
        })),
        aiSteps: [],
        agentToolSlots: [],
        bindable,
        devBindings: slots.flatMap((slot) => {
          const binding = devBinding(slot.name);
          return binding ? [{ slot: slot.name, binding }] : [];
        }),
      }),
    );
    this.preselect();
  }
  /**
   * Chooses each slot's connection the person hasn't chosen: this computer's
   * dev binding while it can still be bound, otherwise the only candidate.
   */
  private preselect() {
    for (const row of this.rows())
      if (row.chosen && !this.choices[row.slot])
        this.choices[row.slot] = row.chosen;
  }
  compatible(slot: ActivationSlot): BindableItem[] {
    return this.rowOf(slot.name)?.candidates ?? [];
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
    const steps = this.steps();
    const calls = steps?.pins() ?? {};
    this.confirm.emit({
      connection_revision_ids: connections,
      ...pins,
      ...(Object.keys(calls).length ? { workflow_activation_ids: calls } : {}),
    });
  }
}
