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
// Open workflow, for a workflow a call step calls or one that calls this one:
// its draft on this computer when there is one, otherwise its published
// version in the project, opened as Studio opens any published version (under
// an identity of its own, never saved over). Loaded only when clicked.
import { describeError } from "../../errors";
import type { LocalDrafts } from "../../local-drafts";
import type { Workflow } from "../../model";

/** Published versions read a page at a time, at most this many pages. */
const MAX_PAGES = 20;

/** What opening a called workflow needs from Studio's shell. */
export interface OpenWorkflowHost {
  readonly localDrafts: Pick<LocalDrafts, "list">;
  readonly profile: unknown;
  readonly api: {
    readonly project: string;
    request<T>(path: string): Promise<T>;
    page(
      collection: string,
      environment?: boolean,
      cursor?: string,
    ): Promise<{
      items: Record<string, unknown>[];
      next_cursor?: string | null;
    }>;
  };
  readonly model: {
    readonly opened: number;
    replace(document: Workflow): void;
  };
  readonly stepDataScope?: string;
  openLocalDraft(id: string): boolean;
  openPublished(record: Record<string, unknown>, load: () => void): void;
  notify(text: string, action?: undefined, tone?: "neutral" | "danger"): void;
}

/** `name@version` as a name and a version; null when it isn't one. */
function parse(uses: string): { name: string; version: string } | null {
  const at = uses.lastIndexOf("@");
  return at > 0 && at < uses.length - 1
    ? { name: uses.slice(0, at), version: uses.slice(at + 1) }
    : null;
}

export async function openCalledWorkflow(
  host: OpenWorkflowHost,
  uses: string,
): Promise<void> {
  const called = parse(uses);
  if (!called) return;
  const { name, version } = called;
  const shown = `${name} ${version}`;
  const local = (host.localDrafts.list() ?? []).find(
    (entry) => entry.name === name && entry.version === version,
  );
  if (local && host.openLocalDraft(local.id)) return;
  if (!host.profile)
    return host.notify(
      `Studio can't find ${shown} on this computer. Connect to a platform to open the project's published workflows.`,
    );
  // An answer that arrives after another workflow opened, or for another account, is dropped.
  const opened = host.model.opened;
  const scope = host.stepDataScope;
  const moved = () =>
    host.model.opened !== opened || host.stepDataScope !== scope;
  try {
    let record: Record<string, unknown> | undefined;
    let cursor: string | undefined;
    for (let page = 0; page < MAX_PAGES && !record; page++) {
      const answer = await host.api.page("workflows", false, cursor);
      if (moved()) return;
      record = answer.items.find(
        (item) => item["name"] === name && item["version"] === version,
      );
      if (!answer.next_cursor) break;
      cursor = answer.next_cursor;
    }
    if (!record)
      return host.notify(`Studio can't find ${shown} in this project.`);
    const detail = await host.api.request<Record<string, unknown>>(
      `${host.api.project}/workflows/${encodeURIComponent(String(record["id"]))}/export`,
    );
    if (moved()) return;
    const document = detail["definition"] ?? detail["document"];
    if (!document || typeof document !== "object")
      return host.notify(
        `Studio couldn't open ${shown}: the platform's answer wasn't a workflow.`,
        undefined,
        "danger",
      );
    host.openPublished(record, () => host.model.replace(document as Workflow));
    host.notify(
      `Opened ${shown} from this project. Your changes start a new draft.`,
    );
  } catch (error) {
    if (!moved())
      host.notify(
        `Studio couldn't open ${shown}: ${describeError(error).message}`,
        undefined,
        "danger",
      );
  }
}
