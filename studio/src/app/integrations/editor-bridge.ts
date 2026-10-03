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
// Editor commands for quick integrations (WP-19/WP-20/WP-23): insert a
// published or newly built action with its connection slot as one undo step,
// and open a template as a new draft. DOM-free and loaded lazily; the shell
// passes itself as the EditorHost.
import type { Target } from "../model";
import type { HttpActionUse } from "./http-action-builder";
import type { EditorHost } from "./editor-host";
import {
  compatibleSlots,
  declareSlot,
  planSlot,
  type SlotPlan,
  type SlotRequirement,
} from "./slot-binding";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** A published action's contract, from the host's cache or its export. */
export async function contractFor(
  host: EditorHost,
  uses: string,
  id?: string,
): Promise<Record<string, unknown> | null> {
  const cached = host.catalogContracts.get(uses);
  if (cached) return cached;
  const row = id
    ? undefined
    : host.actionVersions.find(
        (item) => `${item["name"]}@${item["version"]}` === uses,
      );
  const exportId = id ?? (row ? String(row["id"] ?? "") : "");
  if (!exportId || !host.profile) return null;
  const scope = host.profile;
  try {
    const result = await host.api.request<Record<string, unknown>>(
      `${host.api.project}/actions/${encodeURIComponent(exportId)}/export`,
    );
    const document = result["document"];
    // A workspace change while loading makes the answer meaningless here.
    if (!isRecord(document) || scope !== host.profile) return null;
    host.cacheContract(uses, document);
    return document;
  } catch {
    return null;
  }
}

/** The connection an Action document declares (spec.connection). */
export function requirementOf(
  contract: Record<string, unknown> | null,
): SlotRequirement | null {
  const spec = contract?.["spec"];
  const connection = isRecord(spec) ? spec["connection"] : null;
  return isRecord(connection) && typeof connection["connector"] === "string"
    ? {
        connector: connection["connector"],
        ...(connection["required"] === false ? { required: false } : {}),
      }
    : null;
}

function declare(host: EditorHost, add: NonNullable<SlotPlan["add"]>) {
  const document = structuredClone(host.model.definition);
  document.spec["connections"] = declareSlot(
    document.spec["connections"] as Record<string, unknown> | undefined,
    add,
  );
  host.model.updateWorkflow(document);
}

/**
 * Inserts an action step and, in the same undo step, declares the slot it
 * needs. Returns the new step's ID, or "" when the insert failed.
 */
export function insertActionStep(
  host: EditorHost,
  uses: string,
  plan: SlotPlan,
  requirement: SlotRequirement | null,
  owner: string,
  index: number | undefined,
  detailsMissing = false,
): string {
  host.perform(() =>
    host.model.batch(() => {
      if (plan.add) declare(host, plan.add);
      host.model.insert(
        "action",
        owner,
        index,
        plan.connection ? { uses, connection: plan.connection } : { uses },
      );
    }),
  );
  if (host.error) return "";
  const id = host.model.selected;
  host.message = plan.add
    ? `Inserted ${id}, which calls ${uses}, and added the connection slot ${plan.add.name}.`
    : plan.connection
      ? `Inserted ${id}, which calls ${uses} through the connection slot ${plan.connection}.`
      : requirement
        ? `Inserted ${id}, which calls ${uses}. Choose its connection slot in the inspector.`
        : detailsMissing
          ? `Inserted ${id}, which calls ${uses}. Its details didn't load, so no connection slot was chosen; check the step in the inspector.`
          : `Inserted ${id}, which calls ${uses}.`;
  host.focusStep(id);
  return id;
}

/**
 * Palette and step picker: inserts a published action after the selected
 * step (or at `target`) with its connection slot: the only compatible one,
 * or a new one named after the connector. Without the contract the
 * requirement is unknown, so no slot is guessed.
 */
export async function insertCatalogAction(
  host: EditorHost,
  choice: { uses: string; id?: string },
  target?: Target,
  settled = false,
): Promise<string> {
  host.showPalette = false;
  // The step belongs to the workflow it was asked for: the person may open
  // another one, or leave the designer, while this waits.
  const model = host.model;
  const opened = model.opened;
  const moved = () =>
    host.model !== model || model.opened !== opened || host.view !== "designer";
  if (model.readonly || (!settled && !(await host.ensureApplied())) || moved())
    return "";
  const selected = host.selected;
  const owner = target?.owner ?? selected?.owner ?? "root";
  const index = target
    ? target.index
    : selected
      ? selected.index + 1
      : undefined;
  const contract = await contractFor(host, choice.uses, choice.id);
  if (moved()) return "";
  const requirement = requirementOf(contract);
  const plan = contract ? planSlot(requirement, host.workflowSlots) : {};
  const id = insertActionStep(
    host,
    choice.uses,
    plan,
    requirement,
    owner,
    index,
    !contract,
  );
  host.refreshView();
  return id;
}

/** The builder's published action, cached so the inspector shows it at once. */
function seed(host: EditorHost, use: HttpActionUse) {
  if (!use.placeholder) host.cacheContract(use.uses, use.action);
}

/** "Insert into workflow": a new step after the selected one, with the API's slot. */
export function insertApiAction(host: EditorHost, use: HttpActionUse): string {
  host.apiBuilder = null;
  if (host.model.readonly) return "";
  seed(host, use);
  const selected = host.selected;
  const requirement = { connector: use.connector };
  const plan = planSlot(requirement, host.workflowSlots, use.slot);
  const id = insertActionStep(
    host,
    use.uses,
    plan,
    requirement,
    selected?.owner ?? "root",
    selected ? selected.index + 1 : undefined,
  );
  if (id) host.lastUse = { ...use, slot: plan.connection ?? use.slot };
  host.refreshView();
  return id;
}

/**
 * "Use in this step": the step the builder was opened for calls the new
 * action through the API's slot, declared when needed, as one undo step.
 */
export function useApiAction(host: EditorHost, use: HttpActionUse): string {
  const id = host.apiBuilder?.step ?? "";
  const node = host.model.nodes().find((n) => n.step.id === id);
  if (!node || node.step.kind !== "action" || host.model.readonly)
    return insertApiAction(host, use);
  host.apiBuilder = null;
  seed(host, use);
  // A step that already uses a compatible slot (for example a template's
  // "api" slot) keeps it; otherwise the API's own slot is used or declared.
  const requirement = { connector: use.connector };
  const current = String(node.step["connection"] ?? "");
  const plan = compatibleSlots(requirement, host.workflowSlots).some(
    (slot) => slot.name === current,
  )
    ? { connection: current }
    : planSlot(requirement, host.workflowSlots, use.slot);
  host.perform(() =>
    host.model.batch(() => {
      if (plan.add) declare(host, plan.add);
      host.model.update(
        id,
        JSON.stringify({
          ...node.step,
          uses: use.uses,
          connection: plan.connection,
        }),
      );
    }),
  );
  if (host.error) return "";
  host.lastUse = { ...use, slot: plan.connection ?? use.slot };
  host.message = `${id} now calls ${use.uses} through the connection slot ${plan.connection}. Map its input in the inspector.`;
  host.showInspector = true;
  host.focusInspector();
  host.refreshView();
  return id;
}

/** Opens a template as a new, unsaved draft with its own undo history. */
export function startFromTemplate(
  host: EditorHost,
  choice: { title: string; yaml: string },
) {
  host.showTemplates = false;
  host.newWorkflow();
  host.perform(() => host.model.setSource(choice.yaml, "yaml"));
  host.model.clearHistory();
  host.dirty = false;
  host.scheduleFit();
  // A template that doesn't parse shows its source error; claim nothing else.
  if (!host.error && !host.model.error) {
    host.message = `Opened the template "${choice.title}" as a new draft.`;
    // The control that chose the template is gone: focus the first step.
    const first = host.model.definition.spec.steps[0]?.id;
    if (first) host.focusStep(first);
  }
  host.refreshView();
}
