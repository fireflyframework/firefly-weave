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
// Execute step on a step inside an error path its step won't take: with
// this test data that step succeeds, so the target isn't reached. The
// question says so, shows where the target is (submit › Error › notify-team)
// and offers Simulate a failure, which changes nothing by itself. A native
// modal dialog: the browser contains focus, closes it on Escape and gives
// focus back to Execute step.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  input,
  output,
  viewChild,
} from "@angular/core";
import { Icon } from "../../icon";

let sequence = 0;

@Component({
  selector: "weave-execute-path-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  styles: `
    /* The shared panel's width: at 480 px its answers would stack (they stack
       below 424 px of content), where they share a row at 520. */
    dialog.execute-path {
      border: 0;
      color: var(--text);
    }
    dialog.execute-path::backdrop {
      background: var(--scrim);
    }
    .execute-path-chips {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1);
      margin: var(--space-3) 0 0;
      padding: 0;
      list-style: none;
    }
    .execute-path-chips li {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
    }
    .execute-path-chip {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      min-height: 26px;
      padding: 0 var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      font: var(--type-label);
      overflow-wrap: anywhere;
    }
    .execute-path-chip[data-tone="danger"] {
      border-color: var(--danger-bd);
      color: var(--danger-ink);
    }
    .execute-path-chips .execute-path-sep {
      color: var(--muted);
    }
    @media (max-width: 767px) {
      dialog.execute-path:modal {
        margin: auto 0 0;
        width: 100%;
        max-width: none;
        padding: var(--space-4);
        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
      }
      dialog.execute-path .dialog-actions button {
        flex: 1;
        min-height: 44px;
      }
    }
  `,
  template: `<dialog
    #dialog
    class="modal-panel execute-path"
    [attr.aria-labelledby]="id + '-title'"
    [attr.aria-describedby]="id + '-text'"
    (close)="closed.emit(chose)"
  >
    <h2 [id]="id + '-title'">Simulate a failure of {{ owner() }} first</h2>
    <p class="dialog-message" [id]="id + '-text'">
      {{ stepId() }} runs only when {{ owner() }} fails. With this test data
      {{ owner() }} succeeds, so {{ stepId() }} isn't reached.
    </p>
    <ol
      class="execute-path-chips"
      [attr.aria-label]="'Where ' + stepId() + ' is'"
    >
      <li>
        <span class="execute-path-chip"
          ><weave-icon [name]="ownerIcon()" [size]="16" />{{ owner() }}</span
        >
      </li>
      <li>
        <weave-icon class="execute-path-sep" name="chevron" [size]="16" /><span
          class="execute-path-chip"
          data-tone="danger"
          ><weave-icon name="warning" [size]="16" />Error</span
        >
      </li>
      <li>
        <weave-icon class="execute-path-sep" name="chevron" [size]="16" /><span
          class="execute-path-chip"
          ><weave-icon [name]="stepIcon()" [size]="16" />{{ stepId() }}</span
        >
      </li>
    </ol>
    <div class="dialog-actions">
      <button type="button" (click)="dialog.close()">Cancel</button>
      <!-- It changes nothing by itself: it opens Simulate a failure. -->
      <button type="button" class="primary" autofocus (click)="simulate()">
        Simulate a failure
      </button>
    </div>
  </dialog>`,
})
export class ExecutePathDialog {
  readonly id = `execute-path-${++sequence}`;
  /** The step whose error path holds the target. */
  readonly owner = input.required<string>();
  readonly ownerIcon = input("workflows");
  /** The step Execute step was asked for. */
  readonly stepId = input.required<string>();
  readonly stepIcon = input("workflows");
  /** The dialog closed: true when Simulate a failure closed it. */
  readonly closed = output<boolean>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  chose = false;

  constructor() {
    afterNextRender(() => this.dialog().nativeElement.showModal());
  }
  simulate() {
    this.chose = true;
    this.dialog().nativeElement.close();
  }
}
