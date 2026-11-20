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
  | Extract<ExecuteGate, { kind: "unpin" }>
  | {
      /** Execute in an environment, for a step that changes data there: asked every time. */
      kind: "real";
      title: string;
      stepId: string;
      environment: string;
      /** A sentence of the step kind's own, such as a cost notice. */
      extra?: string;
    }
  | {
      /** Several releases can run a dependency of the step here: which one. */
      kind: "release";
      title: string;
      stepId: string;
      environment: string;
      choices: Record<string, { id: string; label: string }[]>;
    };
let sequence = 0;

@Component({
  selector: "weave-execute-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  host: { class: "modal-backdrop", "(mousedown)": "hold($event)" },
  styles: `
    /* One row per issue: the step and its message, Fix on the row's center. */
    .execute-issues {
      display: grid;
      gap: var(--space-2);
      margin: var(--space-3) 0 0;
      padding: 0;
      list-style: none;
    }
    .execute-issue {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: var(--space-3);
      padding: var(--space-2) var(--space-3);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--sunken);
    }
    .execute-issue-text {
      display: grid;
      gap: 2px;
      min-width: 0;
    }
    .execute-issue-step {
      font: var(--type-label);
      overflow-wrap: anywhere;
    }
    .execute-releases {
      display: grid;
      gap: var(--space-3);
      margin: var(--space-3) 0 0;
    }
    .execute-release {
      display: grid;
      gap: var(--space-1);
    }
    .execute-release select {
      min-height: var(--control-md);
    }
    @media (max-width: 767px), (pointer: coarse) {
      .execute-release select {
        min-height: var(--control-touch);
      }
    }
    .execute-issue-message {
      font: var(--type-small);
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    /* A name in parentheses ("(openai-prod)") never breaks at its hyphen. */
    .execute-keep {
      white-space: nowrap;
    }
  `,
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
                <strong class="execute-issue-step">{{ issue.stepId }}</strong>
                <span class="execute-issue-message">{{ issue.message }}</span>
              </span>
              <button
                type="button"
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
      } @else if (asked.kind === "real") {
        <!-- A step's own sentence (an AI task's cost) says what running it does. -->
        <p class="dialog-message" [id]="messageId">
          @if (asked.extra) {
            @for (part of phrases(asked.extra); track $index) {
              <span
                [class.execute-keep]="part.keep"
                [textContent]="part.text"
              ></span>
            }
          } @else {
            {{ asked.stepId }} changes data in {{ asked.environment }}.
            Repeating it repeats the change.
          }
        </p>
        <div class="dialog-actions">
          <!-- Focus starts on Cancel: a held or repeated Enter mustn't run a change. -->
          <button type="button" data-execute-primary (click)="cancel.emit()">
            Cancel
          </button>
          <button type="button" class="primary" (click)="confirm.emit()">
            Run it
          </button>
        </div>
      } @else if (asked.kind === "release") {
        <p class="dialog-message" [id]="messageId">
          Choose which release runs {{ asked.stepId }} in
          {{ asked.environment }}.
        </p>
        <div class="execute-releases">
          @for (dependency of dependencies(asked); track dependency) {
            <label class="execute-release">
              <span class="execute-issue-step">{{ dependency }}</span>
              <select
                [attr.data-execute-primary]="$first ? '' : null"
                (change)="pick(dependency, $event)"
              >
                @for (choice of asked.choices[dependency]; track choice.id) {
                  <option [value]="choice.id">{{ choice.label }}</option>
                }
              </select>
            </label>
          }
        </div>
        <div class="dialog-actions">
          <button type="button" (click)="cancel.emit()">Cancel</button>
          <button type="button" class="primary" (click)="chooseReleases(asked)">
            Run it
          </button>
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
  /** A step's own sentence in pieces: each name in parentheses kept on one line. */
  phrases(text: string): { text: string; keep: boolean }[] {
    return text
      .split(/(\([^()]*\))/)
      .filter((part) => part !== "")
      .map((part) => ({ text: part, keep: /^\(.*\)$/.test(part) }));
  }
  /** The release question's answer: a release ID per dependency. */
  readonly released = output<Record<string, string>>();
  private readonly picked: Record<string, string> = {};
  dependencies(asked: { choices: Record<string, unknown[]> }): string[] {
    return Object.keys(asked.choices).sort();
  }
  pick(dependency: string, event: Event) {
    this.picked[dependency] = (event.target as HTMLSelectElement).value;
  }
  chooseReleases(asked: {
    choices: Record<string, { id: string; label: string }[]>;
  }) {
    const chosen: Record<string, string> = {};
    for (const dependency of this.dependencies(asked)) {
      const id = this.picked[dependency] ?? asked.choices[dependency][0]?.id;
      if (id) chosen[dependency] = id;
    }
    this.released.emit(chosen);
  }
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
