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
// Shared inputs for the Add a step tests: the built-in step kinds, a target
// that runs text templates, and published actions of two apps and a worker.
import type { ActionPickerItem } from "../src/app/integrations/action-picker";
import { builtInKinds } from "../src/app/editor/ndv/kinds/builtin";
import { llmKind } from "../src/app/editor/ndv/kinds/llm";
import type {
  Catalog,
  CatalogInput,
  KindInfo,
  PanelEntry,
} from "../src/app/editor/add-step/entries";

export const kinds: KindInfo[] = [...builtInKinds, llmKind].map((kind) => ({
  kind: kind.kind,
  label: kind.label,
  description: kind.description,
  keywords: kind.keywords,
  icon: kind.icon,
}));
export const published: ActionPickerItem[] = [
  {
    id: "a1",
    name: "sql.lookup",
    version: "1.0.0",
    connector: "weave-postgresql@2.0.0",
    operation: "read",
  },
  {
    id: "a2",
    name: "sql.lookup",
    version: "1.1.0",
    connector: "weave-postgresql@2.0.0",
    operation: "read",
  },
  {
    id: "a3",
    name: "crm.lookup",
    version: "2.0.0",
    worker: "crm-lookup@1.0.0",
  },
];
export const installed = [
  { adapter: "weave-http", reference: "weave-http@2.0.0" },
  { adapter: "weave-email", reference: "weave-email@1.0.0" },
  { adapter: "weave-postgresql", reference: "weave-postgresql@2.0.0" },
];
export function catalogInput(extra: Partial<CatalogInput> = {}): CatalogInput {
  return {
    kinds,
    language: {
      features: ["text.concat", "text.join"],
      pending: new Set(["forEach", "callWorkflow", "callable"]),
    },
    actions: [],
    catalog: "offline",
    moreActions: false,
    connectors: null,
    triggers: [],
    hasSteps: true,
    canCreateActions: true,
    ...extra,
  };
}
export function entryById(catalog: Catalog, id: string): PanelEntry {
  const found = [...catalog.views.values()]
    .flatMap((view) => view.sections.flatMap((section) => section.entries))
    .find((entry) => entry.id === id);
  if (!found) throw new Error(`No entry ${id}`);
  return found;
}
export function viewLabels(catalog: Catalog, viewId: string): string[] {
  const view = catalog.views.get(viewId);
  if (!view) throw new Error(`No view ${viewId}`);
  return view.sections.flatMap((section) =>
    section.entries.map((entry) => entry.label),
  );
}
