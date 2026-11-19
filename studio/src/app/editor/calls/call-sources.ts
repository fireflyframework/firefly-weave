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
// Where one editor window finds the workflows a step may call: the project's
// catalog lock, read once per account and workspace, and the drafts kept on
// this computer, each parsed once per save. Step details' catalog port and
// the kind context read them through here.
import { parse } from "yaml";
import type { Profile, StudioApi } from "../../api";
import type { Json } from "../../forms/core/json";
import type { LocalDrafts } from "../../local-drafts";
import {
  callables,
  lockNote,
  type CallableList,
  type LocalCallee,
} from "./callable-catalog";
import { calledBy, removalQuestion } from "./called-by";
import { findCallee } from "./callee-interface";
import { CatalogLockReader, type CatalogLockState } from "./catalog-lock";

/** What the sources read from the editor's shell. */
export interface CallSourcesHost {
  readonly profile: Profile | null;
  readonly api: StudioApi;
  /** The signed-in account's scope; another account reads its own lock. */
  readonly stepDataScope?: string;
  readonly localDrafts?: LocalDrafts;
  can(capability: string): boolean;
}
/** A row of step details' catalog port: one version of a workflow a step may call. */
export interface WorkflowRow {
  uses: string;
  title: string;
  group: string;
  /** The workflow's name, which heads its versions in the list. */
  heading: string;
  disabled?: string;
  /** "Draft" for a version on this computer, at the end of its row. */
  aside?: string;
  /** Why Studio can't tell whether the version allows this workflow; it stays choosable. */
  note?: string;
}

const PROJECT = "In this project";
const LOCAL = "On this computer";

/** The catalog port's rows: each version with its group, its workflow and why it can't be chosen. */
export function workflowRows(list: CallableList): WorkflowRow[] {
  return list.items.map((item) => ({
    uses: item.uses,
    title: `${item.name} ${item.version}`,
    group: item.source === "project" ? PROJECT : LOCAL,
    heading: item.name,
    ...(item.disabled ? { disabled: item.disabled } : {}),
    ...(item.source === "local" ? { aside: "Draft" } : {}),
    ...(item.warning ? { note: item.warning } : {}),
  }));
}

export class CallSources {
  readonly lock: CatalogLockReader;
  private parsed = new Map<string, LocalCallee>();
  private kept: LocalCallee[] | null = null;

  constructor(private readonly host: CallSourcesHost) {
    this.lock = new CatalogLockReader({
      scope: () => {
        const profile = host.profile;
        return profile?.projectId
          ? [
              profile.name,
              profile.baseUrl,
              profile.tenantId ?? "",
              profile.projectId,
              host.stepDataScope ?? "",
            ].join("\u0000")
          : null;
      },
      canRead: () => host.can("catalog.read"),
      fetch: () => host.api.request(`${host.api.project}/catalog`),
    });
  }

  /** The drafts on this computer, each read and parsed once per save. */
  drafts(): readonly LocalCallee[] {
    if (this.kept) return this.kept;
    const store = this.host.localDrafts;
    const entries = store?.list() ?? [];
    const parsed = new Map<string, LocalCallee>();
    const kept = entries.map((entry) => {
      const key = `${entry.id}\u0000${entry.savedAt}`;
      let callee = this.parsed.get(key);
      if (!callee) {
        const saved = store!.read(entry.id);
        let document: unknown = null;
        try {
          document = saved
            ? saved.format === "json"
              ? JSON.parse(saved.source)
              : parse(saved.source)
            : null;
        } catch {
          document = null;
        }
        callee = { name: entry.name, version: entry.version, document };
      }
      parsed.set(key, callee);
      return callee;
    });
    this.parsed = parsed;
    this.kept = kept;
    return kept;
  }

  /** Reads the drafts on this computer again (one was saved, opened or deleted). */
  refresh(): void {
    this.kept = null;
  }

  /** The versions `caller` may call, and why the project's are missing. */
  list(caller: string, query?: string): CallableList {
    return callables({
      caller,
      lock: this.lock.current(),
      drafts: this.drafts(),
      query,
    });
  }

  /**
   * Why `uses` can't be found here, or, without it, why the project's
   * versions aren't offered (not connected, no catalog read, a failed read).
   */
  note(uses?: string): string | null {
    const lock = this.lock.current();
    if (!uses) return lockNote(lock);
    const callee = findCallee(uses, { lock, drafts: this.drafts() });
    return callee.state === "missing" || callee.state === "unavailable"
      ? callee.message
      : null;
  }

  /** The called version's contract, as `KindContext.workflowContract` answers; null when unknown. */
  contract(uses: string): Json | null {
    const callee = findCallee(uses, {
      lock: this.lock.current(),
      drafts: this.drafts(),
    });
    return callee.state === "found"
      ? (callee.contract as unknown as Json)
      : null;
  }
}

const made = new WeakMap<object, CallSources>();
/** The sources of one editor window, made on first use. */
export function callSources(host: CallSourcesHost): CallSources {
  let sources = made.get(host);
  if (!sources) made.set(host, (sources = new CallSources(host)));
  return sources;
}

/** How long the removal question waits for the project's callers before asking without them. */
export const CALLERS_WAIT_MS = 3_000;

/**
 * The question before Called by a workflow goes: it names the published
 * workflows that call this one when Studio can read them in time.
 */
export async function calledRemovalQuestion(
  host: CallSourcesHost,
  workflow: { metadata: { name: string }; spec: Record<string, unknown> },
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const state = await Promise.race<CatalogLockState | null>([
    callSources(host).lock.read(),
    new Promise(
      (done) => (timer = setTimeout(() => done(null), CALLERS_WAIT_MS)),
    ),
  ]).finally(() => clearTimeout(timer));
  const name = workflow.metadata.name;
  return removalQuestion(
    name,
    state?.kind === "ready"
      ? calledBy({
          workflow: name,
          callable: workflow.spec["callable"],
          lock: state.lock,
        })
      : null,
  );
}
