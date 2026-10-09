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
  navEntryVisible,
  operateRoutes,
  viewFromPath,
  viewPath,
} from "../src/app/operate/operate-routes";

const id = "11111111-1111-4111-8111-111111111111";

describe("Operate routes", () => {
  it("puts the Operate pages under /operate and the rest at the root", () => {
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
    expect(viewFromPath("/operate")).toEqual({ view: "runs", id: "" });
    expect(viewFromPath("/operate/metrics")).toBeNull();
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
      ["operate", "operate/runs"],
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
      "operate/runs",
      "operate/incidents",
      "operate/workers",
      "operate/clusters",
      "operate/runs/:id",
      "operate/workers/:id",
      "operate/clusters/targets/:id",
      "operate/clusters/deployments/:id",
      "operate/clusters/plans/:id",
      "operate/clusters/jobs/:id",
    ]);
    expect(operateRoutes.every((route) => route.children?.length === 0)).toBe(
      true,
    );
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

  it("shows every entry without a known identity, and Operate entries by capability", () => {
    for (const entry of ["runs", "incidents", "workers", "clusters", "tasks"])
      expect(navEntryVisible(entry, null)).toBe(true);
    const held = new Set(["run.read", "deployment.read"]);
    const holds = (capability: string) => held.has(capability);
    expect(navEntryVisible("runs", holds)).toBe(true);
    expect(navEntryVisible("incidents", holds)).toBe(false);
    expect(navEntryVisible("workers", holds)).toBe(false);
    expect(navEntryVisible("clusters", holds)).toBe(true);
    // Pages outside Operate never hide: their own pages explain access.
    expect(navEntryVisible("tasks", holds)).toBe(true);
    expect(navEntryVisible("connections", () => false)).toBe(true);
  });
});
