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
// A link to a workflow's Runs tab, and to one run in it:
// /workflows/<draft>/designer?tab=runs&run=<run ID>. A link only opens the
// tab and the run, read-only; it carries the run's ID and never an
// environment, so it opens where the session is.

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The Runs tab a link asks for, and its run when its ID is a UUID; null for any other link. */
export function parseRunsLink(
  search: string,
): { tab: "runs"; run: string | null } | null {
  const query = new URLSearchParams(search);
  if (query.get("tab") !== "runs") return null;
  const run = query.get("run");
  return { tab: "runs", run: run && UUID.test(run) ? run : null };
}

/** The address of a workflow's Runs tab, with the chosen run when there is one. */
export function runsLink(draftId: string, run: string | null): string {
  const tab = `/workflows/${encodeURIComponent(draftId)}/designer?tab=runs`;
  return run ? `${tab}&run=${encodeURIComponent(run)}` : tab;
}

/**
 * Puts `url` in the address bar in place of the current entry, without a
 * navigation, while the page still shows `path`; history keeps no entry per
 * run, and a page that moved on keeps its own address.
 */
export function replaceAddress(path: string, url: string): void {
  if (location.pathname !== path) return;
  if (location.pathname + location.search === url) return;
  history.replaceState(history.state, "", url);
}

/**
 * The run a link named, kept for the Runs tab of that draft until it opens:
 * opening a draft replaces its address without the query, so the tab can't
 * read the run from the address by then. In memory only, taken once.
 */
let linked: { draftId: string; run: string } | null = null;
export function handOverLinkedRun(draftId: string, run: string | null): void {
  linked = run ? { draftId, run } : null;
}
export function takeLinkedRun(draftId: string): string | null {
  const run = linked?.draftId === draftId ? linked.run : null;
  linked = null;
  return run;
}
