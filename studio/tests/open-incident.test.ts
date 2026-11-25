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
// Opening the inbox on an incident from a run: the shell's page first, then
// the address that names the incident, unless the person is elsewhere by then.
import { describe, expect, it } from "vitest";
import { openIncident } from "../src/app/operate/transactions/open-incident";

const incident = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
/** A shell whose page changes to `lands` once it is asked for the inbox. */
function shell(lands: string) {
  const steps: string[] = [];
  const host = {
    view: "runs",
    navigate: async (view: string) => {
      steps.push(`page ${view}`);
      await Promise.resolve();
      host.view = lands;
    },
  };
  const router = {
    navigateByUrl: (url: string, extras?: { replaceUrl?: boolean }) => {
      steps.push(`${extras?.replaceUrl ? "replace" : "push"} ${url}`);
      return Promise.resolve(true);
    },
  };
  return {
    steps,
    open: () =>
      openIncident(
        host as unknown as Parameters<typeof openIncident>[0],
        router as unknown as Parameters<typeof openIncident>[1],
        incident,
      ),
  };
}

describe("opening the inbox on an incident", () => {
  it("moves the page first, then names the incident without another step back", async () => {
    const { open, steps } = shell("incidents");
    await open();
    expect(steps).toEqual([
      "page incidents",
      `replace /operate/incidents?incident=${incident}`,
    ]);
  });

  it("leaves a person who is elsewhere by then where they are", async () => {
    const { open, steps } = shell("workers");
    await open();
    expect(steps).toEqual(["page incidents"]);
  });
});
