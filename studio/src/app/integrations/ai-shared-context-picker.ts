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
import type { ScopeEntry } from "../forms/core/scope";
import {
  availableAiResults,
  sharedAiResults,
  withSharedAiResults,
} from "./ai-shared-context";

@Component({
  selector: "weave-ai-shared-context-picker",
  standalone: true,
  template: `
    <fieldset>
      <legend>Shared AI context</legend>
      <p class="hint">
        Choose earlier AI results this step can read. They stay within this
        execution and survive pauses and retries. Your existing context is kept
        under data; selected results appear under sharedAiResults.
      </p>
      @for (ref of choices; track ref.ref) {
        <label
          ><input
            type="checkbox"
            [checked]="selected.includes(ref.stepId!)"
            (change)="toggle(ref.stepId!, $event)"
          />Use result from {{ ref.stepId }}</label
        >
      } @empty {
        <p class="hint">
          No earlier AI result is available here. Add an AI step earlier in this
          path, or use the context editor below for workflow inputs and other
          results.
        </p>
      }
      @for (id of unavailable; track id) {
        <p role="alert">
          The result from {{ id }} is no longer available before this step.
        </p>
      }
      @if (unavailable.length) {
        <button type="button" (click)="removeUnavailable()">
          Remove unavailable results
        </button>
      }
      <p class="hint">
        Only selected structured results are sent to this step's provider.
        Conversations from Lumi and other executions are never included.
      </p>
      @if (error) {
        <p class="field-error" role="alert">{{ error }}</p>
      }
    </fieldset>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    fieldset {
      display: grid;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin: 0;
      min-width: 0;
    }
    legend {
      font-weight: 600;
      padding: 0 4px;
    }
    label {
      display: flex;
      align-items: center;
      gap: 8px;
      overflow-wrap: anywhere;
    }
    input {
      flex: 0 0 auto;
    }
    p {
      margin: 0;
      overflow-wrap: anywhere;
    }
  `,
})
export class AiSharedContextPicker {
  value = input.required<unknown>();
  references = input<ScopeEntry[]>([]);
  valueChange = output<unknown>();
  error = "";
  get choices() {
    return availableAiResults(this.references());
  }
  get selected() {
    return sharedAiResults(this.value());
  }
  get unavailable() {
    return this.selected.filter(
      (id) => !this.choices.some((ref) => ref.stepId === id),
    );
  }
  toggle(id: string, event: Event) {
    const checked = (event.target as HTMLInputElement).checked;
    if (
      !this.update(
        checked
          ? [...this.selected, id]
          : this.selected.filter((item) => item !== id),
      )
    )
      (event.target as HTMLInputElement).checked = this.selected.includes(id);
  }
  removeUnavailable() {
    this.update(this.selected.filter((id) => !this.unavailable.includes(id)));
  }
  private update(selected: string[]) {
    try {
      this.valueChange.emit(
        withSharedAiResults(this.value(), selected, this.references()),
      );
      this.error = "";
      return true;
    } catch (error) {
      this.error =
        error instanceof Error
          ? error.message
          : "Could not update shared context.";
      return false;
    }
  }
}
