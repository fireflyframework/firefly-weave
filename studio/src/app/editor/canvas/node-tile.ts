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
// One tile: its card (shape by role, icon, corner badge) and its label
// block. The canvas places it and handles its clicks; the tile only draws.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../icon";
import {
  labelScale,
  labelWidth,
  type Point,
  type TileShape,
} from "./layout-ltr";
import type { TileBadge, TileBorders } from "./tile-facts";

export interface TileView {
  id: string;
  /** A workflow step (not the trigger or End): selectable. */
  step: boolean;
  kind: string;
  shape: TileShape;
  /** The same object while the layout stays the same. */
  position: Point;
  width: number;
  height: number;
  icon: string;
  title: string;
  subtitle: string;
  /** From the card's top to its label block. */
  labelTop: number;
  name: string;
  tooltip: string;
  badge: TileBadge | null;
  borders: TileBorders;
  pulse: boolean;
  selected: boolean;
  tabIndex: 0 | -1;
}

@Component({
  selector: "weave-node-tile",
  standalone: true,
  imports: [Icon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let v = view();
    <button
      type="button"
      class="tile-body f-drag-blocker"
      data-action="tile"
      [attr.data-shape]="v.shape"
      [attr.aria-label]="v.name"
      [attr.title]="v.tooltip"
      [attr.aria-pressed]="v.step ? v.selected : null"
      [attr.tabindex]="v.tabIndex"
      [style.outline-width.px]="focusWidth()"
      [style.outline-offset.px]="focusWidth()"
    >
      <span
        class="tile-shape"
        [class.selected]="v.borders.selected"
        [class.live]="v.borders.live"
        [class.pulse]="v.pulse"
        [class.unapplied]="v.borders.unapplied"
        [class.error]="v.borders.error"
      ></span>
      @if (v.kind !== "end") {
        <weave-icon class="tile-icon" [name]="v.icon" />
      }
      @if (v.badge; as badge) {
        <span
          class="tile-badge"
          [attr.data-tone]="badge.tone"
          [attr.data-badge]="badge.kind"
        >
          @for (icon of badge.icons; track icon) {
            <weave-icon [name]="icon" [size]="16" />
          }
        </span>
      }
    </button>
    @if (v.kind !== "trigger") {
      <span class="handle handle-in" aria-hidden="true"></span>
    }
    @if (v.title) {
      <div
        class="tile-label"
        [style.top.px]="v.labelTop"
        [style.--label-scale]="labelScale()"
        [style.--label-width.px]="v.shape === 'end' ? null : labelWidth()"
        aria-hidden="true"
      >
        <strong [attr.title]="v.title">{{ v.title }}</strong>
        @if (v.subtitle) {
          <span [attr.title]="v.subtitle">{{ v.subtitle }}</span>
        }
      </div>
    }`,
})
export class NodeTile {
  view = input.required<TileView>();
  zoom = input(1);

  labelScale() {
    return labelScale(this.zoom());
  }
  labelWidth() {
    return labelWidth(this.zoom(), this.view().shape);
  }
  focusWidth() {
    return Math.ceil(2 / this.zoom());
  }
}
