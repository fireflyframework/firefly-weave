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
// Above the Runs tab's read-only canvas: Back to runs on a narrow screen,
// and, while no run is chosen, a note that says how to choose one.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from "@angular/core";
import { Icon } from "../../icon";
import type { RunsView } from "./runs-view";

@Component({
  selector: "weave-run-header",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styleUrl: "./run-header.css",
  host: { class: "run-column-head" },
  template: `@if (tab().narrow() && tab().front() === "canvas") {
      <div class="run-back">
        <button type="button" class="tertiary" (click)="tab().backToRuns()">
          <weave-icon name="chevronLeft" [size]="16" />Back to runs
        </button>
      </div>
    }
    @if (!chosen() && tab().mode() === "ready" && listed()) {
      <div class="run-note" role="note">
        <strong>Choose a run to see it on the canvas.</strong>
        <span>Use the list, or ↑ and ↓ then Enter.</span>
      </div>
    }`,
})
export class RunHeader {
  readonly tab = input.required<RunsView>();
  readonly chosen = computed(() => this.tab().state()?.chosen() ?? null);
  /** The list has runs to choose from. */
  readonly listed = computed(() => !!this.tab().state()?.rows().length);
}
