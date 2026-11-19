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
// The workflows an execution reaches, for the Studio host: every workflow
// the open one calls and, through the drafts on this computer among them, the
// ones those call. A draft here goes with its source, so it runs in the test;
// the others run from the project's copy when the host has one.
import { parse } from "yaml";
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import type { LocalDrafts } from "../../local-drafts";
import { calledReferenceSites } from "../activation/callee-pins";
import type { CalledWorkflows, LocalSource } from "../state/execute-request";
import {
  CALLERS_WAIT_MS,
  callSources,
  type CallSourcesHost,
} from "./call-sources";

/** `name@version` split; null when it isn't one. */
function split(reference: string): [string, string] | null {
  const at = reference.lastIndexOf("@");
  return at > 0 ? [reference.slice(0, at), reference.slice(at + 1)] : null;
}

/** A draft's document, or null when it doesn't parse. */
function documentOf(saved: LocalSource): unknown {
  try {
    return saved.format === "json"
      ? JSON.parse(saved.source)
      : parse(saved.source);
  } catch {
    return null;
  }
}

/**
 * `published` names the references the project publishes: the published
 * version wins over a draft with the same name and version, as in the list.
 */
export function calledWorkflows(
  drafts: Pick<LocalDrafts, "list" | "read"> | undefined,
  workflow: unknown,
  published: (reference: string) => boolean = () => false,
): CalledWorkflows {
  const entries = drafts?.list() ?? [];
  const sources: Record<string, LocalSource> = {};
  const references: string[] = [];
  const queue = calledReferenceSites(workflow).map((site) => site.reference);
  for (let next = 0; next < queue.length; next++) {
    const reference = queue[next];
    if (references.includes(reference)) continue;
    references.push(reference);
    const named = split(reference);
    const entry =
      named &&
      entries.find(
        (item) => item.name === named[0] && item.version === named[1],
      );
    const saved =
      entry && !published(reference) ? drafts?.read(entry.id) : null;
    if (!saved) continue;
    // A draft that doesn't parse still goes: the host says what's wrong with it.
    const source: LocalSource = {
      source: saved.source,
      format: saved.format === "json" ? "json" : "yaml",
    };
    sources[reference] = source;
    queue.push(
      ...calledReferenceSites(documentOf(source)).map((site) => site.reference),
    );
  }
  return { sources, references };
}

/** What the shell gives for the workflows it reaches. */
export type CalleesHost = CallSourcesHost & {
  readonly model: { readonly definition: unknown };
};

/** The project's published workflows, as far as Studio has read them. */
function publishedOf(host: CalleesHost) {
  const state = callSources(host).lock.current();
  return state.kind === "ready" ? state.lock.workflows : new Map();
}

/** The workflows the open workflow reaches, for the execution host. */
export function studioCallees(host: CalleesHost): CalledWorkflows {
  const published = publishedOf(host);
  return calledWorkflows(host.localDrafts, host.model.definition, (reference) =>
    published.has(reference),
  );
}

/**
 * The documents of the workflows it reaches, for the host's checks and
 * compiles: a call's output then has fields, so a pin on it can be checked.
 * The drafts here, and the published versions Studio has read. It waits for
 * the project's published workflows, at most 3 s, so right after a workflow
 * opens a draft doesn't run in place of the published version it repeats;
 * Execute reads the callees after it.
 */
export async function calleeDefinitions(
  host: CalleesHost,
): Promise<JsonObject[]> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([
    callSources(host)
      .lock.read()
      .catch(() => undefined),
    new Promise((done) => (timer = setTimeout(done, CALLERS_WAIT_MS))),
  ]).finally(() => clearTimeout(timer));
  const published = publishedOf(host);
  const reached = calledWorkflows(
    host.localDrafts,
    host.model.definition,
    (reference) => published.has(reference),
  );
  const documents: JsonObject[] = [];
  for (const reference of reached.references) {
    const local = reached.sources[reference];
    const document = local
      ? documentOf(local)
      : published.get(reference)?.document;
    if (isJsonObject(document)) documents.push(document);
  }
  return documents;
}
