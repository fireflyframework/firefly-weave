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
const paths: Record<string, string> = {
  workflows: "M4 3h6v6H4zM14 15h6v6h-6zM7 9v9h7M10 6h7v9",
  runs: "M9 7l8 5-8 5z",
  home: "M3 10l9-8 9 8M5 9v12h5v-7h4v7h5V9",
  tasks: "M8 4V2h8v2M5 4h14v18H5zM8 11l2 2 5-5M8 17h7",
  email: "M3 5h18v14H3zM3 6l9 7 9-7",
  connections: "M8 2v5M16 2v5M5 7h14v3a7 7 0 0 1-14 0zM12 17v5",
  workers:
    "M5 5h14v14H5zM9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4M9 9h6v6H9z",
  settings:
    "M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2",
  action: "M8 5h12v14H8zM3 9l5 3-5 3",
  transform: "M4 5h6v6H4zM14 13h6v6h-6zM10 8h5v5",
  switch: "M12 2l9 10-9 10L3 12z",
  parallel: "M7 3v18M17 3v18M3 7h8M13 17h8",
  wait: "M12 6v6l4 2",
  signal: "M3 5h18v14H3zM3 6l9 7 9-7",
  humanTask: "M9 4a3 3 0 1 0 6 0M5 21v-5a7 7 0 0 1 14 0v5",
  fail: "M6 6l12 12M18 6L6 18",
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
  trash: "M4 6h16M9 6V3h6v3M7 6l1 15h8l1-15",
  menu: "M3 6h18M3 12h18M3 18h18",
  close: "M5 5l14 14M19 5L5 19",
  save: "M4 3h14l3 3v15H3V3zM7 3v6h10V3M7 21v-7h10v7",
};
@Component({
  selector: "weave-icon",
  standalone: true,
  template: `<svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path [attr.d]="path()" />
    @if (name() === "runs" || name() === "wait" || name() === "settings") {
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
      }
    `,
  ],
})
export class Icon {
  name = input("workflows");
  path() {
    return paths[this.name()] ?? paths["workflows"];
  }
}
