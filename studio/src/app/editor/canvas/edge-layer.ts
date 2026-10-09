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
// The edges: one SVG path each, with a wider invisible path to hover and
// an arrowhead per run state. The "+" that inserts on an edge (shown on
// hover or focus, or whenever a step is being placed) is drawn by the
// canvas after the steps, so Tab reaches it after the step it follows.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import type { Insertion, LtrEdge } from "./layout-ltr";
import type { EdgeRun } from "./run-state";

/** A "+" on the canvas: where it is, what it inserts and whose Tab group it joins. */
export interface InsertView {
  key: string;
  tile: string;
  /** Top-left corner, in canvas units. */
  x: number;
  y: number;
  name: string;
  insert: Insertion;
  /** Offered as a place for the step being moved. */
  moving: boolean;
  tabIndex: 0 | -1;
}
export interface EdgeView {
  edge: LtrEdge;
  d: string;
  state: EdgeRun;
  /** The pointer is on the edge: its "+" shows. */
  shown: boolean;
  plus: InsertView | null;
}

@Component({
  selector: "weave-edge-layer",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<svg
    class="edges"
    [attr.width]="width()"
    [attr.height]="height()"
    aria-hidden="true"
  >
    <defs>
      @for (state of states; track state) {
        <marker
          [attr.id]="'canvas-arrow-' + state"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto"
        >
          <path [attr.class]="'edge-arrow ' + state" d="M0 0L10 5L0 10Z" />
        </marker>
      }
    </defs>
    @for (item of edges(); track item.edge.key) {
      <path
        class="edge-hit"
        [attr.d]="item.d"
        [attr.data-edge]="item.edge.key"
      />
      <path
        [attr.class]="
          'edge ' + item.state + (item.edge.shape === 'fan' ? ' fan' : '')
        "
        [attr.d]="item.d"
        [attr.data-edge-line]="item.edge.key"
        [attr.marker-end]="
          item.edge.shape === 'fan'
            ? null
            : 'url(#canvas-arrow-' + item.state + ')'
        "
      />
    }
  </svg>`,
})
export class EdgeLayer {
  edges = input.required<readonly EdgeView[]>();
  /** The drawing's size in canvas units. */
  width = input.required<number>();
  height = input.required<number>();
  readonly states = ["idle", "live", "taken", "skipped"] as const;
}
