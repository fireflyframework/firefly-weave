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
// A decision's Output after an execution: each path, in order, as Taken, Not
// taken or Not checked, then the taken path's result (or why later steps get
// an empty object).
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../../icon";
import type { Json } from "../registry";
import { OUTCOME_WORDS, type PathRow } from "../../state/run-summary";

@Component({
  selector: "weave-path-list",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<ul class="path-list" aria-label="Paths">
      @for (row of rows(); track $index) {
        <li class="path-row" [attr.data-outcome]="row.outcome">
          <span class="path-row-icon" aria-hidden="true">
            @if (row.outcome === "taken") {
              <weave-icon name="check" [size]="16" />
            }
          </span>
          <span class="path-row-label" [title]="text(row)">{{
            text(row)
          }}</span>
          @if (row.outcome === "taken") {
            <span class="pill" data-tone="success">{{ words.taken }}</span>
          } @else {
            <span class="path-row-outcome">{{ words[row.outcome] }}</span>
          }
        </li>
      }
    </ul>
    @let taken = takenLabel();
    @if (taken) {
      @if (result() === null || result() === undefined) {
        <p class="pane-empty-text path-result-empty">
          {{ taken }} has no result, so later steps receive an empty object. Add
          a path result to pass data on.
        </p>
      } @else {
        <h4 class="pane-caption path-result-caption">{{ taken }} result</h4>
        <pre
          class="pane-json path-result"
          tabindex="0"
          aria-label="Path result"
          >{{ json(result()) }}</pre
        >
      }
    }`,
})
export class PathList {
  readonly rows = input.required<PathRow[]>();
  /** The taken path's output, from the run. */
  readonly result = input<Json | null | undefined>(undefined);
  readonly words = OUTCOME_WORDS;
  text(row: PathRow): string {
    return row.condition ? `${row.label} · ${row.condition}` : row.label;
  }
  takenLabel(): string | null {
    return this.rows().find((row) => row.outcome === "taken")?.label ?? null;
  }
  json(value: Json | null | undefined): string {
    return JSON.stringify(value, null, 2);
  }
}
