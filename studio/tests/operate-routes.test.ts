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
import { describe, expect, it } from "vitest";
import {
  clustersRecordPath,
  clustersRoute,
  clustersTabPath,
  legacyRedirects,
  metricsPath,
  metricsRoute,
  navEntryVisible,
  operateRoutes,
  releasesPath,
  viewFromPath,
  viewPath,
  workersRoute,
} from "../src/app/operate/operate-routes";

const id = "11111111-1111-4111-8111-111111111111";

describe("Operate routes", () => {
  it("puts the Operate pages under /operate and the rest at the root", () => {
    expect(viewPath("overview")).toBe("/operate/overview");
    expect(viewPath("runs")).toBe("/operate/runs");
    expect(viewPath("incidents")).toBe("/operate/incidents");
    expect(viewPath("workers", id)).toBe(`/operate/workers/${id}`);
    expect(viewPath("clusters")).toBe("/operate/clusters");
    expect(viewPath("tasks")).toBe("/tasks");
    expect(viewPath("settings")).toBe("/settings");
  });

  it("reads the view and the detail record from a path", () => {
    expect(viewFromPath("/operate/runs")).toEqual({ view: "runs", id: "" });
    expect(viewFromPath(`/operate/runs/${id}`)).toEqual({ view: "runs", id });
    expect(viewFromPath(`/operate/workers/${id.toUpperCase()}`)).toEqual({
      view: "workers",
      id,
    });
    expect(viewFromPath("/operate/runs/not-a-run")).toEqual({
      view: "runs",
      id: "",
    });
    expect(viewFromPath(`/operate/clusters/plans/${id}`)).toEqual({
      view: "clusters",
      id: "",
    });
    // Overview is where Operate opens.
    expect(viewFromPath("/operate")).toEqual({ view: "overview", id: "" });
    expect(viewFromPath("/operate/overview")).toEqual({
      view: "overview",
      id: "",
    });
    expect(viewFromPath(`/operate/overview/${id}`)).toEqual({
      view: "overview",
      id: "",
    });
    expect(viewFromPath("/operate/logs")).toBeNull();
    expect(viewFromPath("/tasks")).toEqual({ view: "tasks", id: "" });
    expect(viewFromPath("/")).toBeNull();
  });

  it("maps the paths from before Operate to their new pages", () => {
    expect(viewFromPath("/runs")).toEqual({ view: "runs", id: "" });
    expect(viewFromPath("/workers")).toEqual({ view: "workers", id: "" });
    expect(viewFromPath("/operations")).toEqual({ view: "clusters", id: "" });
    expect(viewFromPath(`/operations/jobs/${id}`)).toEqual({
      view: "clusters",
      id: "",
    });
    expect(
      legacyRedirects.map((route) => [route.path, route.redirectTo]),
    ).toEqual([
      ["operate", "operate/overview"],
      ["runs", "operate/runs"],
      ["workers", "operate/workers"],
      ["operations", "operate/clusters"],
      ["operations/targets/:id", "operate/clusters/targets/:id"],
      ["operations/deployments/:id", "operate/clusters/deployments/:id"],
      ["operations/plans/:id", "operate/clusters/plans/:id"],
      ["operations/jobs/:id", "operate/clusters/jobs/:id"],
    ]);
  });

  it("keeps the Settings view for each tab address", () => {
    for (const tab of ["platforms", "people", "preferences"])
      expect(viewFromPath(`/settings/${tab}`)).toEqual({
        view: "settings",
        id: "",
      });
  });

  it("declares a componentless route for every Operate address", () => {
    expect(operateRoutes.map((route) => route.path)).toEqual([
      "operate/overview",
      "operate/runs",
      "operate/incidents",
      "operate/workers",
      "operate/clusters",
      "operate/metrics",
      "operate/runs/:id",
      "operate/workers/releases",
      "operate/workers/releases/:id",
      "operate/workers/:id",
      "operate/clusters/targets/:id",
      "operate/clusters/deployments/:id",
      "operate/clusters/plans/:id",
      "operate/clusters/jobs/:id",
      "operate/clusters/change-requests/:id",
    ]);
    expect(operateRoutes.every((route) => route.children?.length === 0)).toBe(
      true,
    );
  });

  it("reads the Workers fleet or its releases, never a release as a worker", () => {
    expect(workersRoute("/operate/workers")).toEqual({
      tab: "fleet",
      release: "",
    });
    expect(workersRoute(`/operate/workers/${id}`)).toEqual({
      tab: "fleet",
      release: "",
    });
    expect(workersRoute("/operate/workers/releases")).toEqual({
      tab: "releases",
      release: "",
    });
    expect(
      workersRoute(`/operate/workers/releases/${id.toUpperCase()}`),
    ).toEqual({
      tab: "releases",
      release: id,
    });
    expect(workersRoute("/operate/workers/releases/not-an-id")).toEqual({
      tab: "releases",
      release: "",
    });
    expect(viewFromPath(`/operate/workers/releases/${id}`)).toEqual({
      view: "workers",
      id: "",
    });
    expect(viewFromPath("/operate/workers/releases")).toEqual({
      view: "workers",
      id: "",
    });
    expect(releasesPath()).toBe("/operate/workers/releases");
    expect(releasesPath(id)).toBe(`/operate/workers/releases/${id}`);
  });

  it("reads a Clusters record screen or tab, new and old", () => {
    expect(clustersRoute(`/operate/clusters/plans/${id}`)).toEqual({
      collection: "plans",
      id,
      tab: "targets",
    });
    expect(clustersRoute(`/operations/targets/${id}`)).toEqual({
      collection: "targets",
      id,
      tab: "targets",
    });
    expect(clustersRoute("/operate/clusters", "?tab=approvals")).toEqual({
      collection: null,
      id: "",
      tab: "approvals",
    });
    expect(clustersRoute("/operate/clusters", "?tab=metrics").tab).toBe(
      "targets",
    );
    expect(clustersRoute("/operate/clusters/targets/example").collection).toBe(
      null,
    );
    expect(clustersRecordPath("jobs", id)).toBe(`/operate/clusters/jobs/${id}`);
    expect(clustersTabPath("targets")).toBe("/operate/clusters");
    expect(clustersTabPath("runners")).toBe("/operate/clusters?tab=runners");
  });

  it("keeps the Metrics range and workflow in the address, and nothing else", () => {
    expect(viewPath("metrics")).toBe("/operate/metrics");
    expect(viewFromPath("/operate/metrics")).toEqual({
      view: "metrics",
      id: "",
    });
    expect(metricsRoute("")).toEqual({ range: "24h", workflow: "" });
    expect(metricsRoute("?range=7d&workflow=order-review")).toEqual({
      range: "7d",
      workflow: "order-review",
    });
    // An unknown range falls back to the default; a name that is not a workflow name is dropped.
    expect(metricsRoute("?range=2y&workflow=%3Cscript%3E")).toEqual({
      range: "24h",
      workflow: "",
    });
    expect(metricsPath({ range: "24h", workflow: "" })).toBe(
      "/operate/metrics",
    );
    expect(metricsPath({ range: "1h", workflow: "order-review" })).toBe(
      "/operate/metrics?range=1h&workflow=order-review",
    );
  });

  it("shows every entry without a known identity, and Operate entries by capability", () => {
    for (const entry of [
      "overview",
      "runs",
      "incidents",
      "workers",
      "clusters",
      "metrics",
      "tasks",
    ])
      expect(navEntryVisible(entry, null)).toBe(true);
    const held = new Set(["run.read", "deployment.read"]);
    const holds = (capability: string) => held.has(capability);
    // Overview needs status.read, the summary's own capability.
    expect(navEntryVisible("overview", holds)).toBe(false);
    expect(navEntryVisible("overview", (c) => c === "status.read")).toBe(true);
    expect(navEntryVisible("runs", holds)).toBe(true);
    expect(navEntryVisible("incidents", holds)).toBe(false);
    expect(navEntryVisible("workers", holds)).toBe(false);
    expect(navEntryVisible("clusters", holds)).toBe(true);
    // Metrics reads run statistics, which need status.read like the summary.
    expect(navEntryVisible("metrics", holds)).toBe(false);
    expect(navEntryVisible("metrics", (c) => c === "status.read")).toBe(true);
    // Pages outside Operate never hide: their own pages explain access.
    expect(navEntryVisible("tasks", holds)).toBe(true);
    expect(navEntryVisible("connections", () => false)).toBe(true);
  });
});
