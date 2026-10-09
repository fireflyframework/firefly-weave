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
// The available ways to identify a published resource or a drive item.
import { validVersion } from "../../../forms/core/identifiers";
import type { ParamSpec } from "../registry";
export type ResourceMode = "list" | "name" | "id" | "url";
export const MODE_LABELS: Record<ResourceMode, string> = {
  list: "From list",
  name: "By name",
  id: "By ID",
  url: "By URL",
};
export const BAD_NAME = "Use name@1.2.0, such as orders.get@1.0.0.";
export const BAD_URL = "Paste a link that starts with https://.";
export function resourceModes(spec: ParamSpec): ResourceMode[] {
  return spec.id === "itemId"
    ? ["id", "url"]
    : spec.choices
      ? ["list", "name"]
      : ["name"];
}
export function firstMode(
  modes: ResourceMode[],
  value: string,
  listed: boolean,
): ResourceMode {
  if (modes.includes("url") && /^https?:\/\//.test(value)) return "url";
  if (modes.includes("list") && (!value || listed)) return "list";
  return modes.find((mode) => mode !== "list") ?? modes[0];
}
export function resourceProblem(mode: ResourceMode, text: string): string {
  if (!text) return "";
  if (mode === "name") {
    const [name, version, extra] = text.split("@");
    if (
      !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name ?? "") ||
      !validVersion(version ?? "") ||
      extra !== undefined
    )
      return BAD_NAME;
  }
  if (mode === "url") {
    try {
      if (!text.startsWith("https://") || !new URL(text).hostname)
        return BAD_URL;
    } catch {
      return BAD_URL;
    }
  }
  return "";
}
