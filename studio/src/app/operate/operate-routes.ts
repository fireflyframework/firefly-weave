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
// Where the Operate pages live, the paths from before Operate that keep
// working, and which navigation entries a person sees. Pure: the shell and the
// pages call these, tests check them. The server authorizes every request; a
// hidden entry is a convenience, never a control.
import type { Routes } from "@angular/router";
import { SETTINGS_TABS } from "../settings/settings-routes";

/** The Operate pages, in navigation order; Overview is the default. */
export const operatePages = [
  "overview",
  "runs",
  "incidents",
  "workers",
  "clusters",
] as const;
export type OperatePage = (typeof operatePages)[number];
/** The Clusters records that have their own screen. */
export const clusterCollections = [
  "targets",
  "deployments",
  "plans",
  "jobs",
  "change-requests",
] as const;
export type ClusterCollection = (typeof clusterCollections)[number];
/** The Clusters tabs, in order; Targets is the default. */
export const clusterTabs = [
  "targets",
  "change-requests",
  "approvals",
  "jobs",
  "runners",
] as const;
export type ClusterTab = (typeof clusterTabs)[number];

/** The capability each Operate page needs somewhere in the workspace. */
export const operateCapabilities: Record<OperatePage, string> = {
  overview: "status.read",
  runs: "run.read",
  incidents: "incident.read",
  workers: "status.read",
  clusters: "deployment.read",
};

const isOperatePage = (view: string): view is OperatePage =>
  (operatePages as readonly string[]).includes(view);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Where a shell view lives: "/operate/runs", "/operate/runs/ID", "/tasks". */
export function viewPath(view: string, id = ""): string {
  const base = isOperatePage(view) ? `/operate/${view}` : `/${view}`;
  return id ? `${base}/${encodeURIComponent(id)}` : base;
}

/** The view a path shows, and the record a detail path opens. */
export interface ViewRoute {
  view: string;
  id: string;
}

/**
 * The view for a path. "/operate/runs/ID" and "/operate/workers/ID" carry the
 * record ID; the paths from before Operate ("/runs", "/workers",
 * "/operations/…") map to their new pages, because Studio may read the
 * address before the router has redirected it.
 */
export function viewFromPath(pathname: string): ViewRoute | null {
  const parts = pathname.split("/").filter(Boolean);
  if (!parts.length) return null;
  if (parts[0] === "operate") {
    const page = parts[1] ?? "overview";
    if (!isOperatePage(page)) return null;
    const detail =
      (page === "runs" || page === "workers") &&
      parts.length === 3 &&
      uuid.test(parts[2]);
    return { view: page, id: detail ? parts[2].toLowerCase() : "" };
  }
  if (parts[0] === "operations") return { view: "clusters", id: "" };
  if (
    parts[0] === "settings" &&
    parts.length === 2 &&
    SETTINGS_TABS.some((tab) => tab.id === parts[1])
  )
    return { view: "settings", id: "" };
  return parts.length === 1 ? { view: parts[0], id: "" } : null;
}

/** A Workers address: the fleet (and a worker), or releases (and a release). */
export interface WorkersRoute {
  tab: "fleet" | "releases";
  /** The release a "/operate/workers/releases/ID" address opens, or "". */
  release: string;
}

/** Reads "/operate/workers/releases/ID", "/operate/workers/releases" or the fleet. */
export function workersRoute(pathname: string): WorkersRoute {
  const parts = pathname.split("/").filter(Boolean);
  if (
    parts[0] !== "operate" ||
    parts[1] !== "workers" ||
    parts[2] !== "releases"
  )
    return { tab: "fleet", release: "" };
  const id = parts.length === 4 && uuid.test(parts[3]) ? parts[3] : "";
  return { tab: "releases", release: id.toLowerCase() };
}

/** "/operate/workers/releases", or one release's "/operate/workers/releases/ID". */
export function releasesPath(id = ""): string {
  return id
    ? `/operate/workers/releases/${encodeURIComponent(id)}`
    : "/operate/workers/releases";
}

/** A Clusters address: one record's screen, or the tab list. */
export interface ClustersRoute {
  collection: ClusterCollection | null;
  id: string;
  tab: ClusterTab;
}

/** Reads "/operate/clusters/plans/ID" or "/operate/clusters?tab=approvals". */
export function clustersRoute(pathname: string, search = ""): ClustersRoute {
  const match =
    /^\/(?:operate\/clusters|operations)\/(targets|deployments|plans|jobs|change-requests)\/([0-9a-f-]{36})$/.exec(
      pathname,
    );
  const tab = new URLSearchParams(search).get("tab") ?? "";
  return {
    collection: match ? (match[1] as ClusterCollection) : null,
    id: match ? match[2] : "",
    tab: (clusterTabs as readonly string[]).includes(tab)
      ? (tab as ClusterTab)
      : "targets",
  };
}

/** "/operate/clusters/plans/ID". */
export function clustersRecordPath(collection: ClusterCollection, id: string) {
  return `/operate/clusters/${collection}/${encodeURIComponent(id)}`;
}

/** "/operate/clusters", or "/operate/clusters?tab=runners". */
export function clustersTabPath(tab: ClusterTab) {
  return tab === "targets"
    ? "/operate/clusters"
    : `/operate/clusters?tab=${tab}`;
}

/**
 * Whether a navigation entry shows. Without a known identity (local
 * authoring, signed out, still checking) every entry shows and its page
 * explains what it needs; with one, an Operate entry shows only when the
 * person holds its capability somewhere in the workspace.
 */
export function navEntryVisible(
  id: string,
  holds: ((capability: string) => boolean) | null,
): boolean {
  if (!isOperatePage(id) || !holds) return true;
  return holds(operateCapabilities[id]);
}

/** The Operate routes: componentless, like every shell route. */
export const operateRoutes: Routes = [
  ...operatePages.map((page) => ({ path: `operate/${page}`, children: [] })),
  { path: "operate/runs/:id", children: [] },
  // Before the worker detail, so "releases" is never read as a worker ID.
  { path: "operate/workers/releases", children: [] },
  { path: "operate/workers/releases/:id", children: [] },
  { path: "operate/workers/:id", children: [] },
  ...clusterCollections.map((collection) => ({
    path: `operate/clusters/${collection}/:id`,
    children: [],
  })),
];

/** Paths from before Operate, kept working as redirects. */
export const legacyRedirects: Routes = [
  { path: "operate", redirectTo: "operate/overview", pathMatch: "full" },
  { path: "runs", redirectTo: "operate/runs", pathMatch: "full" },
  { path: "workers", redirectTo: "operate/workers", pathMatch: "full" },
  { path: "operations", redirectTo: "operate/clusters", pathMatch: "full" },
  // Change requests came after Operate, so they never had an old address.
  ...clusterCollections
    .filter((collection) => collection !== "change-requests")
    .map((collection) => ({
      path: `operations/${collection}/:id`,
      redirectTo: `operate/clusters/${collection}/:id`,
    })),
];
