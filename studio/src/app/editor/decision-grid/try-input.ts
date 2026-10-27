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
// The Try an input panel beside the grid: a form over the table's input
// fields, the live answer, then Weave's check. What is typed lives here only.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../icon";
import { TaskForm } from "../../task-form";
import type { Schema } from "../../forms/core/scope";
import type { TryState } from "./try-input-service";
import { BUSY, UNREACHABLE } from "./try-input-service";

@Component({
  selector: "weave-try-input",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, TaskForm],
  styles: `
    :host {
      display: block;
      --dg-row: var(--control-sm);
    }
    @media (max-width: 767px), (pointer: coarse) {
      :host {
        --dg-row: var(--control-touch);
      }
    }
    .dg-try {
      display: grid;
      gap: var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--surface);
    }
    .dg-try-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
    }
    .dg-try-head h4,
    .dg-try-result-title {
      margin: 0;
      font: var(--type-label);
      color: var(--text);
    }
    .dg-try-head .icon-button {
      width: var(--dg-row);
      min-width: var(--dg-row);
      height: var(--dg-row);
      min-height: var(--dg-row);
    }
    .dg-try-note,
    .dg-try-matched,
    .dg-try-pending,
    .dg-try-offline {
      margin: 0;
      font: var(--type-caption);
      color: var(--muted);
    }
    .dg-try-output {
      margin: 0;
      max-height: 200px;
      overflow: auto;
      padding: var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--raised);
      font: var(--type-mono);
      color: var(--text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .dg-try-refused {
      display: flex;
      align-items: flex-start;
      gap: var(--space-1);
      margin: 0;
      font: var(--type-caption);
      color: var(--danger-ink);
    }
  `,
  template: `
    <section class="dg-try" aria-labelledby="dg-try-title">
      <header class="dg-try-head">
        <h4 id="dg-try-title">Try an input</h4>
        <button
          type="button"
          class="icon-button"
          aria-label="Close Try an input"
          (click)="closed.emit()"
        >
          <weave-icon name="close" [size]="16" />
        </button>
      </header>
      @if (fromTestData()) {
        <p class="dg-try-note">From the step's test data</p>
      }
      <weave-task-form
        [schema]="schema()"
        [initialData]="initial()"
        (dataChange)="changed.emit($event)"
      />
      <h5 class="dg-try-result-title">Result</h5>
      @if (shown(); as answer) {
        <pre class="dg-try-output" tabindex="0" aria-label="Result as JSON">{{
          json(answer.output)
        }}</pre>
        <p class="dg-try-matched">
          {{
            answer.usedDefault
              ? "Matched: Otherwise"
              : answer.matched.length
                ? "Matched: " + answer.matched.join(", ")
                : "Matched: no rule"
          }}
        </p>
      }
      <div class="dg-try-check">
        @switch (checkKind()) {
          @case ("checking") {
            <span class="dg-try-pending">Checking with Weave…</span>
          }
          @case ("answer") {
            <span class="pill" data-tone="neutral">Checked by Weave</span>
          }
          @case ("refused") {
            <p class="dg-try-refused" role="status">
              <weave-icon name="warning" [size]="16" />{{ refusedText() }}
            </p>
          }
          @case ("offline") {
            <p class="dg-try-offline">{{ unreachable }}</p>
          }
          @case ("busy") {
            <p class="dg-try-offline">{{ busy }}</p>
          }
        }
      </div>
      <p class="sr-only" aria-live="polite">{{ announcement() }}</p>
    </section>
  `,
})
export class TryInput {
  readonly schema = input.required<Schema>();
  readonly initial = input<Record<string, unknown>>({});
  readonly fromTestData = input(false);
  readonly state = input.required<TryState>();
  readonly changed = output<Record<string, unknown>>();
  readonly closed = output<void>();
  readonly unreachable = UNREACHABLE;
  readonly busy = BUSY;

  /** Weave's answer once it has checked; until then, Studio's live one. */
  shown(): { output: unknown; matched: string[]; usedDefault: boolean } | null {
    const { checked, live } = this.state();
    if (checked && typeof checked === "object" && "answer" in checked)
      return {
        output: checked.answer.output,
        matched: checked.answer.matched_rule_ids,
        usedDefault: checked.answer.used_default,
      };
    if (checked && typeof checked === "object" && "refused" in checked)
      return null;
    return live?.ok
      ? {
          output: live.output,
          matched: live.matched,
          usedDefault: live.usedDefault,
        }
      : null;
  }
  json(value: unknown): string {
    return JSON.stringify(value, null, 2);
  }
  checkKind(): "checking" | "answer" | "refused" | "offline" | "busy" | null {
    const checked = this.state().checked;
    if (checked === "checking") return "checking";
    if (checked === "busy-host") return "busy";
    if (checked === "offline-host") return "offline";
    if (checked && "answer" in checked) return "answer";
    if (checked && "refused" in checked) return "refused";
    return null;
  }
  refusedText(): string {
    const checked = this.state().checked;
    return checked && typeof checked === "object" && "refused" in checked
      ? checked.refused.message
      : "";
  }
  /** Said once per change, after Weave's check. */
  announcement(): string {
    const checked = this.state().checked;
    if (!checked || checked === "checking") return "";
    if (checked === "offline-host") return UNREACHABLE;
    if (checked === "busy-host") return BUSY;
    if ("refused" in checked) return checked.refused.message;
    const matched = checked.answer.matched_rule_ids;
    return `Checked by Weave: ${
      checked.answer.used_default
        ? "Otherwise"
        : matched.length
          ? `matched ${matched.join(", ")}`
          : "no rule matched"
    }.`;
  }
}
