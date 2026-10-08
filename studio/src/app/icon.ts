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
import { Component, input } from "@angular/core";
import { iconNodes, type IconName, type IconNode } from "./icon-data";

/** Icons come in two sizes; both draw a 1.5px stroke on screen (brand rule). */
export const iconStrokes = { 16: 1.5, 20: 1.5 } as const;
export type IconSize = keyof typeof iconStrokes;
/** Every name weave-icon draws: the generator map in scripts/build-icons.mjs. */
export const iconNames = Object.keys(iconNodes) as IconName[];

export function isIconName(name: string): name is IconName {
  return Object.hasOwn(iconNodes, name);
}

/** A name's drawing; an unknown name draws `workflows` (icon.test.ts rejects it). */
export function iconDrawing(name: string): readonly IconNode[] {
  return isIconName(name) ? iconNodes[name] : iconNodes.workflows;
}

/** A size attribute ("16" in a template) or input (16) as an icon size. */
export function iconSize(
  value: IconSize | `${IconSize}` | undefined,
): IconSize | undefined {
  const size = Number(value);
  return size === 16 || size === 20 ? size : undefined;
}

@Component({
  selector: "weave-icon",
  standalone: true,
  host: {
    "[attr.data-icon]": "name()",
    "[attr.data-icon-missing]": "known() ? null : ''",
    "[style.width.px]": "size() ?? null",
    "[style.height.px]": "size() ?? null",
    "[style.--icon-stroke]": "stroke()",
  },
  template: `<svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    @for (node of nodes(); track $index) {
      @switch (node.tag) {
        @case ("path") {
          <svg:path [attr.d]="node.attrs.d" />
        }
        @case ("circle") {
          <svg:circle
            [attr.cx]="node.attrs.cx"
            [attr.cy]="node.attrs.cy"
            [attr.r]="node.attrs.r"
          />
        }
        @case ("rect") {
          <svg:rect
            [attr.x]="node.attrs.x"
            [attr.y]="node.attrs.y"
            [attr.width]="node.attrs.width"
            [attr.height]="node.attrs.height"
            [attr.rx]="node.attrs.rx"
            [attr.ry]="node.attrs.ry"
          />
        }
        @case ("line") {
          <svg:line
            [attr.x1]="node.attrs.x1"
            [attr.y1]="node.attrs.y1"
            [attr.x2]="node.attrs.x2"
            [attr.y2]="node.attrs.y2"
          />
        }
        @case ("polyline") {
          <svg:polyline [attr.points]="node.attrs.points" />
        }
        @case ("polygon") {
          <svg:polygon [attr.points]="node.attrs.points" />
        }
        @case ("ellipse") {
          <svg:ellipse
            [attr.cx]="node.attrs.cx"
            [attr.cy]="node.attrs.cy"
            [attr.rx]="node.attrs.rx"
            [attr.ry]="node.attrs.ry"
          />
        }
      }
    }
  </svg>`,
  styles: [
    `
      :host {
        display: inline-flex;
        width: 20px;
        height: 20px;
        flex: none;
      }
      svg {
        width: 100%;
        height: 100%;
        stroke-width: var(--icon-stroke, 1.5);
      }
      /* The stroke keeps its width in screen pixels at every icon size. */
      path,
      circle,
      rect,
      line,
      polyline,
      polygon,
      ellipse {
        vector-effect: non-scaling-stroke;
      }
    `,
  ],
})
export class Icon {
  name = input("workflows");
  /** 16 or 20 (the default); CSS may also size the icon. */
  size = input<IconSize | undefined, IconSize | `${IconSize}` | undefined>(
    undefined,
    { transform: iconSize },
  );
  nodes() {
    return iconDrawing(this.name());
  }
  known() {
    return isIconName(this.name());
  }
  stroke() {
    const size = this.size();
    return size ? String(iconStrokes[size]) : null;
  }
}
