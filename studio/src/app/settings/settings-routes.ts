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
// Settings has one address per tab: /settings/platforms, /settings/people
// and /settings/preferences; /settings opens Platforms. People and access
// shows only to people who can manage members or grants.
import type { Routes } from "@angular/router";

export type SettingsTab = "platforms" | "people" | "preferences";
export const SETTINGS_TABS: readonly { id: SettingsTab; label: string }[] = [
  { id: "platforms", label: "Platforms" },
  { id: "people", label: "People and access" },
  { id: "preferences", label: "Preferences" },
];

export const settingsPath = (tab: SettingsTab): string => `/settings/${tab}`;

/** The tabs this person sees, in order. */
export function settingsTabs(canSeePeople: boolean) {
  return SETTINGS_TABS.filter((tab) => tab.id !== "people" || canSeePeople);
}

/** The tab an address opens; an unknown or hidden tab opens Platforms. */
export function settingsTab(
  pathname: string,
  canSeePeople: boolean,
): SettingsTab {
  const segment = pathname.split("/")[2] ?? "";
  return (
    settingsTabs(canSeePeople).find((tab) => tab.id === segment)?.id ??
    "platforms"
  );
}

/** Componentless, like Studio's other routes: the shell renders the page. */
export const settingsRoutes: Routes = [
  { path: "settings", pathMatch: "full", redirectTo: "settings/platforms" },
  ...SETTINGS_TABS.map((tab) => ({ path: `settings/${tab.id}`, children: [] })),
];
