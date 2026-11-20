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
// The Alerts badge's watcher loads after the first page: the production
// build's first page holds none of its code, a later chunk does, and the
// first page stays within its budget (first-page.spec.ts measures it).
import { expect, test } from "@playwright/test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { repository } from "../python-path";

/** Text only the watcher holds: the badge's one read. */
const MARKER = "/alerts?state=firing&limit=1";
const BUILD = join(repository, "studio/dist/studio/browser");

test("the alert watcher loads after the first page", () => {
  // CI builds the production app before the browser tests.
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  expect(first.length).toBeGreaterThan(0);
  expect(
    first.filter((file) =>
      readFileSync(join(BUILD, file), "utf8").includes(MARKER),
    ),
  ).toEqual([]);
  const later = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      !first.includes(file) &&
      readFileSync(join(BUILD, file), "utf8").includes(MARKER),
  );
  expect(later.length).toBe(1);
});
