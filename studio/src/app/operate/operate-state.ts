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
// The states every Operate page and section shares: loading skeletons shaped
// like a table, an empty reason with its next action, an error with its code
// and Try again, a section that could not load, and the capability a person
// lacks. Pages show it in place of their content.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";

export type OperateStateKind =
  | "loading"
  | "empty"
  | "error"
  | "partial"
  | "access";

@Component({
  selector: "weave-operate-state",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: "./operate.css",
  template: `@switch (kind()) {
    @case ("loading") {
      <div class="skeleton" role="status">
        <span class="sr-only">Loading {{ noun() }}…</span>
        @for (row of skeletonRows(); track row) {
          <span class="skeleton-row" aria-hidden="true"></span>
        }
      </div>
    }
    @case ("empty") {
      <div class="empty-state">
        <h2>{{ heading() }}</h2>
        <p>{{ text() }}</p>
        <ng-content />
      </div>
    }
    @case ("access") {
      <div class="empty-state">
        <h2>{{ heading() || "You don't have access to this page" }}</h2>
        <p>
          You need <code>{{ capability() }}</code> in this environment. Ask an
          administrator in Settings › People and access.
        </p>
      </div>
    }
    @default {
      <div class="notice operate-problem" data-tone="danger" role="alert">
        <p>
          {{
            kind() === "partial"
              ? "This section could not be loaded."
              : message() || "Studio could not load this page."
          }}
          @if (kind() === "partial" && message()) {
            {{ message() }}
          }
        </p>
        @if (code()) {
          <small class="support-code">Support code: {{ code() }}</small>
        }
        <button type="button" (click)="retry.emit()">Try again</button>
      </div>
    }
  }`,
})
export class OperateState {
  kind = input.required<OperateStateKind>();
  /** Empty and access headings. */
  heading = input("");
  /** The empty state's reason and next step. */
  text = input("");
  /** What is loading, for screen readers: "workers". */
  noun = input("records");
  /** The error's plain message and support code. */
  message = input("");
  code = input("");
  /** The capability an access state names: "status.read". */
  capability = input("");
  /** How many skeleton rows stand in for the table. */
  rows = input(4);
  retry = output<void>();
  skeletonRows() {
    return Array.from({ length: this.rows() }, (_, index) => index);
  }
}
