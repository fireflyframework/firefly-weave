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
// The question before grouping steps whose data later steps read: after it
// they can read only the transaction's result. It lists each read and starts
// on Cancel, since grouping rewrites none of them. A native modal dialog: the
// browser contains focus, closes it on Escape and gives focus back.
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
import { sequencesOf, type ReferenceSite, type Workflow } from "../../model";
import { ndvRegistry } from "../ndv/registry";
import { readsText } from "./group";

let sequence = 0;

@Component({
  selector: "weave-group-dialog",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styles: `
    dialog.group-dialog {
      padding: var(--space-5);
      border: 1px solid var(--border);
      color: var(--text);
    }
    /* A scroll over the dialog or its backdrop never reaches the page: the
       backdrop is made a scroll container that contains it. */
    dialog.group-dialog:modal {
      overscroll-behavior: contain;
    }
    dialog.group-dialog::backdrop {
      overflow: hidden;
      overscroll-behavior: contain;
      background: var(--scrim);
    }
    /* The message above keeps its own 16 px. */
    .group-reads {
      margin: 0;
      padding: 0;
      list-style: none;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
    }
    .group-reads li {
      display: grid;
      grid-template-columns: 16px 96px max-content minmax(0, 1fr);
      align-items: center;
      gap: var(--space-1);
      min-height: 40px;
      padding: var(--space-1) var(--space-3);
    }
    .group-reads li + li {
      border-top: 1px solid var(--line);
    }
    .group-reads strong {
      overflow: hidden;
      font: var(--type-label);
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .group-reads-verb {
      font: var(--type-small);
      color: var(--muted);
    }
    .group-reads-chip {
      display: inline-flex;
      align-items: center;
      justify-self: start;
      gap: var(--space-1);
      min-width: 0;
      max-width: 100%;
      min-height: 24px;
      padding: 0 var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--sunken);
      font: var(--type-caption);
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .group-reads-chip > weave-icon {
      flex: none;
    }
    .group-hint {
      margin: var(--space-4) 0 0;
      font: var(--type-small);
      color: var(--muted);
    }
    dialog.group-dialog .dialog-actions {
      bottom: calc(-1 * var(--space-5));
      margin: var(--space-5) calc(-1 * var(--space-5)) calc(-1 * var(--space-5));
      padding-inline: var(--space-5);
    }
    @media (max-width: 767px) {
      dialog.group-dialog:modal {
        margin: auto 0 0;
        width: 100%;
        max-width: none;
        padding: var(--space-4);
        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
      }
      dialog.group-dialog .dialog-actions button {
        flex: 1;
        min-height: 44px;
      }
      dialog.group-dialog .dialog-actions {
        bottom: calc(-1 * var(--space-4));
        margin: var(--space-5) calc(-1 * var(--space-4))
          calc(-1 * var(--space-4));
        padding-inline: var(--space-4);
      }
    }
  `,
  template: `<dialog
    #dialog
    class="modal-panel group-dialog"
    [attr.aria-labelledby]="id + '-title'"
    [attr.aria-describedby]="id + '-text'"
    (close)="closed.emit(chose)"
  >
    <h2 [id]="id + '-title'">Group {{ count() }} steps in a transaction?</h2>
    <p class="dialog-message" [id]="id + '-text'">{{ text() }}</p>
    <ul class="group-reads" aria-label="Later reads">
      @for (row of rows(); track $index) {
        <li>
          <weave-icon [name]="row.icon" [size]="16" />
          <strong [attr.title]="row.name">{{ row.name }}</strong>
          <span class="group-reads-verb">reads</span>
          <span class="group-reads-chip"
            ><weave-icon [name]="row.readIcon" [size]="16" />{{
              row.read
            }}</span
          >
        </li>
      }
    </ul>
    <p class="group-hint">
      Group anyway keeps these reads; Diagnostics then lists each one so you can
      point it at the transaction's result.
    </p>
    <div class="dialog-actions">
      <button type="button" (click)="group()">Group anyway</button>
      <button type="button" class="primary" autofocus (click)="dialog.close()">
        Cancel
      </button>
    </div>
  </dialog>`,
})
export class GroupDialog {
  readonly id = `group-dialog-${++sequence}`;
  /** How many steps the run has. */
  readonly count = input.required<number>();
  readonly reads = input.required<readonly ReferenceSite[]>();
  /** The workflow before grouping: its steps name the rows. */
  readonly workflow = input.required<Workflow>();
  /** The dialog closed: true when Group anyway closed it. */
  readonly closed = output<boolean>();
  readonly dialog = viewChild.required<ElementRef<HTMLDialogElement>>("dialog");
  chose = false;
  readonly text = computed(() => readsText(this.reads()));
  /** One row a read, the same read of one step once: "notify reads hold › holdId". */
  readonly rows = computed(() => {
    const kinds = new Map(
      [...sequencesOf(this.workflow()).values()]
        .flat()
        .map((step) => [step.id, step.kind as string]),
    );
    const icon = (id: string) =>
      ndvRegistry.kind(kinds.get(id) ?? "")?.icon ?? "workflows";
    const rows = new Map<
      string,
      { icon: string; name: string; readIcon: string; read: string }
    >();
    for (const { holder, ref } of this.reads()) {
      const [, , step, part, ...fields] = ref
        .split("/")
        .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
      rows.set(`${holder} ${ref}`, {
        icon: holder ? icon(holder) : "output",
        name: holder || "Workflow result",
        readIcon: icon(step),
        read: [step, ...(part === "output" ? [] : [part]), ...fields].join(
          " › ",
        ),
      });
    }
    return [...rows.values()];
  });

  constructor() {
    afterNextRender(() => this.dialog().nativeElement.showModal());
  }
  group() {
    this.chose = true;
    this.dialog().nativeElement.close();
  }
}
