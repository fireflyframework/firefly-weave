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
// The way from a run and its transactions to their incidents in the inbox, and
// where the notice of a run stopped for a transaction leads.
import type { Router } from "@angular/router";
import type { App } from "../../app";
import {
  defaultIncidentFilters,
  incidentFiltersToQuery,
} from "../incidents/incidents-model";
import { viewPath } from "../operate-routes";
import {
  stoppedForTransaction,
  type RunTransactionView,
} from "./transaction-contracts";

/**
 * Opens the inbox on an address query. The shell moves to the page first: an
 * address alone changes no page. A person who is elsewhere by then stays
 * there.
 */
async function openInbox(
  host: Pick<App, "view" | "navigate">,
  router: Pick<Router, "navigateByUrl">,
  query: string,
): Promise<void> {
  await host.navigate("incidents");
  if (host.view !== "incidents") return;
  await router.navigateByUrl(viewPath("incidents") + query, {
    replaceUrl: true,
  });
}
/** Opens the inbox on an incident. The address names it, never its business key. */
export function openIncident(
  host: Pick<App, "view" | "navigate">,
  router: Pick<Router, "navigateByUrl">,
  id: string,
): Promise<void> {
  return openInbox(
    host,
    router,
    incidentFiltersToQuery(defaultIncidentFilters, id),
  );
}
/**
 * Opens the inbox on the active incidents of a code, those of test runs too:
 * the run it is opened from may be one.
 */
export function showInIncidents(
  host: Pick<App, "view" | "navigate">,
  router: Pick<Router, "navigateByUrl">,
  code: string,
): Promise<void> {
  return openInbox(
    host,
    router,
    incidentFiltersToQuery({
      ...defaultIncidentFilters,
      code,
      includeTest: true,
    }),
  );
}

/** A run's transactions as its section lists them; none when they couldn't be read. */
export interface ListedTransactions {
  views: readonly RunTransactionView[];
  /** No page is left to read. */
  complete: boolean;
}
/** Where a stopped run's notice leads. */
export interface NoticeSteps {
  /** The incident of the transaction in doubt. */
  open: { id: string; label: "Open incident" | "Open first incident" } | null;
  /** The inbox filtered by this code, when no transaction listed names an incident. */
  code: string | null;
  /** The transaction in doubt, when it is listed. */
  goTo: "Go to the transaction" | "Go to the first transaction in doubt" | null;
}
/**
 * Where the notice of a run stopped with `code` leads: null for a run no
 * transaction stopped, and until its transactions are read. It never lacks a
 * step after that: transactions list 50 at a time, oldest first, so the one in
 * doubt may not be listed yet, and while more follow the first listed may not
 * be the first.
 */
export function noticeSteps(
  code: string,
  listed: ListedTransactions | null,
): NoticeSteps | null {
  if (!listed || !stoppedForTransaction(code)) return null;
  const doubts = listed.views.filter((view) => view.state === "in_doubt");
  const several = listed.complete && doubts.length > 1;
  const id = doubts.find((view) => view.incident_id)?.incident_id;
  return {
    open: id
      ? { id, label: several ? "Open first incident" : "Open incident" }
      : null,
    code: id ? null : code,
    goTo: !doubts.length
      ? null
      : several
        ? "Go to the first transaction in doubt"
        : "Go to the transaction",
  };
}
