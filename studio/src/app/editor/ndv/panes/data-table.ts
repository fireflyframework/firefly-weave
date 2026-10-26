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
// The Table view of a pane's data: a plain table with a sticky header, one
// line per cell (the whole value in its title), the first 500 rows and at
// most 50 columns, and a note saying so. The pane's search filters the rows.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from "@angular/core";
import type { Json } from "../registry";
import { filterTable, tableOf } from "./table-view";

@Component({
  selector: "weave-data-table",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `@let data = table();
    @let rows = shown();
    <div class="data-table-scroll" tabindex="0">
      <table class="data-table" [attr.aria-label]="label()">
        <thead>
          <tr>
            @for (column of data.columns; track $index) {
              <th scope="col" [title]="column">{{ column }}</th>
            }
          </tr>
        </thead>
        <tbody>
          @for (row of rows; track $index) {
            <tr>
              @for (cell of row; track $index) {
                <td [title]="cell">{{ cell }}</td>
              }
            </tr>
          }
        </tbody>
      </table>
    </div>
    @if (data.note) {
      <p class="data-table-note">{{ data.note }}</p>
    }`,
})
export class DataTable {
  readonly value = input<Json | undefined>(undefined);
  /** "Output data" or "Input data". */
  readonly label = input("Data");
  readonly query = input("");
  readonly table = computed(() => tableOf(this.value()));
  readonly shown = computed(() => filterTable(this.table(), this.query()));
}
