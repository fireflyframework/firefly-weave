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
// What Execute asks before it runs anything: "Fix 1 issue first", listing
// the errors in the steps the execution runs, each with Fix; or, for a
// pinned step, "Unpin and execute?". The primary answer starts focused, and
// Escape closes the question before step details.
import {
  type AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  input,
  output,
} from "@angular/core";
import type { ExecuteGate } from "../state/execute-gate";
import type { Path } from "./registry";

/** An error that keeps an execution from running, and where to fix it. */
export interface ExecuteIssue {
  stepId: string;
  severity: "error" | "warning";
  message: string;
  /** The field that holds it, when step details can focus one. */
  at?: Path;
  /** A step issue of the open step (its key), fixed in place. */
  key?: string;
}
export type ExecuteQuestion =
  | { kind: "fix"; title: string; issues: ExecuteIssue[] }
  | Extract<ExecuteGate, { kind: "unpin" }>;
let sequence = 0;

@Component({
  selector: "weave-execute-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  host: { class: "modal-backdrop", "(mousedown)": "hold($event)" },
  template: `@let asked = question();
    <section
      class="modal-panel execute-dialog"
      role="alertdialog"
      aria-modal="true"
      tabindex="-1"
      [attr.aria-labelledby]="titleId"
      [attr.aria-describedby]="messageId"
      (keydown)="key($event)"
    >
      <header class="modal-header">
        <h2 [id]="titleId">{{ asked.title }}</h2>
      </header>
      @if (asked.kind === "fix") {
        <p class="dialog-message" [id]="messageId">
          Executing runs
          {{
            asked.issues.length === 1 ? "a step that has" : "steps that have"
          }}
          errors. Fix {{ asked.issues.length === 1 ? "it" : "them" }}, then
          execute again.
        </p>
        <ul class="execute-issues" aria-label="Issues to fix">
          @for (issue of asked.issues; track $index) {
            <li class="execute-issue">
              <span class="execute-issue-text">
                <strong>{{ issue.stepId }}</strong>
                <span>{{ issue.message }}</span>
              </span>
              <button
                type="button"
                class="sm"
                [attr.data-execute-primary]="$first ? '' : null"
                [attr.aria-label]="'Fix ' + issue.stepId + ': ' + issue.message"
                (click)="fix.emit(issue)"
              >
                Fix
              </button>
            </li>
          }
        </ul>
        <div class="dialog-actions">
          <button type="button" (click)="cancel.emit()">Close</button>
        </div>
      } @else {
        <p class="dialog-message" [id]="messageId">
          {{ asked.stepId }} uses its pinned output. Executing it unpins the
          output first; Undo brings the pin back.
        </p>
        <div class="dialog-actions">
          <button type="button" (click)="cancel.emit()">Cancel</button>
          <button
            type="button"
            class="primary"
            data-execute-primary
            (click)="confirm.emit()"
          >
            Unpin and execute
          </button>
        </div>
      }
    </section>`,
})
export class ExecuteDialog implements AfterViewInit {
  readonly question = input.required<ExecuteQuestion>();
  readonly fix = output<ExecuteIssue>();
  readonly confirm = output<void>();
  readonly cancel = output<void>();
  readonly titleId = `execute-dialog-title-${++sequence}`;
  readonly messageId = `execute-dialog-message-${sequence}`;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  ngAfterViewInit() {
    // After step details settle their own focus for the key that asked.
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>("[data-execute-primary]")
        ?.focus(),
    );
  }
  /** A click on the backdrop keeps focus in the question. */
  hold(event: MouseEvent) {
    if (event.target === event.currentTarget) event.preventDefault();
  }
  key(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.cancel.emit();
      return;
    }
    if (event.key !== "Tab") return;
    const buttons = [
      ...this.element.nativeElement.querySelectorAll<HTMLElement>("button"),
    ];
    const at = buttons.indexOf(document.activeElement as HTMLElement);
    const next = event.shiftKey
      ? at <= 0
        ? buttons.length - 1
        : at - 1
      : at === buttons.length - 1
        ? 0
        : at + 1;
    event.preventDefault();
    buttons[next]?.focus();
  }
}
