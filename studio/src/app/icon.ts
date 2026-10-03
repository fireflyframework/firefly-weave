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

/** Icons come in two sizes; each has its own stroke width in screen pixels. */
export const iconStrokes = { 16: 1.5, 20: 1.75 } as const;
export type IconSize = keyof typeof iconStrokes;

/**
 * One drawing per meaning: no two names share a path. Names in `ringed` also
 * draw a circle of radius 9 around the path.
 */
export const iconPaths: Readonly<Record<string, string>> = {
  workflows: "M4 3h6v6H4zM14 15h6v6h-6zM7 9v9h7M10 6h7v9",
  runs: "M9 7l8 5-8 5z",
  home: "M3 10l9-8 9 8M5 9v12h5v-7h4v7h5V9",
  tasks: "M8 4V2h8v2M5 4h14v18H5zM8 11l2 2 5-5M8 17h7",
  email: "M3 5h18v14H3zM3 6l9 7 9-7",
  connections: "M8 2v5M16 2v5M5 7h14v3a7 7 0 0 1-14 0zM12 17v5",
  workers:
    "M5 5h14v14H5zM9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4M9 9h6v6H9z",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  action: "M8 5h12v14H8zM3 9l5 3-5 3",
  transform: "M4 5h6v6H4zM14 13h6v6h-6zM10 8h5v5",
  switch: "M12 2l9 10-9 10L3 12z",
  parallel: "M7 3v18M17 3v18M3 7h8M13 17h8",
  wait: "M12 6v6l4 2",
  signal:
    "M12 12v.01M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8",
  humanTask: "M12 2a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM5 21v-5a7 7 0 0 1 14 0v5",
  fail: "M6 6l12 12M18 6L6 18",
  failCircle: "M15 9l-6 6M9 9l6 6",
  play: "M8 4l12 8-12 8z",
  plus: "M12 5v14M5 12h14",
  search: "M16 16l5 5",
  chevron: "M9 5l7 7-7 7",
  undo: "M8 4L3 9l5 5M3 9h10a7 7 0 0 1 0 14",
  redo: "M16 4l5 5-5 5M21 9H11a7 7 0 0 0 0 14",
  fit: "M3 9V3h6M15 3h6v6M21 15v6h-6M9 21H3v-6",
  source: "M8 5l-6 7 6 7M16 5l6 7-6 7M14 3l-4 18",
  check: "M4 12l5 5L20 6",
  download: "M12 3v12M7 10l5 5 5-5M4 17v4h16v-4",
  upload: "M12 21V9M7 14l5-5 5 5M4 7V3h16v4",
  trash: "M4 6h16M9 6V3h6v3M7 6l1 15h8l1-15",
  menu: "M3 6h18M3 12h18M3 18h18",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  close: "M5 5l14 14M19 5L5 19",
  save: "M4 3h14l3 3v15H3V3zM7 3v6h10V3M7 21v-7h10v7",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1",
  workspace: "M12 3l9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5",
  lock: "M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4M12 15v2",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  warning: "M12 3l10 18H2zM12 10v5M12 18v.01",
  help: "M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01",
  logout: "M10 4H4v16h6M15 8l4 4-4 4M19 12H9",
  cloud: "M7 19h10a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.2 9.3 4.9 4.9 0 0 0 7 19z",
  laptop: "M5 5h14v10H5zM2 19h20",
  panel: "M3 4h18v16H3zM15 4v16",
  /** Tidy layout: boxes back in a column. */
  tidy: "M8 3h8v5H8zM8 16h8v5H8zM12 8v8",
};
/** Icons drawn inside a ring. */
export const ringedIcons: ReadonlySet<string> = new Set([
  "runs",
  "wait",
  "help",
  "failCircle",
]);

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
    <path [attr.d]="path()" />
    @if (ringed()) {
      <circle cx="12" cy="12" r="9" />
    }
    @if (name() === "search") {
      <circle cx="10" cy="10" r="7" />
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
        stroke-width: var(--icon-stroke, 1.75);
      }
      /* The stroke keeps its width in screen pixels at every icon size. */
      path,
      circle {
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
  path() {
    return iconPaths[this.name()] ?? iconPaths["workflows"];
  }
  ringed() {
    return ringedIcons.has(this.name());
  }
  stroke() {
    const size = this.size();
    return size ? String(iconStrokes[size]) : null;
  }
}
