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
// The inspector's sections for a "Call an action" step: the action, its
// connection slot, its input and its output. Loaded lazily (@defer) so the
// initial bundle stays small; the shell passes itself as the host and keeps
// owning the state.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../icon";
import { CatalogPicker } from "../integrations/catalog-picker";
import { TaskForm } from "../task-form";
import type { App } from "../app";

@Component({
  selector: "weave-action-inspector",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [Icon, CatalogPicker, TaskForm],
  template: `@let h = host();
    @if (h.selected; as selected) {
      <!-- One contract: the action, its connection, its input and output. -->
      <div class="integration-contract action-sections">
        <details
          class="inspector-section"
          [open]="h.sectionOpen('action')"
          (toggle)="h.sectionToggled('action', $event)"
        >
          <summary id="integration-heading" tabindex="-1">Action</summary>
          <section class="section-body" aria-labelledby="integration-heading">
            @switch (h.integrationState) {
              @case ("offline") {
                <div class="integration-state">
                  <p>
                    <strong>Working locally.</strong> Enter the action version
                    to call in the properties below. Connect to a platform in
                    Settings to choose from published actions.
                  </p>
                  <button type="button" (click)="h.navigate('settings')">
                    Open Settings
                  </button>
                </div>
              }
              @case ("identity") {
                <div class="integration-state">
                  <p>
                    Studio could not confirm your account's permissions, so
                    published actions are hidden. Check the platform connection
                    in Settings.
                  </p>
                  <button type="button" (click)="h.navigate('settings')">
                    Open Settings
                  </button>
                </div>
              }
              @case ("forbidden") {
                <div class="integration-state">
                  <p>
                    Your account cannot read the action catalog in this
                    workspace. Ask an administrator for catalog access, or enter
                    the action version below.
                  </p>
                  <small class="support-code"
                    >Needed permission: catalog.read</small
                  >
                </div>
              }
              @case ("loading") {
                <p class="integration-state" role="status">
                  <span class="loading-spinner small"></span>Loading published
                  actions…
                </p>
              }
              @case ("error") {
                <div class="integration-state error-state" role="alert">
                  <p>
                    Published actions could not be loaded.
                    {{ h.catalogError?.message }}
                  </p>
                  @if (h.catalogError?.code) {
                    <small class="support-code"
                      >Support code: {{ h.catalogError.code }}</small
                    >
                  }
                  <button
                    type="button"
                    (click)="h.loadActionCatalog(false, true)"
                  >
                    Try again
                  </button>
                </div>
              }
              @case ("empty") {
                <div class="integration-state">
                  <p>
                    This project has no published actions yet.
                    {{
                      h.can("definition.publish")
                        ? "Create one from an API, or publish an action version and refresh the list."
                        : "Ask a developer to publish an action, then refresh the list."
                    }}
                  </p>
                  <button
                    type="button"
                    (click)="h.loadActionCatalog(false, true)"
                  >
                    Refresh actions
                  </button>
                </div>
              }
              @case ("ready") {
                @defer (on immediate) {
                  <weave-catalog-picker
                    [host]="h"
                    [value]="h.bufferedUses()"
                    [disabled]="h.model.readonly || !h.bufferReadable"
                    (choose)="h.chooseCatalogAction($event.id ?? '')"
                  />
                } @placeholder {
                  <p class="hint">Loading the action list…</p>
                }
              }
            }
            @if (
              !h.model.readonly &&
              h.bufferReadable &&
              h.canCreateIntegration() &&
              (!h.bufferedUses() || h.bufferedUses() === "your-action@1.0.0")
            ) {
              <div class="integration-state integration-create">
                <p>
                  No action yet? Describe the API request, or import an OpenAPI
                  document, and Studio builds the action with no code.
                </p>
                <button type="button" (click)="h.openApiBuilder('step')">
                  <weave-icon name="action" />New API action
                </button>
              </div>
            }
            @if (h.contractState === "loading") {
              <p class="hint" role="status">
                <span class="loading-spinner small"></span>Loading the action's
                details…
              </p>
            } @else if (h.contractState === "error") {
              <div class="integration-state error-state" role="alert">
                <p>
                  The action's details could not be loaded.
                  {{ h.contractError }}
                </p>
                <button type="button" (click)="h.retryContract()">
                  Try again
                </button>
              </div>
            }
            @if (h.actionContract) {
              <h4>What this action needs</h4>
              <dl class="integration-requirements">
                <dt>Runs on</dt>
                <dd>
                  {{
                    h.actionImplementation()["kind"] === "worker"
                      ? "Worker"
                      : "Connector"
                  }}
                  <code>{{
                    h.actionImplementation()["kind"] === "worker"
                      ? h.actionImplementation()["taskType"] +
                        " " +
                        h.actionImplementation()["taskVersion"]
                      : h.actionImplementation()["uses"]
                  }}</code>
                </dd>
                @if (h.actionImplementation()["kind"] !== "worker") {
                  <dt>Operation</dt>
                  <dd>
                    <code>{{ h.actionImplementation()["action"] }}</code>
                  </dd>
                }
                <dt>Connection</dt>
                <dd>{{ h.connectionRequirementLabel() }}</dd>
                <dt>Side effect</dt>
                <dd>{{ h.sideEffectLabel() }}</dd>
                <dt>Timeout</dt>
                <dd>{{ h.actionSpec()["timeoutSeconds"] }} seconds</dd>
                <dt>Retry</dt>
                <dd>{{ h.retryLabel() }}</dd>
              </dl>
              <p class="hint">
                Timeout and retry belong to this published action version.
                Publish a new version to change them.
              </p>
            }
            @if (h.integrationIssues(); as issues) {
              @if (issues.length) {
                <div class="integration-issues" role="status">
                  <strong>Configuration incomplete</strong>
                  <ul>
                    @for (issue of issues; track issue) {
                      <li>{{ issue }}</li>
                    }
                  </ul>
                </div>
              }
            }
          </section>
        </details>
        <details
          class="inspector-section"
          [open]="h.sectionOpen('connection')"
          (toggle)="h.sectionToggled('connection', $event)"
        >
          <summary>Connection</summary>
          <div class="section-body">
            <label
              >Connection slot<select
                aria-label="Connection slot"
                [disabled]="h.model.readonly || !h.bufferReadable"
                (change)="h.chooseConnectionSlot($event)"
              >
                <option value="" [selected]="!h.bufferedConnection()">
                  {{
                    h.actionContract && h.actionRequirement()
                      ? h.actionRequirement()?.required === false
                        ? "No connection"
                        : "Choose a connection slot"
                      : "No connection"
                  }}
                </option>
                @for (slot of h.compatibleSlots(); track slot.name) {
                  <option
                    [value]="slot.name"
                    [selected]="slot.name === h.bufferedConnection()"
                  >
                    {{ slot.name }} · {{ slot.connector }}
                  </option>
                }
                @if (
                  h.bufferedConnection() &&
              !h.compatibleSlots().some(
                (slot) => slot.name === h.bufferedConnection()
              )
                ) {
                  <option [value]="h.bufferedConnection()" selected>
                    {{ h.bufferedConnection() }} (not compatible)
                  </option>
                }
              </select></label
            >
            @if (h.profile && h.httpSlot()) {
              <div class="integration-state">
                <p>
                  Each environment binds the slot
                  <code>{{ h.httpSlot() }}</code> to an API connection when a
                  version is activated. Create one if this API has no connection
                  yet.
                </p>
                <button
                  type="button"
                  (click)="h.openConnectionDialog(h.httpSlot())"
                >
                  Create a connection for this API
                </button>
              </div>
            }
            @if (h.actionContract && !h.actionRequirement()) {
              <p class="hint">This action does not use a connection.</p>
            } @else if (!h.compatibleSlots().length) {
              <p class="hint">
                {{
                  h.actionContract && h.actionRequirement()
                    ? "This workflow has no slot for " +
                      h.actionRequirement()?.connector +
                      " yet."
                    : "This workflow declares no connection slots."
                }}
                A connection slot is a named placeholder. When you activate a
                version, you choose a real connection for each slot in that
                environment.
              </p>
            }
            @if (!h.actionContract || h.actionRequirement()) {
              <details
                class="add-slot"
                [open]="!!h.actionRequirement() && !h.compatibleSlots().length"
              >
                <summary>Add connection slot</summary>
                <div class="add-slot-fields">
                  <label
                    >Slot name<input
                      [value]="
                        h.newSlotNameEdited
                          ? h.newSlotName
                          : h.suggestedSlotName()
                      "
                      (input)="
                        h.newSlotName = h.value($event);
                        h.newSlotNameEdited = true
                      "
                  /></label>
                  @if (h.actionContract && h.actionRequirement()) {
                    <p class="hint">
                      Connector:
                      <code>{{ h.actionRequirement()?.connector }}</code>
                    </p>
                  } @else {
                    <label
                      >Connector<input
                        placeholder="crm@1.0.0"
                        [value]="h.newSlotConnector"
                        (input)="h.newSlotConnector = h.value($event)"
                    /></label>
                  }
                  @if (
                    !h.actionContract ||
                    h.actionRequirement()?.required === false
                  ) {
                    <label class="checkbox-field"
                      ><input
                        type="checkbox"
                        [checked]="h.newSlotRequired"
                        (change)="h.newSlotRequired = !h.newSlotRequired"
                      />Required at activation</label
                    >
                  }
                  <button
                    type="button"
                    [disabled]="h.model.readonly || !h.bufferReadable"
                    (click)="h.addConnectionSlot()"
                  >
                    Add slot
                  </button>
                  @if (h.slotError) {
                    <p class="error" role="alert">
                      {{ h.slotError }}
                    </p>
                  }
                </div>
              </details>
            }
          </div>
        </details>
        <details
          class="inspector-section"
          [open]="h.sectionOpen('input')"
          (toggle)="h.sectionToggled('input', $event)"
        >
          <summary>Input</summary>
          <div class="section-body">
            @if (h.actionContract && h.actionInputFields()) {
              @if (h.actionInputMode === "fields") {
                <form
                  class="action-input-form"
                  data-field="with"
                  aria-label="Action input"
                  (submit)="$event.preventDefault()"
                  (input)="h.touchInspector('step')"
                  (change)="h.touchInspector('step')"
                  (click)="h.touchInspector('step', $event)"
                >
                  <!-- Disabled while Advanced JSON holds text that is not a step. -->
                  <fieldset
                    class="action-input-fields"
                    [disabled]="!h.bufferReadable"
                  >
                    @for (key of [selected.step.id]; track key) {
                      @defer (on immediate) {
                        <weave-task-form
                          [schema]="h.actionInputSchema()"
                          [bindings]="true"
                          [expression]="h.actionInitialInput"
                          [scope]="h.referenceContext"
                          (expressionChange)="h.actionInputChange($event)"
                          (missingChange)="h.actionMissing = $event"
                          (validityChange)="h.actionInputValid.set($event)"
                        />
                      } @placeholder {
                        <p class="hint">Loading the input fields…</p>
                      }
                    }
                  </fieldset>
                </form>
                <p class="hint">
                  Each input is a value, data from the workflow input or an
                  earlier step, or a formula.
                  <button
                    type="button"
                    class="text-link"
                    [disabled]="!h.bufferReadable"
                    (click)="h.customInput(true)"
                  >
                    Write one expression for the whole input
                  </button>
                </p>
              } @else {
                <p class="hint">
                  The input is one custom expression, edited as Input in the
                  properties below.
                  <button
                    type="button"
                    class="text-link"
                    [disabled]="!h.bufferReadable"
                    (click)="h.customInput(false)"
                  >
                    Edit the input field by field
                  </button>
                </p>
              }
            } @else {
              <p class="hint">
                Map the input with the Input expression in the properties below.
                Use Data to pass the workflow input or an earlier step's output.
              </p>
            }
          </div>
        </details>
        <details
          class="inspector-section"
          [open]="h.sectionOpen('output')"
          (toggle)="h.sectionToggled('output', $event)"
        >
          <summary>Output</summary>
          <div class="section-body">
            <p class="hint">
              Later steps can read this action's result at
              <code>/steps/{{ selected.step.id }}/output</code>.
            </p>
            <div class="output-actions">
              <button type="button" (click)="h.copyOutputReference()">
                Copy output reference
              </button>
              <button
                type="button"
                (click)="h.useActionOutput()"
                [disabled]="
                  h.model.readonly ||
                  !h.canApply ||
                  !h.actionContract ||
                  selected.owner !== 'root'
                "
              >
                Use action output as workflow result
              </button>
            </div>
            @if (selected.owner !== "root") {
              <p class="hint">
                For nested actions, expose output through the enclosing branch
                output first.
              </p>
            }
            @if (h.actionContract) {
              <details>
                <summary>Output schema</summary>
                <pre>{{ h.pretty(h.actionSpec()["outputSchema"]) }}</pre>
              </details>
            }
          </div>
        </details>
      </div>
    }`,
})
export class ActionInspector {
  /** The editor shell. */
  host = input.required<App>();
}
