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
// An AI agent's slots under its card: a diamond on the card's bottom edge,
// a dashed connector and a chip per slot. Chips show data of the step;
// they are not steps.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../icon";
import type { LtrChip } from "./layout-ltr";

export interface ChipView extends LtrChip {
  icon: string;
  state: "ok" | "missing" | "error";
  /** A required slot with nothing in it. */
  warn: boolean;
  /** "Model, required, not set". */
  name: string;
}
export interface SubNodeView {
  step: string;
  chips: readonly ChipView[];
}

const icons: Record<string, string> = {
  model: "model",
  memory: "memory",
  tools: "tools",
  output: "output",
};
export const chipIcon = (slot: string): string => icons[slot] ?? "agent";

@Component({
  selector: "weave-sub-node-row",
  standalone: true,
  imports: [Icon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<svg class="sub-node-links" aria-hidden="true">
      @for (chip of row().chips; track chip.id) {
        <line
          class="sub-node-link"
          [attr.x1]="chip.handle.x"
          [attr.y1]="chip.handle.y + 6"
          [attr.x2]="chip.x + 38"
          [attr.y2]="chip.y"
        />
      }
    </svg>
    @for (chip of row().chips; track chip.id) {
      <span
        class="sub-node-handle"
        [style.left.px]="chip.handle.x - 6"
        [style.top.px]="chip.handle.y - 6"
        aria-hidden="true"
      ></span>
      <span
        class="sub-node-chip"
        [class.needs-setup]="chip.warn"
        [attr.data-state]="chip.state"
        [style.left.px]="chip.x"
        [style.top.px]="chip.y"
        role="img"
        [attr.aria-label]="chip.name"
        [attr.title]="chip.name"
        ><weave-icon [name]="chip.icon" [size]="16" /><span
          aria-hidden="true"
          >{{ chip.label }}</span
        ></span
      >
    }`,
})
export class SubNodeRow {
  row = input.required<SubNodeView>();
}
