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
// The way from a run's transaction to its incident in the inbox.
import type { Router } from "@angular/router";
import type { App } from "../../app";
import { viewPath } from "../operate-routes";

/**
 * Opens the inbox on an incident. The shell moves to the page first: an
 * address alone changes no page. A person who is elsewhere by then stays
 * there. The address names the incident, never its business key.
 */
export async function openIncident(
  host: Pick<App, "view" | "navigate">,
  router: Pick<Router, "navigateByUrl">,
  id: string,
): Promise<void> {
  await host.navigate("incidents");
  if (host.view !== "incidents") return;
  await router.navigateByUrl(
    `${viewPath("incidents")}?incident=${encodeURIComponent(id)}`,
    { replaceUrl: true },
  );
}
