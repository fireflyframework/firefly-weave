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
// The canvas tools at the bottom right: zoom out, the zoom level (which
// resets to 100%), zoom in, Show minimap, Fit view and the keyboard
// shortcuts; below 768 px, where the minimap is hidden, one menu. Each hint
// names the key that does the same on the canvas.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";

@Component({
  selector: "weave-canvas-tools",
  standalone: true,
  imports: [Icon, RowMenu],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let percent = (zoom() * 100).toFixed(0) + "%";
    <div
      class="canvas-tools canvas-v2-tools"
      role="toolbar"
      aria-label="Canvas view"
    >
      <button
        type="button"
        class="icon-button"
        aria-label="Zoom out"
        title="Zoom out (-)"
        (click)="zoomOut.emit()"
      >
        <weave-icon name="zoomOut" [size]="16" />
      </button>
      <button
        type="button"
        class="zoom-level"
        [attr.aria-label]="'Reset zoom to 100%, now ' + percent"
        title="Reset zoom to 100% (0)"
        (click)="reset.emit()"
      >
        {{ percent }}
      </button>
      <button
        type="button"
        class="icon-button"
        aria-label="Zoom in"
        title="Zoom in (+)"
        (click)="zoomIn.emit()"
      >
        <weave-icon name="zoomIn" [size]="16" />
      </button>
      <span class="toolbar-divider"></span>
      <button
        type="button"
        class="icon-button minimap-toggle"
        aria-label="Show minimap"
        title="Show minimap"
        [attr.aria-pressed]="minimap()"
        (click)="toggleMinimap.emit()"
      >
        <weave-icon name="minimap" [size]="16" />
      </button>
      <button type="button" title="Fit view (1)" (click)="fit.emit()">
        <weave-icon name="fit" [size]="16" /><span class="tool-text"
          >Fit view</span
        >
      </button>
      <button
        type="button"
        class="icon-button"
        aria-label="Keyboard shortcuts"
        title="Keyboard shortcuts (?)"
        aria-haspopup="dialog"
        (click)="shortcuts.emit()"
      >
        <weave-icon name="help" [size]="16" />
      </button>
    </div>
    <weave-row-menu
      class="canvas-v2-tools-menu"
      label="Canvas view"
      text="View"
      [items]="menu"
    />
    <span class="sr-only" aria-live="polite">Zoom {{ percent }}</span>`,
})
export class CanvasTools {
  zoom = input.required<number>();
  zoomIn = output<void>();
  zoomOut = output<void>();
  reset = output<void>();
  fit = output<void>();
  /** "Show minimap" keeps the minimap open. */
  minimap = input(false);
  toggleMinimap = output<void>();
  /** The "?" sheet. */
  shortcuts = output<void>();
  readonly menu: RowMenuItem[] = [
    { label: "Zoom in", run: () => this.zoomIn.emit() },
    { label: "Zoom out", run: () => this.zoomOut.emit() },
    { label: "Reset zoom to 100%", run: () => this.reset.emit() },
    { label: "Fit view", run: () => this.fit.emit() },
    { label: "Keyboard shortcuts", run: () => this.shortcuts.emit() },
  ];
}
