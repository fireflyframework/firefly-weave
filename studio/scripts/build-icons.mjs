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
// Writes src/app/icon-data.ts from lucide-static, Studio's only icon source.
// A new icon is a new entry in iconMap, never an inline SVG: run
// `npm run icons` and commit both files. icon.test.ts regenerates the data
// and fails when the committed file differs.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as prettier from "prettier";

export const LUCIDE_VERSION = "1.52.0";

/** Weave icon name → Lucide icon name (one Lucide icon per Weave name). */
export const iconMap = {
  // Navigation and objects
  home: "house",
  workflows: "workflow",
  runs: "circle-play",
  tasks: "clipboard-check",
  email: "mail",
  connections: "plug",
  operations: "network",
  workers: "cpu",
  settings: "settings",
  workspace: "layers",
  user: "user-round",
  template: "layout-template",
  assistant: "messages-square",
  // Step kinds (brand mapping; lanes do not reassign them)
  action: "zap",
  decisionTable: "table",
  llm: "sparkles",
  transform: "braces",
  switch: "split",
  parallel: "git-fork",
  wait: "hourglass",
  signal: "radio-tower",
  humanTask: "user-round-check",
  fail: "octagon-x",
  // Commands
  play: "play",
  plus: "plus",
  search: "search",
  chevron: "chevron-right",
  undo: "undo-2",
  redo: "redo-2",
  fit: "scan",
  source: "code-xml",
  check: "check",
  download: "download",
  upload: "upload",
  trash: "trash",
  menu: "menu",
  more: "ellipsis",
  close: "x",
  save: "save",
  lock: "lock",
  external: "external-link",
  copy: "copy",
  refresh: "refresh-cw",
  warning: "triangle-alert",
  help: "circle-question-mark",
  logout: "log-out",
  cloud: "cloud",
  laptop: "laptop",
  panel: "panel-right",
  tidy: "layout-grid",
  failCircle: "circle-x",
  // Triggers
  manualTrigger: "mouse-pointer-click",
  webhook: "webhook",
  schedule: "calendar-clock",
  kafka: "inbox",
  appEvent: "app-window",
  calledTrigger: "square-arrow-down-right",
  // AI agent slots
  agent: "bot",
  model: "brain-circuit",
  tools: "wrench",
  memory: "database",
  output: "file-output",
  // Flow kinds
  forEach: "iteration-cw",
  callWorkflow: "square-stack",
  textTemplate: "text-cursor-input",
  // Editor
  execute: "flask-conical",
  pin: "pin",
  unpin: "pin-off",
  stale: "clock-alert",
  breakpoint: "circle-dot",
  runHistory: "history",
  stickyNote: "sticky-note",
  minimap: "map",
  zoomIn: "zoom-in",
  zoomOut: "zoom-out",
  drag: "grip-vertical",
  spinner: "loader-circle",
  edit: "pencil",
  docs: "book-open",
  expand: "maximize-2",
  chevronLeft: "chevron-left",
  chevronDown: "chevron-down",
  schemaView: "list-tree",
  tableView: "table-2",
  jsonView: "file-braces",
  // Data types in step details
  typeText: "type",
  typeNumber: "hash",
  typeBoolean: "toggle-left",
  typeList: "list",
  typeObject: "box",
  typeDate: "calendar",
  typeFile: "file",
  typeAny: "circle-dashed",
  // Operate
  overview: "gauge",
  clusters: "server",
  metrics: "chart-line",
  logs: "scroll-text",
  alerts: "bell",
  alertFiring: "bell-ring",
  incidents: "siren",
  approvals: "stamp",
  compose: "container",
  kubernetes: "ship-wheel",
  drain: "circle-pause",
  revoke: "ban",
  // Severity and health
  critical: "octagon-alert",
  info: "info",
  degraded: "circle-alert",
  ok: "circle-check",
  off: "circle-off",
};

/** The attributes weave-icon binds for each element Lucide uses. */
const ATTRIBUTES = {
  path: ["d"],
  circle: ["cx", "cy", "r"],
  rect: ["x", "y", "width", "height", "rx", "ry"],
  line: ["x1", "y1", "x2", "y2"],
  polyline: ["points"],
  polygon: ["points"],
  ellipse: ["cx", "cy", "rx", "ry"],
};
const studio = new URL("../", import.meta.url);
const lucide = new URL("node_modules/lucide-static/", studio);
const target = new URL("src/app/icon-data.ts", studio);
const ELEMENT = /<(\w+)\b([^>]*?)\/>/g;

/** The drawing of one Lucide icon as a list of elements and attributes. */
export function lucideNodes(name) {
  const file = new URL(`icons/${name}.svg`, lucide);
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    throw Error(
      `lucide-static has no icon "${name}" (${fileURLToPath(file)}).`,
    );
  }
  const start = text.indexOf(">", text.indexOf("<svg")) + 1;
  const body = text.slice(start, text.lastIndexOf("</svg>"));
  if (body.replace(ELEMENT, "").trim())
    throw Error(`${name}.svg has content weave-icon cannot draw.`);
  const nodes = [...body.matchAll(ELEMENT)].map(([, tag, rest]) => {
    const allowed = ATTRIBUTES[tag];
    if (!allowed)
      throw Error(`${name}.svg uses <${tag}>, which weave-icon does not draw.`);
    const attrs = {};
    for (const [, key, value] of rest.matchAll(/([\w-]+)="([^"]*)"/g)) {
      if (!allowed.includes(key))
        throw Error(
          `${name}.svg: <${tag}> has an unexpected "${key}" attribute.`,
        );
      attrs[key] = value;
    }
    return { tag, attrs };
  });
  if (!nodes.length) throw Error(`${name}.svg draws nothing.`);
  return nodes;
}

/** The text of src/app/icon-data.ts, formatted as the repository formats it. */
export async function renderIconData() {
  const { version } = JSON.parse(
    readFileSync(new URL("package.json", lucide), "utf8"),
  );
  if (version !== LUCIDE_VERSION)
    throw Error(
      `Expected lucide-static ${LUCIDE_VERSION}, found ${version}. Run npm ci.`,
    );
  const drawings = Object.fromEntries(
    Object.entries(iconMap).map(([weave, name]) => [weave, lucideNodes(name)]),
  );
  const attributes = [...new Set(Object.values(ATTRIBUTES).flat())];
  const source = `/*
Generated by studio/scripts/build-icons.mjs from lucide-static ${LUCIDE_VERSION}
(https://lucide.dev). Do not edit: change iconMap in the script and run
\`npm run icons\`. Icon geometry: Copyright (c) 2026 Lucide Icons and
Contributors, ISC License; icons derived from Feather: Copyright (c)
2013-present Cole Bemis, MIT License. Full text:
studio/public/licenses/lucide-LICENSE.txt.
*/
export type IconTag = ${Object.keys(ATTRIBUTES)
    .map((t) => JSON.stringify(t))
    .join(" | ")};
export type IconAttribute = ${attributes.map((a) => JSON.stringify(a)).join(" | ")};
export interface IconNode {
  readonly tag: IconTag;
  readonly attrs: Readonly<Partial<Record<IconAttribute, string>>>;
}
/** Weave icon name → the Lucide icon it draws. */
export const lucideNames = ${JSON.stringify(iconMap)} as const;
export type IconName = keyof typeof lucideNames;
/** Each icon's elements, in Lucide's order, on a 24 × 24 grid. */
export const iconNodes: Readonly<Record<IconName, readonly IconNode[]>> = ${JSON.stringify(drawings)};
`;
  const options = await prettier.resolveConfig(fileURLToPath(target));
  return prettier.format(source, { ...options, parser: "typescript" });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(target, await renderIconData());
  console.log(
    `Wrote ${fileURLToPath(target)} (${Object.keys(iconMap).length} icons).`,
  );
}
