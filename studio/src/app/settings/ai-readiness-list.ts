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
// The AI readiness checklist: one row per fact the platform checked, with the
// fix the person may run. The page runs the fixes; this list only shows them.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../icon";
import type { FixAction, ReadinessRow } from "../integrations/ai-models-model";

@Component({
  selector: "weave-ai-readiness-list",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<ul class="readiness-list">
    @for (row of rows(); track row.id; let index = $index) {
      @let action = actions()[index];
      <li class="readiness-row" [attr.data-status]="row.status">
        <weave-icon
          class="readiness-icon"
          [name]="row.icon"
          [size]="16"
          [attr.data-tone]="row.tone"
        />
        <span class="readiness-text"
          ><span class="readiness-label">{{ row.label }}</span
          ><span class="readiness-detail"> · {{ row.detail }}</span></span
        >
        @if (action) {
          @if (action.kind === "button") {
            <button
              type="button"
              class="readiness-action"
              [disabled]="running() !== ''"
              [attr.aria-busy]="running() === row.id ? 'true' : null"
              (click)="act.emit(row)"
            >
              {{ running() === row.id ? "Working…" : action.label }}
            </button>
          } @else if (action.kind === "link") {
            <button
              type="button"
              class="tertiary readiness-action"
              (click)="act.emit(row)"
            >
              {{ action.label }}
            </button>
          } @else if (action.kind === "copy") {
            <span class="readiness-command">
              <code>{{ action.command }}</code>
              <button
                type="button"
                class="tertiary"
                [attr.aria-label]="'Copy ' + action.command"
                (click)="copy.emit(action.command)"
              >
                Copy
              </button>
            </span>
          } @else {
            <span class="readiness-ask">{{ action.label }}</span>
          }
        }
        @if (failures()[row.id]; as failure) {
          <p class="field-error readiness-error" role="alert">{{ failure }}</p>
        }
      </li>
    }
  </ul>`,
  styles: `
    .readiness-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
    }
    .readiness-row {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr) auto;
      align-items: center;
      column-gap: var(--space-2);
      row-gap: var(--space-1);
      min-height: var(--control-sm);
      padding: var(--space-1) 0;
      border-bottom: 1px solid var(--line);
    }
    .readiness-icon[data-tone="success"] {
      color: var(--success-ink);
    }
    .readiness-icon[data-tone="warning"] {
      color: var(--warning-ink);
    }
    .readiness-icon[data-tone="neutral"] {
      color: var(--muted);
    }
    .readiness-text {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .readiness-detail,
    .readiness-ask {
      color: var(--muted);
    }
    .readiness-ask {
      font: var(--type-small);
    }
    .readiness-command,
    .readiness-error {
      grid-column: 2 / -1;
      margin: 0;
    }
    .readiness-command {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      min-width: 0;
    }
    .readiness-command code {
      flex: 1 1 auto;
      min-width: 0;
      overflow-wrap: anywhere;
      font: var(--type-mono);
      background: var(--sunken);
      padding: var(--space-1) var(--space-2);
      border-radius: var(--radius-xs);
    }
    @media (max-width: 767px), (pointer: coarse) {
      .readiness-row {
        min-height: var(--control-touch);
      }
    }
  `,
})
export class AiReadinessList {
  rows = input.required<ReadinessRow[]>();
  actions = input.required<(FixAction | null)[]>();
  /** The item whose fix is running, or "". */
  running = input("");
  failures = input<Record<string, string>>({});
  act = output<ReadinessRow>();
  copy = output<string>();
}
