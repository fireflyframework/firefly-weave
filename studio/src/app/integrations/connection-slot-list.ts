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
// One connection-slot editor is shared by workflow settings and action steps.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from "@angular/core";
import type { App } from "../app";
import { slotValidation } from "./connection-slots";
import type { WorkflowSlot } from "./slot-binding";

@Component({
  selector: "weave-connection-slots",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `@let h = host();
    <section class="slot-list" role="region" aria-label="Connection slots">
      <p class="hint">
        A slot names the connection this workflow needs. Choose the real
        connection when you activate the workflow.
      </p>
      @for (slot of h.workflowSlots; track slot.name) {
        <fieldset
          class="slot-row"
          [disabled]="h.editingLocked || h.model.readonly"
        >
          <legend>{{ slot.name }}</legend>
          <label
            >Slot name<input
              [value]="slot.name"
              (change)="edit(slot, 'name', $event)"
          /></label>
          <label
            >Connector<input
              [value]="slot.connector"
              placeholder="connector-name@1.0.0"
              list="slot-connectors"
              (change)="edit(slot, 'connector', $event)"
          /></label>
          <label class="slot-required"
            ><input
              type="checkbox"
              role="switch"
              [checked]="slot.required"
              (change)="edit(slot, 'required', $event)"
            />Required</label
          >
          <div class="slot-impact">
            <span
              >Used by {{ h.slotUseCount(slot.name) }}
              {{ h.slotUseCount(slot.name) === 1 ? "step" : "steps" }}</span
            ><button
              type="button"
              [attr.aria-label]="'Remove ' + slot.name"
              (click)="h.removeConnectionSlot(slot.name)"
            >
              Remove
            </button>
          </div>
          @if (errors()[slot.name]) {
            <p class="error" role="alert">{{ errors()[slot.name] }}</p>
          }
        </fieldset>
      }
      <datalist id="slot-connectors">
        @for (connector of h.slotConnectorOptions(); track connector) {
          <option [value]="connector"></option>
        }
      </datalist>
      @if (!h.workflowSlots.length) {
        <p class="hint">No connection slots yet.</p>
      }
      @if (!h.selected || !h.actionRequirement()) {
        <details>
          <summary>Add connection slot</summary>
          <div class="slot-row">
            <label
              >New slot name<input
                [value]="h.newSlotName"
                (input)="
                  h.newSlotName = h.value($event); h.newSlotNameEdited = true
                "
                placeholder="customer-system"
            /></label>
            <label
              >New slot connector<input
                [value]="h.newSlotConnector"
                (input)="h.newSlotConnector = h.value($event)"
                placeholder="connector-name@1.0.0"
                list="slot-connectors"
            /></label>
            <label class="slot-required"
              ><input
                type="checkbox"
                [checked]="h.newSlotRequired"
                (change)="h.newSlotRequired = !h.newSlotRequired"
              />Required</label
            >
            <button
              type="button"
              [disabled]="h.editingLocked || h.model.readonly"
              (click)="h.addConnectionSlot()"
            >
              Add slot
            </button>
            @if (h.slotError) {
              <p class="error" role="alert">{{ h.slotError }}</p>
            }
          </div>
        </details>
      }
    </section>`,
  styles: [
    `
      :host,
      .slot-list {
        display: block;
        min-width: 0;
      }
      .slot-row {
        display: grid;
        gap: 12px;
        margin: 12px 0;
        padding: 12px;
        border: 1px solid var(--border);
        border-radius: 8px;
        min-width: 0;
      }
      legend {
        font-weight: 600;
        overflow-wrap: anywhere;
      }
      label {
        display: grid;
        gap: 6px;
        font-size: 13px;
      }
      input {
        min-width: 0;
        max-width: 100%;
      }
      .slot-required {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .slot-required input {
        width: 24px;
        height: 24px;
      }
      .slot-impact {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        font-size: 13px;
      }
      summary {
        cursor: pointer;
        padding: 8px 0;
        font-weight: 600;
      }
    `,
  ],
})
export class ConnectionSlotList {
  host = input.required<App>();
  errors = signal<Record<string, string>>({});
  edit(
    slot: WorkflowSlot,
    key: "name" | "connector" | "required",
    event: Event,
  ) {
    const input = event.target as HTMLInputElement;
    const next = {
      ...slot,
      [key]: key === "required" ? input.checked : input.value.trim(),
    };
    const error = slotValidation(next, this.host().workflowSlots, slot.name);
    this.errors.update((errors) => ({ ...errors, [slot.name]: error }));
    if (!error) void this.host().updateConnectionSlot(slot.name, next);
  }
}
