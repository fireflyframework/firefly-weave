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
// The pages the shell mounts by view, each loaded the first time it opens.
// One outlet for all of them, rather than a deferred block each, keeps the
// first page small: every block adds a few hundred bytes to it.
import type { Type } from "@angular/core";
import { operatePages } from "./operate/operate-routes";

/** A page the outlet shows: an Operate page, Settings, or the record lists. */
export type PageKey = (typeof operatePages)[number] | "settings" | "records";

/** The page a view shows; every view without its own shares the record lists. */
export function pageKey(view: string): PageKey {
  return view === "settings" ||
    (operatePages as readonly string[]).includes(view)
    ? (view as PageKey)
    : "records";
}

/** Loads each page's component, outside the first page. */
export const pageLoaders: Record<PageKey, () => Promise<Type<unknown>>> = {
  overview: () =>
    import("./operate/overview/overview-page").then((m) => m.OverviewPage),
  runs: () => import("./operate/runs/runs-page").then((m) => m.RunsPage),
  incidents: () =>
    import("./operate/incidents/incidents-page").then((m) => m.IncidentsPage),
  workers: () =>
    import("./operate/workers/workers-page").then((m) => m.WorkersPage),
  clusters: () =>
    import("./operate/clusters/clusters-page").then((m) => m.ClustersPage),
  metrics: () =>
    import("./operate/metrics/metrics-page").then((m) => m.MetricsPage),
  logs: () => import("./operate/logs/logs-page").then((m) => m.LogsPage),
  alerts: () =>
    import("./operate/alerts/alerts-page").then((m) => m.AlertsPage),
  settings: () =>
    import("./settings/settings-page").then((m) => m.SettingsPage),
  records: () => import("./operations/records-view").then((m) => m.RecordsView),
};
