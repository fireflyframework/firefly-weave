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
// Why Publish… stopped: a transaction that isn't safe, its errors one by one
// and the fix of each. A native modal dialog that starts on Close: the
// browser contains focus, closes it on Escape and gives focus back. Choosing
// a fix closes it, since the next check lists what is left.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  computed,
  input,
  output,
  viewChild,
} from "@angular/core";
import type { ChosenFix } from "../../designer/diagnostics-list";
import { Icon } from "../../icon";
import {
  problemsText,
  type PublishSummary,
  type TransactionProblem,
} from "./publish-summary";

let sequence = 0;

@Component({
  selector: "weave-publish-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styles: `
    dialog.publish-dialog {
      width: 600px;
      padding: var(--space-5);
      border: 1px solid var(--border);
      color: var(--text);
    }
    dialog.publish-dialog:modal {
      overscroll-behavior: contain;
    }
    dialog.publish-dialog::backdrop {
      overflow: hidden;
      overscroll-behavior: contain;
      background: var(--scrim);
    }
    .publish-callout {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr);
      gap: var(--space-1) var(--space-2);
      padding: var(--space-3);
      border: 1px solid var(--danger-bd);
      border-radius: var(--radius-md);
      background: var(--danger-bg);
    }
    .publish-callout > weave-icon {
      margin-top: 2px;
      color: var(--danger-ink);
    }
    .publish-callout > p {
      grid-column: 2;
      margin: 0;
      font: var(--type-small);
      color: var(--muted);
    }
    .publish-callout > .publish-count {
      font: var(--type-label);
      color: var(--text);
    }
    .publish-problems {
      margin: var(--space-4) 0 0;
      padding: 0;
      list-style: none;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
    }
    .publish-problems li {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: start;
      gap: var(--space-3);
      padding: var(--space-3);
    }
    .publish-problems li + li {
      border-top: 1px solid var(--line);
    }
    .publish-problems strong {
      font: var(--type-label);
    }
    .publish-problems p {
      margin: 2px 0 0;
      font: var(--type-small);
      overflow-wrap: anywhere;
    }
    .publish-problems button {
      min-height: 32px;
      padding: 0 var(--space-3);
      white-space: nowrap;
    }
    dialog.publish-dialog .dialog-actions {
      bottom: calc(-1 * var(--space-5));
      margin: var(--space-5) calc(-1 * var(--space-5)) calc(-1 * var(--space-5));
      padding-inline: var(--space-5);
    }
    @media (max-width: 767px) {
      dialog.publish-dialog:modal {
        margin: auto 0 0;
        width: 100%;
        max-width: none;
        padding: var(--space-4);
        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
      }
      .publish-problems li {
        grid-template-columns: minmax(0, 1fr);
      }
      .publish-problems button {
        justify-self: start;
        min-height: 44px;
      }
      dialog.publish-dialog .dialog-actions button {
        flex: 1;
        min-height: 44px;
      }
      dialog.publish-dialog .dialog-actions {
        bottom: calc(-1 * var(--space-4));
        margin: var(--space-5) calc(-1 * var(--space-4))
          calc(-1 * var(--space-4));
        padding-inline: var(--space-4);
      }
    }
  `,
  template: `<dialog
    #dialog
    class="modal-panel publish-dialog"
    [attr.aria-labelledby]="id + '-title'"
    [attr.aria-describedby]="id + '-text'"
    (close)="closed.emit(chosen)"
  >
    <h2 [id]="id + '-title'">
      Publish {{ summary().name }} {{ summary().version }}
    </h2>
    <div class="publish-callout" [id]="id + '-text'">
      <weave-icon name="failCircle" [size]="16" />
      <p class="publish-count">{{ text() }}</p>
      <p>Fix them, then publish. Each fix is one undo step.</p>
    </div>
    <ul class="publish-problems" aria-label="Problems">
      @for (problem of summary().problems; track $index) {
        <li>
          <div>
            <strong>{{ problem.step }}</strong>
            <p>{{ problem.text }}</p>
          </div>
          @if (problem.fix; as fix) {
            <button
              type="button"
              [attr.aria-label]="fix.name"
              (click)="choose(problem)"
            >
              {{ fix.label }}
            </button>
          }
        </li>
      }
    </ul>
    <div class="dialog-actions">
      <button type="button" class="primary" autofocus (click)="dialog.close()">
        Close
      </button>
      <button type="button" disabled>Publish</button>
    </div>
  </dialog>`,
})
export class PublishDialog {
  readonly id = `publish-dialog-${++sequence}`;
  readonly summary = input.required<PublishSummary>();
  /** The dialog closed: the fix that closed it, or null. */
  readonly closed = output<ChosenFix | null>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  chosen: ChosenFix | null = null;
  readonly text = computed(() => problemsText(this.summary().problems));

  constructor() {
    afterNextRender(() => this.dialog().nativeElement.showModal());
  }
  choose({ fix, location }: TransactionProblem) {
    this.chosen = fix && { fix, location };
    this.dialog().nativeElement.close();
  }
}
