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
// What the Add a step panel offers: the trigger view, "What happens next?"
// with its categories, one view per app, Custom workers, other actions and
// every version of a published action. Call an API, email and file steps
// are the common actions step details edits as one form. Pure: the panel
// draws a catalog and search.ts searches it. Entries the target can't run
// stay listed, disabled, with the reason.
import {
  compareActionVersions,
  latestActions,
  type ActionPickerItem,
} from "../../integrations/action-picker";
import type { TriggerKind } from "../canvas/layout-ltr";
import {
  TRIGGER_DESCRIPTIONS,
  TRIGGER_ICONS,
  TRIGGER_KEYWORDS,
  TRIGGER_LABELS,
} from "../canvas/trigger-tiles";
import {
  COMMON_ACTIONS,
  FILE_CONNECTORS,
  type CommonAction,
} from "../ndv/owned/common-actions";
import {
  MAX_TRIGGERS,
  TRIGGER_KINDS,
  type TriggerIntent,
} from "../state/canvas-sidecar";

export type Preset =
  | "text-template"
  | "approve-reject"
  | "free-text"
  | "custom-form";
export type PanelCommand =
  | "templates"
  | "new-api-action"
  | "load-more"
  | "retry-catalog";
export type EntryInsert =
  | { kind: "step"; step: string; preset?: Preset }
  | { kind: "action"; uses: string; id?: string }
  | { kind: "trigger"; trigger: TriggerKind }
  | { kind: "common"; common: string; connector?: string }
  | { kind: "command"; command: PanelCommand };
export type EntrySection = "triggers" | "steps" | "actions";
export interface PanelEntry {
  /** Stable across renders: the option's test hook and DOM ID source. */
  id: string;
  label: string;
  description: string;
  /** A `<weave-icon>` name. */
  icon: string;
  /** "trigger" draws the list tile with a rounded left side. */
  shape: "trigger" | "step";
  /** Where search groups it. */
  section: EntrySection;
  keywords: string;
  /** Everyday words that find this entry first ("http" finds Call an API). */
  aliases: string;
  /** Why it can't be used now; null when it can. */
  disabled: string | null;
  insert: EntryInsert | null;
  /** The view → or Enter opens. */
  open: string | null;
  /** The step kind it drags onto a "+" as, or null. */
  drag: string | null;
  /** Right-aligned, for example "12 actions". */
  meta: string;
  /** Draws an arrow: the entry opens a view or a dialog. */
  chevron: boolean;
  /** An older version of a published action: found only by its version. */
  older: boolean;
}
export interface PanelSection {
  /** Empty for a view's single list. */
  heading: string;
  entries: PanelEntry[];
}
export interface PanelView {
  id: string;
  title: string;
  subtitle: string;
  /** The view a category or app belongs to; null at the start. */
  parent: string | null;
  sections: PanelSection[];
  /** One sentence when a list is empty, loading or failed. */
  note: string;
}
export interface KindInfo {
  kind: string;
  label: string;
  description: string;
  keywords: string;
  icon: string;
}
/** An installed app (a connector descriptor). */
export interface ConnectorInfo {
  adapter: string;
  /** name@version of the connector; actions name it in `implementation.uses`. */
  reference: string;
}
/** What the target runs: its language features, and what Studio can't edit yet. */
export interface LanguageTarget {
  features: readonly string[];
  /** Step kinds and workflow fields the manifest marks pending for Studio. */
  pending: ReadonlySet<string>;
}
export type CatalogState =
  | "offline"
  | "loading"
  | "ready"
  | "error"
  | "forbidden";
export interface CatalogInput {
  kinds: readonly KindInfo[];
  /** null when Studio couldn't read what the target runs. */
  language: LanguageTarget | null;
  actions: readonly ActionPickerItem[];
  catalog: CatalogState;
  moreActions: boolean;
  /** Installed apps when connected; null when working locally. */
  connectors: readonly ConnectorInfo[] | null;
  /** The explicit trigger choices. */
  triggers: readonly TriggerIntent[];
  hasSteps: boolean;
  canCreateActions: boolean;
}
export interface Catalog {
  views: ReadonlyMap<string, PanelView>;
  /** Every entry search may find, each once. */
  searchable: readonly PanelEntry[];
  /** "Make a custom API call": what search offers when nothing matches. */
  customCall: PanelEntry;
}

type EntryFields = Pick<
  PanelEntry,
  "id" | "label" | "description" | "icon" | "section"
> &
  Partial<PanelEntry>;
const entry = (fields: EntryFields): PanelEntry => ({
  shape: "step",
  keywords: "",
  aliases: "",
  disabled: null,
  insert: null,
  open: null,
  drag: null,
  meta: "",
  older: false,
  ...fields,
  chevron: fields.chevron ?? !!fields.open,
});

/** Why an entry can't be used here, or null. */
export function gateReason(
  feature: string | undefined,
  pending: string | undefined,
  registered: boolean,
  language: LanguageTarget | null,
  noun: string,
): string | null {
  if (feature && !language?.features.includes(feature))
    return `Needs a platform with ${noun} (${feature})`;
  if ((pending && language?.pending.has(pending)) || !registered)
    return `Studio can't add ${noun} yet.`;
  return null;
}

const CATEGORIES = [
  {
    id: "category:ai",
    label: "AI",
    description: "AI agents and AI tasks",
    icon: "llm",
  },
  {
    id: "category:app",
    label: "Action in an app",
    description: "Call APIs, send email and work with files",
    icon: "action",
  },
  {
    id: "category:data",
    label: "Data transformation",
    description: "Shape, template and look up data",
    icon: "transform",
  },
  {
    id: "category:flow",
    label: "Flow",
    description: "Branch, run in parallel, loop and stop",
    icon: "switch",
  },
  {
    id: "category:wait",
    label: "Wait",
    description: "Pause for a time or a signal",
    icon: "wait",
  },
  {
    id: "category:human",
    label: "Human review",
    description: "Ask a person to decide or answer",
    icon: "humanTask",
  },
  {
    id: "add-trigger",
    label: "Add another trigger",
    description: "Start this workflow another way",
    icon: "manualTrigger",
  },
] as const;

interface StepSpec {
  id: string;
  kind: string;
  label?: string;
  description?: string;
  icon?: string;
  keywords?: string;
  feature?: string;
  /** What the reason calls these steps: "loops". */
  noun?: string;
  preset?: Preset;
  /** Draggable onto a "+" as its kind. */
  drag?: boolean;
  section?: EntrySection;
}
const CATEGORY_STEPS: Readonly<Record<string, readonly StepSpec[]>> = {
  "category:ai": [
    {
      id: "kind:agent",
      kind: "agent",
      label: "AI agent",
      description: "Reasons with a model, tools and memory",
      icon: "agent",
      keywords: "agent assistant chat tools memory reasoning",
      feature: "ai.agent",
      noun: "AI agents",
    },
    { id: "kind:llm", kind: "llm", drag: true },
  ],
  "category:data": [
    { id: "kind:transform", kind: "transform", drag: true },
    {
      id: "preset:text-template",
      kind: "transform",
      preset: "text-template",
      label: "Text template",
      description: "Build text from the fields of earlier steps",
      icon: "textTemplate",
      keywords: "text template string concat join message format",
      feature: "text.concat",
      noun: "text templates",
    },
    { id: "kind:decisionTable", kind: "decisionTable", drag: true },
  ],
  "category:flow": [
    { id: "kind:switch", kind: "switch", drag: true },
    { id: "kind:parallel", kind: "parallel", drag: true },
    {
      id: "kind:forEach",
      kind: "forEach",
      label: "Loop over items",
      description: "Run steps once for each item of a list",
      icon: "forEach",
      keywords: "loop each iterate items list batch for",
      feature: "flow.forEach",
      noun: "loops",
    },
    {
      id: "kind:callWorkflow",
      kind: "callWorkflow",
      label: "Call a workflow",
      description: "Run another workflow and use its result",
      icon: "callWorkflow",
      keywords: "sub-workflow subworkflow call execute run workflow",
      feature: "flow.callWorkflow",
      noun: "sub-workflows",
    },
    {
      id: "kind:fail",
      kind: "fail",
      label: "Stop with error",
      description: "Stop the run with a clear reason",
      keywords: "error throw stop abort fail",
      drag: true,
    },
  ],
  "category:wait": [
    { id: "kind:wait", kind: "wait", drag: true },
    { id: "kind:signal", kind: "signal", drag: true },
  ],
  "category:human": [
    {
      id: "preset:approve-reject",
      kind: "humanTask",
      preset: "approve-reject",
      label: "Approve or reject",
      description: "Ask a person to approve or reject",
      keywords: "approval approve reject review person decide human task",
      drag: true,
    },
    {
      id: "preset:free-text",
      kind: "humanTask",
      preset: "free-text",
      label: "Free text answer",
      description: "Ask a person a question and get their answer",
      keywords: "question answer text person ask human task",
    },
    {
      id: "preset:custom-form",
      kind: "humanTask",
      preset: "custom-form",
      label: "Custom form",
      description: "Ask a person to fill in a form you design",
      keywords: "form fields person input review human task",
    },
  ],
};
/** Call an action: the plain action step, whose action is chosen in step details. */
const CALL_AN_ACTION: StepSpec = {
  id: "kind:action",
  kind: "action",
  description: "Run a published or shared action; choose it in step details",
  drag: true,
  section: "actions",
};

function stepEntry(spec: StepSpec, input: CatalogInput): PanelEntry {
  const info = input.kinds.find((kind) => kind.kind === spec.kind);
  const label = spec.label ?? info?.label ?? spec.kind;
  const disabled = gateReason(
    spec.feature,
    spec.kind,
    !!info,
    input.language,
    spec.noun ?? label.toLowerCase(),
  );
  return entry({
    id: spec.id,
    label,
    description: spec.description ?? info?.description ?? "",
    icon: spec.icon ?? info?.icon ?? "action",
    section: spec.section ?? "steps",
    keywords: [spec.keywords, info?.keywords, spec.kind]
      .filter(Boolean)
      .join(" "),
    disabled,
    insert: {
      kind: "step",
      step: spec.kind,
      ...(spec.preset ? { preset: spec.preset } : {}),
    },
    drag: spec.drag && !disabled ? spec.kind : null,
  });
}

function triggerEntries(
  input: CatalogInput,
  kinds: readonly TriggerKind[] = TRIGGER_KINDS,
): PanelEntry[] {
  const list = input.triggers;
  const has = (kind: TriggerKind) =>
    list.some((intent) => intent.kind === kind);
  const implicit = !list.length && input.hasSteps;
  const full = list.length >= MAX_TRIGGERS;
  return kinds.map((kind) => {
    let disabled: string | null = null;
    if (kind === "manual" && (has("manual") || implicit))
      disabled = "This workflow already starts from its input form.";
    else if (kind === "called" && has("called"))
      disabled = "Other workflows can already call this one.";
    else if (full)
      disabled = `A workflow can have at most ${MAX_TRIGGERS} triggers.`;
    else if (kind === "called") {
      const language = input.language;
      disabled =
        !language || !language.features.includes("flow.callWorkflow")
          ? "Needs a platform with sub-workflows (flow.callWorkflow)"
          : language.pending.has("callable")
            ? "Studio can't make workflows callable yet."
            : null;
    }
    return entry({
      id: `trigger:${kind}`,
      label: TRIGGER_LABELS[kind],
      description: TRIGGER_DESCRIPTIONS[kind],
      icon: TRIGGER_ICONS[kind],
      shape: "trigger",
      section: "triggers",
      keywords: TRIGGER_KEYWORDS[kind],
      disabled,
      insert: { kind: "trigger", trigger: kind },
    });
  });
}

interface AppSpec {
  label: string;
  description: string;
  icon: string;
  /** The common actions (COMMON_ACTIONS IDs) this app offers as steps. */
  common: readonly string[];
  triggers: readonly TriggerKind[];
}
const FILE_STEPS = [
  "list-files",
  "read-file",
  "download-file",
  "write-file",
  "move-file",
  "delete-file",
] as const;
const HTTP = "weave-http";
/** The apps Studio knows. Those with common steps always show; the others show locally, or when installed. */
export const BUILT_IN_APPS: Readonly<Record<string, AppSpec>> = {
  "weave-http": {
    label: "HTTP APIs",
    description: "Call any REST API",
    icon: "action",
    common: ["call-api"],
    triggers: [],
  },
  "weave-email": {
    label: "Email",
    description: "Send and reply to email",
    icon: "email",
    common: ["send-email", "reply-email"],
    triggers: ["email"],
  },
  "weave-ftp": {
    label: "FTP",
    description: "Files on an FTP server",
    icon: "typeFile",
    common: FILE_STEPS,
    triggers: [],
  },
  "weave-ftps": {
    label: "FTPS",
    description: "Files on an FTP server over TLS",
    icon: "typeFile",
    common: FILE_STEPS,
    triggers: [],
  },
  "weave-sftp": {
    label: "SFTP",
    description: "Files on an SFTP server",
    icon: "typeFile",
    common: FILE_STEPS,
    triggers: [],
  },
  "weave-microsoft-drive": {
    label: "Microsoft drive",
    description: "Files in OneDrive or SharePoint",
    icon: "typeFile",
    common: FILE_STEPS,
    triggers: [],
  },
  "weave-google-drive": {
    label: "Google Drive",
    description: "Files in Google Drive",
    icon: "typeFile",
    common: FILE_STEPS,
    triggers: [],
  },
  "weave-postgresql": {
    label: "PostgreSQL",
    description: "Rows in a PostgreSQL database",
    icon: "action",
    common: [],
    triggers: [],
  },
  "weave-kafka": {
    label: "Kafka",
    description: "Messages on Kafka topics",
    icon: "kafka",
    common: [],
    triggers: ["broker"],
  },
  "weave-whatsapp": {
    label: "WhatsApp",
    description: "WhatsApp messages",
    icon: "appEvent",
    common: [],
    triggers: ["provider"],
  },
  "weave-telegram": {
    label: "Telegram",
    description: "Telegram messages",
    icon: "appEvent",
    common: [],
    triggers: ["provider"],
  },
  "weave-teams": {
    label: "Microsoft Teams",
    description: "Teams messages",
    icon: "appEvent",
    common: [],
    triggers: ["provider"],
  },
};
/** Everyday words that find Call an API before anything else. */
const CALL_API_ALIASES = "http https api request url rest";

const humanize = (adapter: string) => {
  const name = adapter.replace(/^weave-/, "").replace(/[-_.]+/g, " ");
  return name.charAt(0).toUpperCase() + name.slice(1);
};
const appOf = (adapter: string): AppSpec =>
  BUILT_IN_APPS[adapter] ?? {
    label: humanize(adapter),
    description: `Actions of the ${humanize(adapter)} app`,
    icon: "action",
    common: [],
    triggers: [],
  };
/** What a sentence calls an app: "email", "SFTP", "Google Drive". */
export function appName(adapter: string): string {
  return adapter === "weave-email" ? "email" : appOf(adapter).label;
}
/** The app a published action runs on: its connector reference's name. */
const appOfAction = (item: ActionPickerItem) =>
  item.connector?.split("@")[0] ?? "";
const plural = (count: number, one: string, many: string) =>
  `${count} ${count === 1 ? one : many}`;
/** A file server's connector reference ("weave-sftp@1.0.0"), for inserting on that server. */
const fileConnectorOf = (adapter: string) =>
  [...FILE_CONNECTORS].find((reference) => reference.startsWith(`${adapter}@`));

function commonEntry(action: CommonAction, adapter: string): PanelEntry {
  const app = appOf(adapter);
  const connector = fileConnectorOf(adapter);
  return entry({
    id: `common:${adapter}:${action.id}`,
    label: action.label,
    description:
      adapter === HTTP
        ? action.description
        : `${app.label} · ${action.description}`,
    icon: action.icon,
    section: "actions",
    keywords: `${action.keywords} ${app.label.toLowerCase()}`,
    aliases: action.id === "call-api" ? CALL_API_ALIASES : "",
    insert: {
      kind: "common",
      common: action.id,
      ...(connector ? { connector } : {}),
    },
  });
}
function actionEntry(
  item: ActionPickerItem,
  icon: string,
  older: boolean,
): PanelEntry {
  const uses = `${item.name}@${item.version}`;
  return entry({
    id: `action:${uses}`,
    label: item.name,
    description: [`Version ${item.version}`, item.operation]
      .filter(Boolean)
      .join(" · "),
    icon,
    section: "actions",
    keywords: [item.connector, item.worker, item.operation, item.description]
      .filter(Boolean)
      .join(" "),
    insert: { kind: "action", uses, ...(item.id ? { id: item.id } : {}) },
    older,
  });
}
/** The newest version of each action, each followed by "All versions" when it has others. */
function actionEntries(
  items: readonly ActionPickerItem[],
  icon: string,
  parent: string,
): { entries: PanelEntry[]; views: PanelView[] } {
  const entries: PanelEntry[] = [];
  const views: PanelView[] = [];
  for (const latest of latestActions(items)) {
    entries.push(actionEntry(latest, icon, false));
    const versions = items
      .filter((item) => item.name === latest.name)
      .sort((a, b) => compareActionVersions(b.version, a.version));
    if (versions.length < 2) continue;
    const id = `versions:${latest.name}`;
    entries.push(
      entry({
        id: `open:${id}`,
        label: `All versions of ${latest.name}`,
        description: `${versions.length} versions, newest first`,
        icon,
        section: "actions",
        open: id,
      }),
    );
    views.push({
      id,
      title: `${latest.name} versions`,
      subtitle: "",
      parent,
      sections: [
        {
          heading: "",
          entries: versions.map((item) =>
            actionEntry(item, icon, item !== latest),
          ),
        },
      ],
      note: "",
    });
  }
  return { entries, views };
}

function catalogNote(input: CatalogInput, empty: boolean): string {
  switch (input.catalog) {
    case "offline":
      return empty ? "Connect to a platform to see its published actions." : "";
    case "forbidden":
      return "Your account can't read the published actions in this workspace.";
    case "loading":
      return "Loading published actions…";
    case "error":
      return "Published actions couldn't be loaded.";
    default:
      return empty ? "No published actions use this app yet." : "";
  }
}
function catalogCommands(input: CatalogInput): PanelEntry[] {
  if (input.catalog === "error")
    return [
      entry({
        id: "command:retry-catalog",
        label: "Try again",
        description: "Load the published actions again",
        icon: "refresh",
        section: "actions",
        insert: { kind: "command", command: "retry-catalog" },
      }),
    ];
  if (input.catalog === "ready" && input.moreActions)
    return [
      entry({
        id: "command:load-more",
        label: "Load more actions",
        description: "This workspace has more published actions",
        icon: "refresh",
        section: "actions",
        insert: { kind: "command", command: "load-more" },
      }),
    ];
  return [];
}
const isInsertable = (item: PanelEntry) =>
  !!item.insert && item.insert.kind !== "command";
const COMMON_APPS = Object.keys(BUILT_IN_APPS).filter(
  (adapter) => BUILT_IN_APPS[adapter].common.length,
);

export function buildCatalog(input: CatalogInput): Catalog {
  const views = new Map<string, PanelView>();
  const add = (view: PanelView) => void views.set(view.id, view);
  const triggers = triggerEntries(input);
  const templates = entry({
    id: "command:templates",
    label: "Start from a template",
    description: "Approval flows, API sync, AI support agent…",
    icon: "template",
    section: "steps",
    keywords: "template example starter sample",
    insert: { kind: "command", command: "templates" },
    chevron: true,
  });
  add({
    id: "triggers",
    title: "What triggers this workflow?",
    subtitle: "A trigger starts each run. You can add more triggers later.",
    parent: null,
    sections: [
      { heading: "Triggers", entries: triggers },
      ...(input.hasSteps
        ? []
        : [{ heading: "Not sure yet?", entries: [templates] }]),
    ],
    note: "",
  });
  add({
    id: "add-trigger",
    title: "Add another trigger",
    subtitle: "",
    parent: "next",
    sections: [{ heading: "Triggers", entries: triggers }],
    note: "",
  });
  add({
    id: "next",
    title: "What happens next?",
    subtitle: "",
    parent: null,
    sections: [
      {
        heading: "",
        entries: CATEGORIES.map((category) =>
          entry({ ...category, section: "steps", open: category.id }),
        ),
      },
    ],
    note: "",
  });
  const steps: PanelEntry[] = [];
  for (const [id, specs] of Object.entries(CATEGORY_STEPS)) {
    const list = specs.map((spec) => stepEntry(spec, input));
    steps.push(...list);
    add({
      id,
      title: CATEGORIES.find((category) => category.id === id)!.label,
      subtitle: "",
      parent: "next",
      sections: [{ heading: "", entries: list }],
      note: "",
    });
  }

  const seen = new Set<string>();
  const items = input.actions.filter((item) => {
    const key = `${item.name}@${item.version}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const byApp = new Map<string, ActionPickerItem[]>();
  const workers: ActionPickerItem[] = [];
  const others: ActionPickerItem[] = [];
  for (const item of items) {
    const app = appOfAction(item);
    if (app) byApp.set(app, [...(byApp.get(app) ?? []), item]);
    else if (item.worker) workers.push(item);
    else others.push(item);
  }
  // One order, local or connected: HTTP APIs first, then by name.
  const apps = [
    ...new Set([
      ...(input.connectors
        ? [
            ...COMMON_APPS,
            ...input.connectors.map(
              (connector) =>
                connector.reference.split("@")[0] || connector.adapter,
            ),
          ]
        : Object.keys(BUILT_IN_APPS)),
      ...byApp.keys(),
    ]),
  ].sort((a, b) =>
    a === HTTP
      ? -1
      : b === HTTP
        ? 1
        : appOf(a).label.localeCompare(appOf(b).label, "en"),
  );
  const commands = catalogCommands(input);
  const common = new Map(COMMON_ACTIONS.map((action) => [action.id, action]));
  const callApi = commonEntry(common.get("call-api")!, HTTP);
  const callAction = stepEntry(CALL_AN_ACTION, input);
  const newApiAction = entry({
    id: "command:new-api-action",
    label: "New API action",
    description:
      "Describe a request or import OpenAPI, to reuse it in other workflows",
    icon: "plus",
    section: "actions",
    keywords: "openapi swagger reusable api action builder",
    disabled: input.canCreateActions
      ? null
      : "Your account can't publish actions in this workspace.",
    insert: { kind: "command", command: "new-api-action" },
    chevron: true,
  });
  const owned: PanelEntry[] = [callApi];
  const published: PanelEntry[] = [];
  const rows: PanelEntry[] = [];
  for (const adapter of apps) {
    const app = appOf(adapter);
    const list = byApp.get(adapter) ?? [];
    const actions = actionEntries(list, app.icon, `app:${adapter}`);
    actions.views.forEach(add);
    published.push(...actions.entries);
    for (const view of actions.views)
      published.push(...view.sections[0].entries);
    const commonList =
      adapter === HTTP
        ? []
        : app.common.flatMap((id) => {
            const action = common.get(id);
            return action ? [commonEntry(action, adapter)] : [];
          });
    owned.push(...commonList);
    const actionSection =
      adapter === HTTP
        ? [callApi, newApiAction, ...actions.entries]
        : [...commonList, ...actions.entries];
    const triggerList = triggerEntries(input, app.triggers);
    add({
      id: `app:${adapter}`,
      title: app.label,
      subtitle: app.description,
      parent: "category:app",
      sections: [
        {
          heading: `Actions (${actionSection.filter(isInsertable).length})`,
          entries: [...actionSection, ...commands],
        },
        ...(triggerList.length
          ? [
              {
                heading: `Triggers (${triggerList.length})`,
                entries: triggerList,
              },
            ]
          : []),
      ],
      note: catalogNote(input, !list.length),
    });
    const count = latestActions(list).length;
    rows.push(
      entry({
        id: `app:${adapter}`,
        label: app.label,
        description: app.description,
        icon: app.icon,
        section: "actions",
        keywords: [adapter, ...commonList.map((item) => item.label)]
          .join(" ")
          .toLowerCase(),
        open: `app:${adapter}`,
        meta: count ? plural(count, "action", "actions") : "",
      }),
    );
  }
  for (const [id, label, description, icon, list] of [
    [
      "workers",
      "Custom workers",
      "Actions that run on your own workers",
      "workers",
      workers,
    ],
    [
      "other",
      "Other actions",
      "Published actions of other kinds",
      "action",
      others,
    ],
  ] as const) {
    if (!list.length) continue;
    const groups = new Map<string, ActionPickerItem[]>();
    for (const item of list) {
      const heading = id === "workers" ? (item.worker ?? "").split("@")[0] : "";
      groups.set(heading, [...(groups.get(heading) ?? []), item]);
    }
    const sections = [...groups].map(([heading, group]) => {
      const actions = actionEntries(group, icon, id);
      actions.views.forEach(add);
      published.push(...actions.entries);
      for (const view of actions.views)
        published.push(...view.sections[0].entries);
      return { heading, entries: actions.entries };
    });
    add({
      id,
      title: label,
      subtitle: description,
      parent: "category:app",
      sections: commands.length
        ? [...sections, { heading: "", entries: commands }]
        : sections,
      note: "",
    });
    rows.push(
      entry({
        id: `open:${id}`,
        label,
        description,
        icon,
        section: "actions",
        keywords: id === "workers" ? "worker task queue custom" : "other",
        open: id,
        meta: plural(latestActions(list).length, "action", "actions"),
      }),
    );
  }
  // App event opens the apps whose events can start a run (each app's own
  // view offers the trigger), when any of them is there.
  const eventApps = rows
    .filter((row) =>
      appOf(row.open!.replace(/^app:/, "")).triggers.includes("provider"),
    )
    .sort((a, b) => a.label.localeCompare(b.label, "en"));
  const appEvent = triggers.findIndex((item) => item.id === "trigger:provider");
  if (eventApps.length && appEvent >= 0) {
    triggers[appEvent] = {
      ...triggers[appEvent],
      open: "app-events",
      chevron: true,
    };
    add({
      id: "app-events",
      title: "App event",
      subtitle: "Start a run when something happens in one of these apps.",
      parent: "triggers",
      sections: [{ heading: "", entries: eventApps }],
      note: "",
    });
  }
  add({
    id: "category:app",
    title: "Action in an app",
    subtitle: "",
    parent: "next",
    sections: [{ heading: "", entries: [callApi, callAction, ...rows] }],
    note: input.catalog === "loading" ? "Loading published actions…" : "",
  });

  const searchable = new Map<string, PanelEntry>();
  for (const item of [
    ...triggers,
    ...steps,
    ...owned,
    callAction,
    ...rows,
    ...published.filter(isInsertable),
    newApiAction,
    ...(input.hasSteps ? [] : [templates]),
  ])
    if (!searchable.has(item.id)) searchable.set(item.id, item);
  return {
    views,
    searchable: [...searchable.values()],
    customCall: {
      ...callApi,
      id: "custom-call",
      label: "Make a custom API call",
    },
  };
}
