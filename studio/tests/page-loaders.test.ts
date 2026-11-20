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
// The shell mounts its pages through one outlet: every Operate page and
// Settings has its own lazily loaded page, and every other view shares the
// record lists, so moving between them keeps that page.
import { describe, expect, it } from "vitest";
import { operatePages } from "../src/app/operate/operate-routes";
import { pageKey, pageLoaders } from "../src/app/page-loaders";

describe("the shell's pages", () => {
  it("give every Operate page and Settings its own page, and the record lists one between them", () => {
    for (const page of operatePages) expect(pageKey(page)).toBe(page);
    expect(pageKey("settings")).toBe("settings");
    for (const view of [
      "workflows",
      "tasks",
      "email",
      "connections",
      "connect",
    ])
      expect(pageKey(view)).toBe("records");
    expect(Object.keys(pageLoaders).sort()).toEqual(
      [...operatePages, "settings", "records"].sort(),
    );
  });
});
