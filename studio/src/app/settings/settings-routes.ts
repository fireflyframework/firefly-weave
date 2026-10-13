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
// Settings has one address per tab: /settings/platforms, /settings/people,
// /settings/ai and /settings/preferences; /settings opens Platforms. People
// and access shows only to people who can manage members or grants; AI models
// shows once a platform is connected.
import type { Routes } from "@angular/router";

export type SettingsTab = "platforms" | "people" | "ai" | "preferences";
export const SETTINGS_TABS: readonly { id: SettingsTab; label: string }[] = [
  { id: "platforms", label: "Platforms" },
  { id: "people", label: "People and access" },
  { id: "ai", label: "AI models" },
  { id: "preferences", label: "Preferences" },
];
/** Which optional tabs this person sees. */
export interface SettingsAccess {
  people: boolean;
  ai: boolean;
}

export const settingsPath = (tab: SettingsTab): string => `/settings/${tab}`;

/** The tabs this person sees, in order. */
export function settingsTabs(access: SettingsAccess) {
  return SETTINGS_TABS.filter(
    (tab) =>
      (tab.id !== "people" || access.people) && (tab.id !== "ai" || access.ai),
  );
}

/** The tab an address opens; an unknown or hidden tab opens Platforms. */
export function settingsTab(
  pathname: string,
  access: SettingsAccess,
): SettingsTab {
  const segment = pathname.split("/")[2] ?? "";
  return (
    settingsTabs(access).find((tab) => tab.id === segment)?.id ?? "platforms"
  );
}

/** Componentless, like Studio's other routes: the shell renders the page. */
export const settingsRoutes: Routes = [
  { path: "settings", pathMatch: "full", redirectTo: "settings/platforms" },
  ...SETTINGS_TABS.map((tab) => ({ path: `settings/${tab.id}`, children: [] })),
];
