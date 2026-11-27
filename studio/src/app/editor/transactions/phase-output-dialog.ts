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
// The question when Execute stops at an undo step whose result needs test
// output: what it needs, where (hold › Confirm with · what runs it), and Set
// test output, which opens its editor and changes nothing by itself. Asked
// once for each answer, by what executed: step details or the run log. A
// native modal dialog: the browser contains focus, closes it on Escape and
// gives focus back.
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
import { Icon } from "../../icon";
import type { PhaseBlock } from "../state/execute-gate";

/** The answers the question was asked for: never twice, whoever shows it. */
const asked = new WeakSet<object>();
/** Whether the question still has to be asked for an answer; from now on it was. */
export function askOnce(answer: object): boolean {
  if (asked.has(answer)) return false;
  asked.add(answer);
  return true;
}

let sequence = 0;

@Component({
  selector: "weave-phase-output-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styles: `
    dialog.phase-output {
      border: 0;
      color: var(--text);
    }
    /* A scroll over the dialog or its backdrop never reaches the page: the
       backdrop is made a scroll container that contains it. */
    dialog.phase-output:modal {
      overscroll-behavior: contain;
    }
    dialog.phase-output::backdrop {
      overflow: hidden;
      overscroll-behavior: contain;
      background: var(--scrim);
    }
    .phase-output-chips {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-1);
      margin: var(--space-3) 0 0;
      padding: 0;
      list-style: none;
    }
    .phase-output-chips li {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      min-width: 0;
    }
    .phase-output-chip {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      min-width: 0;
      min-height: 26px;
      padding: 0 var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      font: var(--type-label);
      overflow-wrap: anywhere;
    }
    .phase-output-chip > weave-icon {
      flex: none;
    }
    .phase-output-sep {
      flex: none;
      color: var(--muted);
    }
    .phase-output-hint {
      margin: var(--space-4) 0 0;
      font: var(--type-small);
      color: var(--muted);
    }
    @media (max-width: 767px) {
      dialog.phase-output:modal {
        margin: auto 0 0;
        width: 100%;
        max-width: none;
        padding: var(--space-4);
        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
      }
      dialog.phase-output .dialog-actions button {
        flex: 1;
        min-height: 44px;
      }
      /* The footer spans the sheet to its 16 px padding, not the panel's 28. */
      dialog.phase-output .dialog-actions {
        bottom: calc(-1 * var(--space-4));
        margin: var(--space-6) calc(-1 * var(--space-4))
          calc(-1 * var(--space-4));
        padding-inline: var(--space-4);
      }
    }
  `,
  template: `<dialog
    #dialog
    class="modal-panel phase-output"
    [attr.aria-labelledby]="id + '-title'"
    [attr.aria-describedby]="id + '-text'"
    (close)="closed.emit(chose)"
  >
    <h2 [id]="id + '-title'">Set test output to execute</h2>
    <p class="dialog-message" [id]="id + '-text'">{{ block().message }}</p>
    <ol class="phase-output-chips" aria-label="What needs it">
      <li>
        <span class="phase-output-chip"
          ><weave-icon [name]="icon()" [size]="16" />{{ block().stepId }}</span
        >
      </li>
      <li>
        <weave-icon class="phase-output-sep" name="chevron" [size]="16" /><span
          class="phase-output-chip"
          ><weave-icon name="undo" [size]="16" />{{ ran() }}</span
        >
      </li>
    </ol>
    <p class="phase-output-hint">
      The test output is saved with this workflow's test data, for Execute only.
      Real runs ignore it.
    </p>
    <div class="dialog-actions">
      <button type="button" (click)="dialog.close()">Cancel</button>
      <!-- It changes nothing by itself: it opens the undo step's test output. -->
      <button type="button" class="primary" autofocus (click)="set()">
        Set test output
      </button>
    </div>
  </dialog>`,
})
export class PhaseOutputDialog {
  readonly id = `phase-output-${++sequence}`;
  readonly block = input.required<PhaseBlock>();
  /** The step's icon. */
  readonly icon = input("action");
  /** The dialog closed: true when Set test output closed it. */
  readonly closed = output<boolean>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  chose = false;
  /** "Confirm with · source-system.confirm-hold 1.0.0". */
  readonly ran = computed(() => {
    const { label, uses } = this.block();
    return `${label} · ${uses.replace(/@([^@]*)$/, " $1")}`;
  });

  constructor() {
    afterNextRender(() => this.dialog().nativeElement.showModal());
  }
  set() {
    this.chose = true;
    this.dialog().nativeElement.close();
  }
}
