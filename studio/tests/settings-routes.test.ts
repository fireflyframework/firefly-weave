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
import { routes } from "../src/app/routes";
import {
  SETTINGS_TABS,
  settingsPath,
  settingsTab,
  settingsTabs,
} from "../src/app/settings/settings-routes";

const everything = { people: true, ai: true };
const nothing = { people: false, ai: false };
describe("Settings addresses", () => {
  it("gives each tab its own address", () => {
    expect(SETTINGS_TABS.map((tab) => settingsPath(tab.id))).toEqual([
      "/settings/platforms",
      "/settings/people",
      "/settings/ai",
      "/settings/preferences",
    ]);
  });
  it("opens the tab an address names", () => {
    expect(settingsTab("/settings/preferences", nothing)).toBe("preferences");
    expect(settingsTab("/settings/people", everything)).toBe("people");
    expect(settingsTab("/settings/ai", everything)).toBe("ai");
    expect(settingsTab("/settings/platforms", everything)).toBe("platforms");
  });
  it("opens Platforms for /settings, an unknown tab or a tab the person can't see", () => {
    expect(settingsTab("/settings", everything)).toBe("platforms");
    expect(settingsTab("/settings/administration", everything)).toBe(
      "platforms",
    );
    expect(settingsTab("/settings/people", nothing)).toBe("platforms");
    expect(settingsTab("/settings/ai", nothing)).toBe("platforms");
  });
  it("shows People and access to managers and AI models once a platform is connected", () => {
    expect(settingsTabs(nothing).map((tab) => tab.label)).toEqual([
      "Platforms",
      "Preferences",
    ]);
    expect(
      settingsTabs({ people: false, ai: true }).map((tab) => tab.label),
    ).toEqual(["Platforms", "AI models", "Preferences"]);
    expect(settingsTabs(everything).map((tab) => tab.label)).toEqual([
      "Platforms",
      "People and access",
      "AI models",
      "Preferences",
    ]);
  });
  it("routes /settings to Platforms and each tab to the shell", () => {
    const paths = routes.map((route) => route.path);
    expect(routes.find((route) => route.path === "settings")).toMatchObject({
      redirectTo: "settings/platforms",
      pathMatch: "full",
    });
    for (const tab of ["platforms", "people", "ai", "preferences"])
      expect(paths).toContain(`settings/${tab}`);
    expect(paths.indexOf("settings/preferences")).toBeLessThan(
      paths.indexOf("**"),
    );
  });
});
