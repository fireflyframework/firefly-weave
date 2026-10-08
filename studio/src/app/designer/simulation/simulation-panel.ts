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
// Simulation setup and controls. The panel never calls the platform: it emits
// the debug-session create body and each command body, and the host sends them
// (POST {project}/debug/sessions and .../commands with If-Match). Everything it
// shows is derived from the compiled artifact the session runs, so node IDs,
// signal names and schemas always match what the simulator validates.
import {
  ChangeDetectorRef,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  OnChanges,
  SimpleChanges,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Icon } from "../../icon";
import { Modal } from "../../dialog";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import type { PlainError } from "../../errors";
import { TaskForm } from "../../task-form";
import {
  eventText,
  simulationMessage,
  statusDetail,
  statusLabel,
  type SimulationEvent,
} from "./simulation-copy";
import {
  ValueEditor,
  advanceOptions,
  advanceSeconds,
  breakpointsCommand,
  createBody,
  decisionCommand,
  sampleValue,
  signalCommand,
  simulationPlan,
  stepResults,
  type DebugCommandBody,
  type DebugCreateBody,
  type DebugSessionData,
  type HumanSlot,
  type Json,
  type SignalSlot,
  type SimulationNodes,
} from "./simulation-plan";

export type * from "./simulation-plan";

const UNITS = [
  { label: "seconds", seconds: 1 },
  { label: "minutes", seconds: 60 },
  { label: "hours", seconds: 3600 },
  { label: "days", seconds: 86400 },
];
let sequence = 0;
const noNodes = (): SimulationNodes => ({
  status: "",
  current: [],
  active: [],
  breakpoints: [],
  done: [],
});

/**
 * Simulation setup dialog plus the control panel docked beside the canvas. Inputs: the
 * compiled artifact (required), the current debug session, a busy flag and the
 * last request failure. Outputs: start (create body), command (command body),
 * nodesChange (canvas highlights) and close.
 */
@Component({
  selector: "weave-simulation-panel",
  standalone: true,
  imports: [Icon, Modal, ModalSheet, NgTemplateOutlet, TaskForm],
  template: `
    <ng-template #editor let-state let-label="label" let-id="id">
      @if (state.formCapable) {
        <div class="sim-editor-tools">
          <button
            type="button"
            class="sim-link"
            [attr.aria-pressed]="state.jsonMode"
            (click)="state.toggle()"
          >
            {{ state.jsonMode ? "Use the form" : "Edit as JSON" }}
          </button>
        </div>
      }
      @if (state.jsonMode) {
        <label class="sim-json-label" [for]="id">{{ label }} (JSON)</label>
        <textarea
          class="monospace sim-json"
          spellcheck="false"
          [id]="id"
          [value]="state.json"
          [attr.aria-invalid]="!!state.jsonError"
          [attr.aria-describedby]="state.jsonError ? id + '-error' : null"
          (input)="state.editJson(textOf($event))"
        ></textarea>
        @if (state.jsonError) {
          <p class="error" [id]="id + '-error'">{{ state.jsonError }}</p>
        }
      } @else {
        <weave-task-form
          [schema]="state.schema"
          [initialData]="state.initial"
          (dataChange)="state.formChange($event)"
          (validityChange)="state.formValid = $event"
        />
      }
    </ng-template>

    @if (showSetup()) {
      <weave-modal
        heading="Simulate this workflow"
        closeLabel="Close simulation setup"
        [describedBy]="prefix + '-setup-description'"
        [wide]="true"
        (dismiss)="cancelSetup()"
      >
        <p class="dialog-message" [id]="prefix + '-setup-description'">
          The simulation steps through the compiled workflow without calling
          real systems. Enter the workflow input, and the result each action
          should return.
        </p>
        @if (plan().actions.length) {
          <div class="sim-tabs" role="tablist" aria-label="Simulation setup">
            @for (name of tabNames; track name) {
              <button
                type="button"
                role="tab"
                [id]="prefix + '-tab-' + name"
                [attr.aria-selected]="tab() === name"
                [attr.aria-controls]="prefix + '-panel-' + name"
                [attr.tabindex]="tab() === name ? 0 : -1"
                [class.selected]="tab() === name"
                (click)="tab.set(name)"
                (keydown)="tabKey($event)"
              >
                {{
                  name === "input"
                    ? "Workflow input"
                    : "Action results (" + plan().actions.length + ")"
                }}
              </button>
            }
          </div>
        }
        <div
          class="sim-tab-panel"
          role="tabpanel"
          [id]="prefix + '-panel-input'"
          [attr.aria-labelledby]="
            plan().actions.length ? prefix + '-tab-input' : null
          "
          [attr.aria-label]="plan().actions.length ? null : 'Workflow input'"
          [hidden]="tab() !== 'input'"
        >
          @if (inputEditor(); as state) {
            <ng-container
              [ngTemplateOutlet]="editor"
              [ngTemplateOutletContext]="{
                $implicit: state,
                label: 'Workflow input',
                id: prefix + '-input-json',
              }"
            />
          }
        </div>
        @if (plan().actions.length) {
          <div
            class="sim-tab-panel"
            role="tabpanel"
            [id]="prefix + '-panel-actions'"
            [attr.aria-labelledby]="prefix + '-tab-actions'"
            [hidden]="tab() !== 'actions'"
          >
            <p class="hint">
              Each action returns this result instead of running. A skipped
              action stops the simulation when it is reached.
            </p>
            @for (slot of plan().actions; track slot.nodeId) {
              @let state = actionEditors.get(slot.nodeId)!;
              <fieldset class="sim-action" [attr.data-node]="slot.nodeId">
                <legend>
                  <span class="sim-node">{{ slot.nodeId }}</span>
                  @if (slot.reference) {
                    <small>{{ slot.reference }}</small>
                  }
                </legend>
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="state.skipped"
                    (change)="state.skipped = checked($event)"
                  />Skip this action</label
                >
                <!-- Hidden, not removed: skipping keeps the edited result. -->
                <div class="sim-result" [hidden]="state.skipped">
                  <ng-container
                    [ngTemplateOutlet]="editor"
                    [ngTemplateOutletContext]="{
                      $implicit: state,
                      label: 'Result of ' + slot.nodeId,
                      id: prefix + '-result-' + $index,
                    }"
                  />
                </div>
              </fieldset>
            }
          </div>
        }
        <!-- Only a failed start belongs here; a failed command stays in the
             panel and is not repeated when Start over reopens the setup. -->
        @if (error() && errorScope === "setup") {
          <div class="notice error-notice" role="alert">
            <p>{{ errorMessage() }}</p>
            @if (error()?.code) {
              <span class="support-code"
                >Support code: {{ error()?.code }}</span
              >
            }
          </div>
        }
        @if (setupProblem(); as problem) {
          <p class="hint sim-blocker" [id]="prefix + '-blocker'">
            {{ problem }}
          </p>
        }
        <div class="dialog-actions">
          <button type="button" (click)="cancelSetup()">Cancel</button>
          <button
            type="button"
            class="primary"
            [disabled]="!!setupProblem()"
            [attr.aria-disabled]="busy() || null"
            [attr.aria-describedby]="
              setupProblem() ? prefix + '-blocker' : null
            "
            (click)="startSimulation()"
          >
            {{ busy() ? "Starting…" : "Start simulation" }}
          </button>
        </div>
      </weave-modal>
    }

    @if (session(); as current) {
      @let view = current.view;
      @let wait = waitKind();
      <section
        class="sim-panel"
        [class.collapsed]="collapsed() && !docked()"
        [class.docked]="docked()"
        [attr.aria-labelledby]="prefix + '-title'"
        [attr.aria-busy]="busy()"
        [weaveModalSheet]="!collapsed()"
        [sheetWhen]="sheetWhen"
        [sheetInitialFocus]="'#' + prefix + '-title'"
        [sheetReturnFocus]="collapseToggle"
        (sheetDismiss)="collapsed.set(true)"
      >
        <header class="sim-header">
          <h2 [id]="prefix + '-title'" tabindex="-1">Simulation</h2>
          <span
            class="pill sim-status"
            role="status"
            [attr.data-tone]="tone(view.status)"
            >{{ label(view.status) }}</span
          >
          <span class="sim-header-tools">
            @if (!docked()) {
              <button
                type="button"
                class="icon-button"
                [id]="prefix + '-toggle'"
                [attr.aria-label]="
                  collapsed() ? 'Expand simulation' : 'Collapse simulation'
                "
                [attr.aria-expanded]="!collapsed()"
                [attr.aria-controls]="prefix + '-body'"
                (click)="collapsed.set(!collapsed())"
              >
                <weave-icon name="chevron" [class.sim-turned]="!collapsed()" />
              </button>
            }
            <button
              type="button"
              class="icon-button"
              aria-label="Close simulation"
              (click)="closePanel()"
            >
              <weave-icon name="close" />
            </button>
          </span>
        </header>
        <div
          class="sim-body"
          [id]="prefix + '-body'"
          [hidden]="collapsed() && !docked()"
        >
          <p class="hint">
            {{ detail(view.status) }} Simulated time:
            <time [attr.datetime]="view.now">{{ clock(view.now) }}</time
            >. No real systems are called.
          </p>
          @if (view.current_nodes.length) {
            <p class="sim-now">
              Now at
              @for (node of view.current_nodes; track node) {
                <strong>{{ node }}</strong>
              }
            </p>
          }
          @if (error() && errorScope === "panel") {
            <div class="notice error-notice" role="alert">
              <p>{{ errorMessage() }}</p>
              @if (error()?.code) {
                <span class="support-code"
                  >Support code: {{ error()?.code }}</span
                >
              }
            </div>
          }
          @for (problem of view.diagnostics; track $index) {
            <div class="notice error-notice" role="status">
              <p>{{ diagnosticMessage(problem) }}</p>
              <span class="support-code">Support code: {{ problem.code }}</span>
            </div>
          }

          @if (!finished()) {
            <!-- Only the control that matches what the run waits for; it is
                 the one primary button here. -->
            @if (wait === "human") {
              @for (human of activeHumans(); track human.nodeId) {
                @let state = decisionEditors.get(human.nodeId)!;
                <form
                  class="sim-section sim-wait"
                  [attr.aria-labelledby]="prefix + '-decide-' + human.nodeId"
                  (submit)="decide($event, human)"
                >
                  <h3 [id]="prefix + '-decide-' + human.nodeId">
                    Decide for {{ human.nodeId }}
                  </h3>
                  @if (submitted.has(human.nodeId)) {
                    <p class="hint" role="status">
                      Decision sent: {{ chosen.get(human.nodeId) }}. Continue
                      the simulation to apply it.
                    </p>
                  } @else {
                    <fieldset class="sim-choices">
                      <legend>Decision</legend>
                      @for (option of human.decisions; track option) {
                        <label class="checkbox-field"
                          ><input
                            type="radio"
                            [name]="prefix + '-decision-' + human.nodeId"
                            [value]="option"
                            [checked]="chosen.get(human.nodeId) === option"
                            (change)="chosen.set(human.nodeId, option)"
                          />{{ option }}</label
                        >
                      }
                    </fieldset>
                    <ng-container
                      [ngTemplateOutlet]="editor"
                      [ngTemplateOutletContext]="{
                        $implicit: state,
                        label: 'Decision data',
                        id: prefix + '-decision-json-' + $index,
                      }"
                    />
                    @let decisionWhy = decisionProblem(human);
                    @let decisionJson =
                      !!decisionWhy &&
                      state.jsonMode &&
                      decisionWhy === state.jsonError;
                    <!-- A JSON error is already shown under the editor. -->
                    @if (decisionWhy && !decisionJson) {
                      <p class="hint" [id]="prefix + '-decide-why-' + $index">
                        {{ decisionWhy }}
                      </p>
                    }
                    <button
                      type="submit"
                      [class.primary]="$first"
                      [disabled]="!!decisionWhy"
                      [attr.aria-disabled]="busy() || null"
                      [attr.aria-describedby]="
                        !decisionWhy
                          ? null
                          : decisionJson
                            ? prefix + '-decision-json-' + $index + '-error'
                            : prefix + '-decide-why-' + $index
                      "
                    >
                      Submit decision
                    </button>
                  }
                </form>
              }
            } @else if (wait === "signal") {
              <form
                class="sim-section sim-wait"
                [attr.aria-labelledby]="prefix + '-signal-title'"
                (submit)="sendSignal($event)"
              >
                <h3 [id]="prefix + '-signal-title'">Send a signal</h3>
                @if (waitingSignals().length > 1) {
                  <label [for]="prefix + '-signal-name'">Signal</label>
                  <select
                    [id]="prefix + '-signal-name'"
                    (change)="signalChoice.set(valueOf($event))"
                  >
                    @for (slot of waitingSignals(); track slot.nodeId) {
                      <option
                        [value]="slot.nodeId"
                        [selected]="slot.nodeId === selectedSignal()?.nodeId"
                      >
                        {{ slot.name }}
                      </option>
                    }
                  </select>
                }
                <!-- One editor per signal, hidden rather than removed, so each
                     keeps its own edits and what is shown is what is sent. The
                     disabled fieldset keeps hidden editors out of the browser's
                     form checks, which would otherwise block Send. -->
                @for (slot of plan().signals; track slot.nodeId) {
                  @let shown = slot.nodeId === selectedSignal()?.nodeId;
                  <fieldset
                    class="sim-signal"
                    role="none"
                    [attr.data-node]="slot.nodeId"
                    [hidden]="!shown"
                    [disabled]="!shown"
                  >
                    <ng-container
                      [ngTemplateOutlet]="editor"
                      [ngTemplateOutletContext]="{
                        $implicit: signalEditors.get(slot.nodeId)!,
                        label: 'Signal payload',
                        id: prefix + '-signal-json-' + $index,
                      }"
                    />
                  </fieldset>
                }
                @if (selectedSignal(); as slot) {
                  @let state = signalEditors.get(slot.nodeId)!;
                  @let signalWhy = state.problem();
                  @if (signalWhy && !state.jsonMode) {
                    <p class="hint" [id]="prefix + '-signal-why'">
                      {{ signalWhy }}
                    </p>
                  }
                  <button
                    type="submit"
                    class="primary"
                    [disabled]="!!signalWhy"
                    [attr.aria-disabled]="busy() || null"
                    [attr.aria-describedby]="
                      !signalWhy
                        ? null
                        : state.jsonMode
                          ? prefix +
                            '-signal-json-' +
                            plan().signals.indexOf(slot) +
                            '-error'
                          : prefix + '-signal-why'
                    "
                  >
                    Send {{ slot.name }}
                  </button>
                }
              </form>
            } @else if (wait === "time") {
              <div
                class="sim-section sim-wait"
                role="group"
                [attr.aria-labelledby]="prefix + '-time-title'"
              >
                <h3 [id]="prefix + '-time-title'">Advance time</h3>
                @for (option of advance(); track option.nodeId) {
                  <button
                    type="button"
                    class="sim-preset"
                    [class.primary]="$first"
                    [attr.aria-disabled]="busy() || null"
                    (click)="
                      send({ kind: 'advance_time', seconds: option.seconds })
                    "
                  >
                    {{ option.label }}
                  </button>
                }
              </div>
            }
            <div class="sim-controls">
              <!-- While busy, controls stay focusable (aria-disabled) so focus
                   is not lost; send() ignores clicks until the reply arrives. -->
              <button
                type="button"
                [class.primary]="!wait"
                [disabled]="finished()"
                [attr.aria-disabled]="busy() || null"
                (click)="send({ kind: 'continue' })"
              >
                <weave-icon name="play" />Continue
              </button>
              <button
                type="button"
                [disabled]="finished()"
                [attr.aria-disabled]="busy() || null"
                (click)="send({ kind: 'next' })"
              >
                Step
              </button>
              <button
                type="button"
                class="tertiary"
                [attr.aria-disabled]="busy() || null"
                (click)="restart()"
              >
                Start over
              </button>
            </div>
            @if (wait !== "time") {
              <details class="sim-section">
                <summary>Advance time</summary>
                @for (option of advance(); track option.nodeId) {
                  <button
                    type="button"
                    class="sim-preset"
                    [attr.aria-disabled]="busy() || null"
                    (click)="
                      send({ kind: 'advance_time', seconds: option.seconds })
                    "
                  >
                    {{ option.label }}
                  </button>
                }
                <ng-container [ngTemplateOutlet]="customTime" />
              </details>
            } @else {
              <details class="sim-section">
                <summary>Advance by another amount</summary>
                <ng-container [ngTemplateOutlet]="customTime" />
              </details>
            }
          } @else {
            <div class="sim-controls">
              <button
                type="button"
                [attr.aria-disabled]="busy() || null"
                (click)="restart()"
              >
                Start over
              </button>
            </div>
          }

          @if (selectedStep(); as step) {
            <section
              class="sim-section"
              [attr.aria-labelledby]="prefix + '-selected-title'"
            >
              <h3 [id]="prefix + '-selected-title'">{{ step }}</h3>
              @if (stepData(step); as data) {
                @if (data.input !== undefined) {
                  <p class="sim-data-label">Input</p>
                  <pre class="sim-data">{{ pretty(data.input) }}</pre>
                }
                @if (data.output !== undefined) {
                  <p class="sim-data-label">Output</p>
                  <pre class="sim-data">{{ pretty(data.output) }}</pre>
                }
              } @else {
                <p class="hint">The simulated run hasn't reached this step.</p>
              }
            </section>
          }

          <details class="sim-section">
            <summary>Breakpoints ({{ breakpoints().length }})</summary>
            <p class="hint">Continue pauses before these steps.</p>
            <fieldset class="sim-choices">
              <legend class="sr-only">Pause before</legend>
              @for (step of plan().steps; track step.id) {
                <label class="checkbox-field"
                  ><input
                    type="checkbox"
                    [checked]="breakpointSet().has(step.id)"
                    [attr.aria-disabled]="busy() || null"
                    (click)="holdWhileBusy($event)"
                    (change)="toggleBreakpoint(step.id)"
                  /><span class="sim-node">{{ step.id }}</span></label
                >
              }
            </fieldset>
          </details>

          <details class="sim-section" [open]="finished()">
            <summary>Step results ({{ results().length }})</summary>
            @if (!results().length) {
              <p class="hint">No step has finished yet.</p>
            }
            <dl class="sim-results">
              @for (result of results(); track result.stepId) {
                <dt class="sim-node">{{ result.stepId }}</dt>
                <dd>
                  <pre>{{ pretty(result.output) }}</pre>
                </dd>
              }
              @if (finished() && view.variables["output"] !== undefined) {
                <dt>Workflow output</dt>
                <dd>
                  <pre>{{ pretty(view.variables["output"]) }}</pre>
                </dd>
              }
            </dl>
          </details>

          <details class="sim-section">
            <summary>Activity</summary>
            <ol class="sim-activity">
              @for (entry of recentEvents(); track $index) {
                <li>{{ describe(entry) }}</li>
              }
            </ol>
          </details>
        </div>
      </section>
    }

    <ng-template #customTime>
      <form
        class="sim-advance"
        [attr.aria-label]="'Advance by an amount'"
        (submit)="advanceCustom($event)"
      >
        <label [for]="prefix + '-amount'">Amount</label>
        <input
          [id]="prefix + '-amount'"
          inputmode="numeric"
          autocomplete="off"
          [value]="amount()"
          [attr.aria-invalid]="amount() !== '' && customSeconds() === null"
          [attr.aria-describedby]="
            amount() !== '' && customSeconds() === null
              ? prefix + '-amount-error'
              : null
          "
          (input)="amount.set(valueOf($event))"
        />
        <label class="sr-only" [for]="prefix + '-unit'">Unit</label>
        <select [id]="prefix + '-unit'" (change)="unit.set(numberOf($event))">
          @for (option of units; track option.seconds) {
            <option
              [value]="option.seconds"
              [selected]="option.seconds === unit()"
            >
              {{ option.label }}
            </option>
          }
        </select>
        <button
          type="submit"
          [disabled]="customSeconds() === null"
          [attr.aria-disabled]="busy() || null"
        >
          Advance
        </button>
        @if (amount() !== "" && customSeconds() === null) {
          <p class="error" [id]="prefix + '-amount-error'">
            Enter a whole number, zero or more.
          </p>
        }
      </form>
    </ng-template>
  `,
  styles: [
    `
      /* Busy, not unavailable: the global disabled look, a progress cursor. */
      button[aria-disabled="true"],
      input[aria-disabled="true"] {
        cursor: progress;
      }
      input[aria-disabled="true"] {
        background: var(--disabled-bg);
        color: var(--disabled-ink);
      }
      /* Both tabs share the width; on a narrow panel a label wraps rather
         than scrolling the tab list sideways. */
      .sim-tabs {
        display: flex;
        gap: 4px;
        border-bottom: 1px solid var(--line);
        margin: 12px 0;
      }
      .sim-tabs button {
        flex: 0 1 auto;
        min-width: 0;
        border: 0;
        border-bottom: 3px solid transparent;
        border-radius: 0;
        background: transparent;
        min-height: 36px;
        white-space: normal;
        text-align: start;
        line-height: 1.25;
      }
      .sim-tabs button.selected {
        border-bottom-color: var(--accent);
        color: var(--text);
      }
      .sim-tab-panel {
        display: grid;
        gap: 10px;
      }
      .sim-tab-panel[hidden],
      .sim-result[hidden],
      .sim-signal[hidden],
      .sim-body[hidden] {
        display: none;
      }
      .sim-action {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 10px 12px;
        margin: 0;
        min-width: 0;
        display: grid;
        gap: 8px;
      }
      .sim-action legend {
        display: flex;
        gap: 8px;
        align-items: baseline;
        flex-wrap: wrap;
        padding: 0 4px;
      }
      .sim-action legend small {
        color: var(--muted);
        overflow-wrap: anywhere;
      }
      .sim-editor-tools {
        display: flex;
        justify-content: flex-end;
      }
      button.sim-link {
        border: 0;
        background: transparent;
        color: var(--link);
        min-height: 30px;
        padding: 4px 6px;
        text-decoration: underline;
      }
      .sim-signal {
        border: 0;
        margin: 0;
        padding: 0;
        min-width: 0;
        display: grid;
        gap: 8px;
      }
      .sim-json {
        width: 100%;
        min-height: 120px;
      }
      .sim-json-label {
        font-size: 12px;
        font-weight: 600;
      }
      .sim-blocker {
        margin: 12px 0 0;
      }
      .sim-node {
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      /* The host leaves no box: the panel itself is the grid column the
         designer gives it, in place of the inspector. */
      :host {
        display: contents;
      }
      .sim-panel {
        min-width: 0;
        min-height: 0;
        overflow: auto;
        overscroll-behavior: contain;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        padding: 14px 16px 16px;
      }
      /* Phones: a full-height sheet while expanded, a bar at the bottom
         while folded away. */
      @media (max-width: 767px) {
        .sim-panel {
          position: fixed;
          top: 60px;
          right: 0;
          bottom: 0;
          width: min(400px, calc(100vw - 52px));
          border-radius: 0;
          z-index: 15;
          box-shadow: var(--shadow-2);
        }
        .sim-panel.collapsed {
          top: auto;
          left: 64px;
          right: 12px;
          bottom: 12px;
          width: auto;
          border-radius: var(--radius-md);
        }
      }
      .sim-header {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .sim-header h2 {
        margin: 0;
        font-size: 16px;
      }
      .sim-header h2:focus {
        outline: none;
      }
      .sim-header-tools {
        margin-left: auto;
        display: flex;
      }
      .sim-turned {
        transform: rotate(90deg);
      }

      .sim-body {
        display: grid;
        gap: 10px;
        margin-top: 8px;
      }
      .sim-body .hint {
        margin: 0;
      }
      .sim-now {
        margin: 0;
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: baseline;
        font: var(--type-body);
      }
      .sim-now strong {
        color: var(--text);
      }
      /* What the run waits for: the one place to act now. */
      .sim-wait {
        border: 1px solid var(--warning-bd);
        border-radius: var(--radius-md);
        background: var(--warning-bg);
        padding: var(--space-3);
      }
      .sim-wait h3 {
        color: var(--warning-ink);
      }
      .sim-data-label {
        margin: 0;
        font: var(--type-label);
      }
      .sim-data {
        margin: 0;
        max-height: 180px;
        overflow: auto;
        background: var(--raised);
        padding: 8px;
        border-radius: var(--radius-xs);
        font: var(--type-mono);
      }
      .sim-controls {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }
      .sim-section {
        border-top: 1px solid var(--line);
        padding-top: 10px;
        display: grid;
        gap: 8px;
        min-width: 0;
      }
      .sim-section h3 {
        margin: 0;
        font-size: 13px;
      }
      .sim-section summary {
        cursor: pointer;
        font-weight: 600;
        font-size: 13px;
        min-height: 28px;
      }
      .sim-section > button,
      .sim-preset {
        justify-self: start;
        white-space: normal;
        text-align: left;
      }
      .sim-choices {
        border: 0;
        margin: 0;
        padding: 0;
        min-width: 0;
        display: flex;
        flex-wrap: wrap;
        gap: 6px 16px;
      }
      .sim-choices legend {
        font-size: 12px;
        font-weight: 600;
        padding: 0;
        margin-bottom: 4px;
      }
      .sim-advance {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
      }
      .sim-advance input {
        width: 96px;
      }
      .sim-results {
        margin: 0;
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 4px;
      }
      .sim-results dd {
        margin: 0 0 6px;
      }
      .sim-results pre {
        margin: 0;
        max-height: 180px;
        overflow: auto;
        background: var(--sunken);
        padding: 8px;
        border-radius: var(--radius-xs);
        font-size: 12px;
      }
      .sim-activity {
        margin: 0;
        padding-left: 20px;
        font-size: 12px;
      }
    `,
  ],
})
export class SimulationPanel implements OnChanges {
  /** Compiled artifact JSON (compile result "artifact"); required to start. */
  artifact = input<Json | null>(null);
  /** The debug session returned by create or the last command; null in setup. */
  session = input<DebugSessionData | null>(null);
  /** True while the host waits for the platform; controls are disabled. */
  busy = input(false);
  /** The last failed request, as describeError() returns it. */
  error = input<PlainError | null>(null);
  /** The step selected on the canvas: the panel shows its simulated data. */
  selectedStep = input("");
  start = output<DebugCreateBody>();
  command = output<DebugCommandBody>();
  nodesChange = output<SimulationNodes>();
  close = output<void>();

  prefix = `simulation-${++sequence}`;
  /** Where the expanded panel covers the canvas and acts as a modal sheet. */
  readonly sheetWhen = sheetWhen.simulation;
  /** Escape collapses the panel; focus goes to the control that expands it. */
  readonly collapseToggle = () =>
    document.getElementById(`${this.prefix}-toggle`);
  tabNames = ["input", "actions"] as const;
  units = UNITS;
  tab = signal<"input" | "actions">("input");
  editing = signal(false);
  collapsed = signal(false);
  amount = signal("");
  unit = signal(60);
  signalChoice = signal("");
  breakpoints = signal<string[]>([]);
  plan = computed(() => simulationPlan(this.artifact()));
  showSetup = computed(() => !this.session() || this.editing());
  breakpointSet = computed(() => new Set(this.breakpoints()));
  finished = computed(() =>
    ["succeeded", "failed", "cancelled", "timed_out"].includes(
      this.session()?.view.status ?? "",
    ),
  );
  activeHumans = computed(() => {
    const active = new Set(this.session()?.view.active_nodes ?? []);
    return this.plan().humans.filter((h) => active.has(h.nodeId));
  });
  orderedSignals = computed(() => {
    const signals = this.plan().signals;
    return [
      ...signals.filter((s) => this.isActive(s.nodeId)),
      ...signals.filter((s) => !this.isActive(s.nodeId)),
    ];
  });
  selectedSignal = computed(
    (): SignalSlot | undefined =>
      this.orderedSignals().find((s) => s.nodeId === this.signalChoice()) ??
      this.orderedSignals()[0],
  );
  advance = computed(() => advanceOptions(this.session()?.view, this.plan()));
  results = computed(() => stepResults(this.session()?.view, this.plan()));
  recentEvents = computed(() =>
    [...(this.session()?.view.events ?? [])].slice(-12).reverse(),
  );
  customSeconds = computed(() => advanceSeconds(this.amount(), this.unit()));
  /** Signals the run waits for now. */
  waitingSignals = computed(() =>
    this.plan().signals.filter((s) => this.isActive(s.nodeId)),
  );
  /**
   * What the run waits for, so only that control shows: a person's
   * decision, a signal, or time (a wait or a deadline). "" when nothing
   * waits (paused at a breakpoint, for example): Continue leads then.
   */
  waitKind = computed((): "human" | "signal" | "time" | "" => {
    if (this.activeHumans().length) return "human";
    if (this.waitingSignals().length) return "signal";
    if (this.advance().length) return "time";
    return "";
  });
  /** Beside the canvas from 768 px; a sheet on phones. */
  docked = signal(
    typeof matchMedia === "function"
      ? matchMedia("(min-width: 768px)").matches
      : true,
  );

  inputEditor = signal<ValueEditor | null>(null);
  actionEditors = new Map<string, ValueEditor>();
  signalEditors = new Map<string, ValueEditor>();
  decisionEditors = new Map<string, ValueEditor>();
  chosen = new Map<string, string>();
  /** Human tasks whose decision the simulator accepted; Continue applies it. */
  submitted = new Set<string>();

  /** Where the current error is shown: the setup dialog or the panel. */
  errorScope: "setup" | "panel" = "panel";

  private cdr = inject(ChangeDetectorRef);
  private element = inject(ElementRef<HTMLElement>);
  private injector = inject(Injector);
  private destroyed = false;
  /** A request was just emitted and the host's busy flag has not arrived yet. */
  private emitted = false;
  private confirmedBreakpoints: string[] = [];
  private pendingBreakpoints: string[] | null = null;
  private pendingDecision: string | null = null;
  private sessionId = "";
  private revision = -1;

  constructor() {
    const query =
      typeof matchMedia === "function"
        ? matchMedia("(min-width: 768px)")
        : null;
    const follow = () => this.docked.set(!!query?.matches);
    query?.addEventListener("change", follow);
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      query?.removeEventListener("change", follow);
    });
  }

  ngOnChanges(changes: SimpleChanges) {
    // A failure belongs to whatever was on screen when it arrived: a failed
    // start to the setup dialog, a failed command to the panel.
    if (changes["error"] && this.error())
      this.errorScope = this.showSetup() ? "setup" : "panel";
    if (changes["artifact"]) this.prepare();
    if (changes["session"]) this.sessionChanged();
    if (changes["error"] && this.error()) {
      // The last command failed: show the confirmed state again.
      if (this.pendingBreakpoints) {
        this.pendingBreakpoints = null;
        this.breakpoints.set(this.confirmedBreakpoints);
        this.announceNodes();
      }
      this.pendingDecision = null;
    }
  }

  /** Toggles a breakpoint, for example from a canvas node's context menu. */
  toggleBreakpoint(nodeId: string) {
    // Breakpoints live in the platform's session, so there must be one.
    if (this.blocked() || !this.session()) return;
    const next = new Set(this.breakpoints());
    if (next.has(nodeId)) next.delete(nodeId);
    else next.add(nodeId);
    const body = breakpointsCommand(this.plan(), next);
    const ids = body.kind === "breakpoints" ? body.node_ids : [];
    this.pendingBreakpoints = ids;
    this.breakpoints.set(ids);
    this.holdUntilRendered();
    this.command.emit(body);
    this.announceNodes();
  }

  setupProblem(): string {
    if (!this.artifact())
      return "Compile the workflow before starting a simulation.";
    const input = this.inputEditor();
    const inputProblem = input?.problem();
    if (inputProblem) return `Workflow input: ${inputProblem}`;
    for (const slot of this.plan().actions) {
      const state = this.actionEditors.get(slot.nodeId);
      if (!state || state.skipped) continue;
      const problem = state.problem();
      if (problem) return `Result of ${slot.nodeId}: ${problem}`;
    }
    return "";
  }
  startSimulation() {
    const artifact = this.artifact();
    if (!artifact || this.setupProblem() || this.blocked()) return;
    const results: Record<string, unknown> = {};
    for (const slot of this.plan().actions) {
      const state = this.actionEditors.get(slot.nodeId);
      if (state && !state.skipped) results[slot.nodeId] = state.value;
    }
    this.holdUntilRendered();
    this.start.emit(
      createBody(
        artifact,
        this.inputEditor()?.value ?? {},
        results,
        new Date(),
      ),
    );
  }
  cancelSetup() {
    if (this.session()) this.editing.set(false);
    else this.closePanel();
  }
  /** Clears the canvas highlights, then asks the host to close the panel. */
  closePanel() {
    this.emitNodes(noNodes());
    this.close.emit();
  }
  restart() {
    if (this.blocked()) return;
    this.inputEditor()?.rebase();
    for (const editor of this.actionEditors.values()) editor.rebase();
    this.editing.set(true);
  }
  send(body: DebugCommandBody) {
    if (this.blocked()) return;
    this.holdUntilRendered();
    this.command.emit(body);
  }
  sendSignal(event: Event) {
    event.preventDefault();
    const slot = this.selectedSignal();
    if (!slot) return;
    const state = this.signalEditors.get(slot.nodeId)!;
    if (state.problem()) return;
    this.send(signalCommand(slot, state.value));
  }
  decide(event: Event, human: HumanSlot) {
    event.preventDefault();
    if (this.decisionProblem(human) || this.blocked()) return;
    this.pendingDecision = human.nodeId;
    this.send(
      decisionCommand(
        human,
        this.chosen.get(human.nodeId)!,
        this.decisionEditors.get(human.nodeId)!.value,
      ),
    );
  }
  decisionProblem(human: HumanSlot): string {
    if (!this.chosen.get(human.nodeId)) return "Choose a decision.";
    return this.decisionEditors.get(human.nodeId)?.problem() ?? "";
  }
  /** Keeps a checkbox unchanged while a command is in flight. */
  holdWhileBusy(event: Event) {
    if (this.blocked()) event.preventDefault();
  }
  advanceCustom(event: Event) {
    event.preventDefault();
    const seconds = this.customSeconds();
    if (seconds === null) return;
    this.send({ kind: "advance_time", seconds });
  }
  tabKey(event: KeyboardEvent) {
    const order = this.tabNames;
    const index = order.indexOf(this.tab());
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % order.length;
    else if (event.key === "ArrowLeft")
      next = (index - 1 + order.length) % order.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = order.length - 1;
    else return;
    event.preventDefault();
    this.tab.set(order[next]);
    document.getElementById(`${this.prefix}-tab-${order[next]}`)?.focus();
  }
  isActive(nodeId: string) {
    return !!this.session()?.view.active_nodes.includes(nodeId);
  }
  errorMessage() {
    const error = this.error();
    return error ? simulationMessage(error.code, error.message) : "";
  }
  diagnosticMessage(problem: { code: string; message?: string }) {
    return simulationMessage(
      problem.code,
      problem.message || "The simulated run stopped with a problem.",
    );
  }
  label(status: string) {
    return statusLabel(status);
  }
  /** The status pill's tone: waiting is gold, a failure red, done jade. */
  tone(status: string) {
    if (["failed", "timed_out", "cancelled"].includes(status)) return "danger";
    if (["waiting", "paused"].includes(status)) return "warning";
    if (status === "succeeded") return "success";
    if (status === "running") return "info";
    return null;
  }
  /** What the simulated run recorded for a step: its input and output. */
  stepData(stepId: string): { input?: unknown; output?: unknown } | null {
    const variables = this.session()?.view.variables;
    if (!variables) return null;
    const pick = (steps: unknown) => {
      const state =
        steps && typeof steps === "object"
          ? (steps as Json)[stepId]
          : undefined;
      return state && typeof state === "object" ? (state as Json) : null;
    };
    let state = pick(variables["steps"]);
    const branches = variables["branches"];
    if (!state && branches && typeof branches === "object")
      for (const branch of Object.values(branches as Json)) {
        state =
          branch && typeof branch === "object"
            ? pick((branch as Json)["steps"])
            : null;
        if (state) break;
      }
    if (!state || (!("input" in state) && !("output" in state))) return null;
    return {
      ...("input" in state ? { input: state["input"] } : {}),
      ...("output" in state ? { output: state["output"] } : {}),
    };
  }
  detail(status: string) {
    return statusDetail(status);
  }
  describe(event: SimulationEvent) {
    return eventText(event);
  }
  clock(iso: string) {
    const time = Date.parse(iso);
    return Number.isFinite(time) ? new Date(time).toLocaleString() : iso;
  }
  pretty(value: unknown) {
    return JSON.stringify(value, null, 2) ?? "null";
  }
  textOf(event: Event) {
    return (event.target as HTMLTextAreaElement).value;
  }
  valueOf(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  numberOf(event: Event) {
    return Number((event.target as HTMLSelectElement).value);
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }

  private prepare() {
    const plan = this.plan();
    const previous = this.inputEditor();
    const inputStart =
      previous &&
      JSON.stringify(previous.schema) === JSON.stringify(plan.inputSchema)
        ? previous.value
        : (sampleValue(plan.inputSchema) ?? {});
    this.inputEditor.set(new ValueEditor(plan.inputSchema, inputStart));
    const actions = new Map<string, ValueEditor>();
    for (const slot of plan.actions) {
      const old = this.actionEditors.get(slot.nodeId);
      actions.set(
        slot.nodeId,
        old && JSON.stringify(old.schema) === JSON.stringify(slot.outputSchema)
          ? old
          : new ValueEditor(slot.outputSchema, sampleValue(slot.outputSchema)),
      );
    }
    this.actionEditors = actions;
    this.resetControls();
    if (!plan.actions.length) this.tab.set("input");
  }
  private resetControls() {
    const plan = this.plan();
    this.signalEditors = new Map(
      plan.signals.map((s) => [
        s.nodeId,
        new ValueEditor(s.payloadSchema, sampleValue(s.payloadSchema)),
      ]),
    );
    this.decisionEditors = new Map(
      plan.humans.map((h) => [
        h.nodeId,
        new ValueEditor(h.formSchema, sampleValue(h.formSchema)),
      ]),
    );
    this.chosen = new Map();
  }
  private sessionChanged() {
    const session = this.session();
    if (!session) {
      this.sessionId = "";
      this.revision = -1;
      // No session, nothing to highlight; breakpoints went with the session.
      this.breakpoints.set([]);
      this.announceNodes();
      return;
    }
    const host = this.element.nativeElement as HTMLElement;
    const hadFocus = host.contains(document.activeElement);
    const fresh = session.id !== this.sessionId;
    if (fresh) {
      this.sessionId = session.id;
      this.editing.set(false);
      this.collapsed.set(false);
      this.breakpoints.set([]);
      this.confirmedBreakpoints = [];
      this.pendingBreakpoints = null;
      this.pendingDecision = null;
      this.submitted = new Set();
      this.resetControls();
    } else if (session.revision !== this.revision) {
      if (this.pendingBreakpoints) {
        this.confirmedBreakpoints = this.pendingBreakpoints;
        this.pendingBreakpoints = null;
      }
      if (this.pendingDecision) this.submitted.add(this.pendingDecision);
      this.pendingDecision = null;
      // A decision is spent once its task is no longer waiting.
      const active = new Set(session.view.active_nodes);
      for (const human of this.plan().humans)
        if (!active.has(human.nodeId)) {
          this.chosen.delete(human.nodeId);
          this.submitted.delete(human.nodeId);
        }
    }
    this.revision = session.revision;
    this.announceNodes();
    this.cdr.markForCheck();
    // Land keyboard users on the panel when it opens (after the setup dialog
    // returned focus to its opener), and whenever the control they used
    // disappeared or became disabled with this update.
    setTimeout(() => {
      const active = document.activeElement;
      const lost =
        !active ||
        active === document.body ||
        (active instanceof HTMLButtonElement && active.disabled);
      if (fresh || (hadFocus && lost))
        host.querySelector<HTMLElement>(`#${this.prefix}-title`)?.focus();
    });
  }
  /** True while a request is in flight; controls ignore clicks then. */
  private blocked() {
    return this.busy() || this.emitted;
  }
  /**
   * The host's busy flag reaches this component only on the next render, so
   * the second click of a double click would otherwise send the same request
   * twice (a second session, or a command rejected for a stale revision).
   */
  private holdUntilRendered() {
    this.emitted = true;
    afterNextRender(() => (this.emitted = false), { injector: this.injector });
  }
  private announceNodes() {
    const view = this.session()?.view;
    const live = new Set([
      ...(view?.current_nodes ?? []),
      ...(view?.active_nodes ?? []),
    ]);
    const nodes: SimulationNodes = {
      status: view?.status ?? "",
      current: [...(view?.current_nodes ?? [])],
      active: [...(view?.active_nodes ?? [])],
      breakpoints: [...this.breakpoints()],
      done: stepResults(view, this.plan())
        .map((result) => result.stepId)
        .filter((id) => !live.has(id)),
    };
    // Emitted after the current change detection pass, so the host can store
    // it in plain fields without a changed-after-checked error.
    queueMicrotask(() => this.emitNodes(nodes));
  }
  private emitNodes(nodes: SimulationNodes) {
    if (!this.destroyed) this.nodesChange.emit(nodes);
  }
}
