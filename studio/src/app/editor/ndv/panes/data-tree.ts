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
// Rows of fields: an icon for the type, the name, the type in words, and for
// input fields a drag handle — every row with a reference is a drag source
// (application/x-weave-ref) and focusable for the mapping integration.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../../icon";
import { dragSchema, typeIcon, type TreeRow } from "./views";

export const REF_MIME = "application/x-weave-ref";

@Component({
  selector: "weave-data-tree",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<ul class="data-tree" role="list" [attr.aria-label]="label()">
    @for (row of rows(); track row.key) {
      <li
        class="data-row"
        [style.--depth]="row.depth"
        [attr.draggable]="row.ref ? 'true' : null"
        [attr.tabindex]="row.ref ? 0 : null"
        [attr.data-ref]="row.ref ?? null"
        [attr.aria-label]="
          row.label +
          ', ' +
          row.typeLabel +
          (mapped().includes(row.ref ?? '') ? ', mapped in this step' : '')
        "
        (dragstart)="drag($event, row)"
      >
        @if (row.ref) {
          <weave-icon
            class="data-grip"
            name="drag"
            [size]="16"
            aria-hidden="true"
          />
        }
        <span class="data-type" aria-hidden="true"
          ><weave-icon [name]="icon(row)" [size]="16"
        /></span>
        <span class="data-key">{{ row.label }}</span>
        <span class="data-kind">{{ row.typeLabel }}</span>
        @if (mapped().includes(row.ref ?? "")) {
          <weave-icon
            class="data-mapped"
            name="check"
            [size]="16"
            aria-hidden="true"
          />
        }
      </li>
    }
  </ul>`,
})
export class DataTree {
  rows = input.required<TreeRow[]>();
  label = input.required<string>();
  /** References this step's fields already map (the check mark). */
  mapped = input<string[]>([]);
  icon(row: TreeRow): string {
    return typeIcon(row.types, row.typeLabel);
  }
  drag(event: DragEvent, row: TreeRow) {
    if (!row.ref || !event.dataTransfer) return;
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData(
      REF_MIME,
      JSON.stringify({
        ref: row.ref,
        breadcrumb: row.breadcrumb,
        schema: dragSchema(row.schema ?? {}),
      }),
    );
    event.dataTransfer.setData("text/plain", row.ref);
  }
}
