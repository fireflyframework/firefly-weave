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
// Debug in editor and Copy to editor from Operate's run detail: the run
// opens on the Runs tab of its workflow's newest draft (the one open when
// it is that workflow's), or of a draft created from the version the run
// used, then Debug or Copy starts there. Loaded only when clicked.
import type { DialogOptions } from "../../dialog";
import { describeError } from "../../errors";
import { shortId } from "../../format";
import type { Workflow } from "../../model";
import { findDraft, type DraftPage } from "./draft-for-run";
import { readRunRecord } from "./run-record";
import { handOverDebug, handOverLinkedRun } from "./run-links";
import { workflowOf } from "./runs-tab-state";

/** What opening a run in the editor needs from Studio's shell. */
export interface OpenRunHost {
  readonly api: {
    readonly project: string;
    readonly environment: string;
    request<T>(path: string): Promise<T>;
    page(
      collection: string,
      environment?: boolean,
      cursor?: string,
    ): Promise<DraftPage>;
    mutate<T>(
      path: string,
      method: string,
      body: unknown,
      revision?: number,
      key?: string,
    ): Promise<T>;
  };
  readonly profile: { environmentId?: string | null } | null;
  readonly stepDataScope: string;
  readonly draftId: string;
  readonly draftRevision: number | undefined;
  readonly model: { readonly definition: Workflow };
  readonly dialogs: { confirm(options: DialogOptions): Promise<boolean> };
  notify(text: string, action?: undefined, tone?: "neutral" | "danger"): void;
  restoreWorkflow(id: string): Promise<boolean>;
  navigate(view: "designer"): Promise<unknown>;
  selectTab(tab: string): Promise<boolean>;
}

export async function openRunInEditor(
  host: OpenRunHost,
  runId: string,
  intent: "debug" | "copy" | null,
): Promise<void> {
  const fence = {
    scope: host.stepDataScope,
    environment: host.profile?.environmentId ?? "",
  };
  const holds = () =>
    host.stepDataScope === fence.scope &&
    (host.profile?.environmentId ?? "") === fence.environment;
  const refuse = (message: string) =>
    host.notify(
      `Studio couldn't open run ${shortId(runId)} in the editor: ${message}`,
      undefined,
      "danger",
    );
  try {
    const api = host.api;
    const record = readRunRecord(
      await api.request<unknown>(
        `${api.environment}/runs/${encodeURIComponent(runId)}`,
      ),
    );
    if (!holds()) return;
    if (!record) return refuse("the platform's answer wasn't a run.");
    const exported = await api.request<{ document?: unknown }>(
      `${api.project}/workflows/${encodeURIComponent(record.versionId)}/export`,
    );
    if (!holds()) return;
    const version = workflowOf(exported?.document);
    if (!version) return refuse("its version couldn't be read.");
    const { name } = version.metadata;
    // The workflow open now, when it is this one's draft on the platform.
    const open =
      host.profile &&
      host.draftRevision !== undefined &&
      host.model.definition.metadata.name === name
        ? host.draftId
        : null;
    let draftId = open;
    if (!draftId) {
      const { found } = await findDraft(name, (cursor) =>
        api.page("drafts", false, cursor),
      );
      if (!holds()) return;
      draftId = found?.id ?? null;
    }
    if (!draftId) {
      const create = await host.dialogs.confirm({
        title: `Create a draft from ${version.metadata.version}?`,
        message:
          `${name} has no draft here. Create a draft from ${version.metadata.version} to read this run in the editor?\n\n` +
          `The draft starts as a copy of ${version.metadata.version}, the version this run used; published versions don't change.`,
        confirmLabel: "Create draft",
        cancelLabel: "Cancel",
      });
      if (!create || !holds()) return;
      draftId = crypto.randomUUID();
      // One key for this draft: a save whose answer was lost is sent again
      // as the same save.
      await api.mutate(
        `${api.project}/drafts/${encodeURIComponent(draftId)}`,
        "PUT",
        { document: version },
        undefined,
        crypto.randomUUID(),
      );
      if (!holds()) return;
    }
    if (draftId === open) await host.navigate("designer");
    else if (!(await host.restoreWorkflow(draftId))) return;
    if (!holds() || host.draftId !== draftId) return;
    // The workflow shows: its Runs tab takes the run, then Debug or Copy
    // starts there once the run has loaded.
    handOverLinkedRun(draftId, runId);
    handOverDebug(draftId, runId, intent);
    await host.selectTab("Runs");
  } catch (error) {
    if (holds()) refuse(describeError(error).message);
  }
}
