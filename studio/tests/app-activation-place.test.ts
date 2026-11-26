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
// An activation goes where its dialog was opened, as who opened it: a sign-in
// check that ends meanwhile can change either under the open dialog.
import "@angular/compiler";
import { afterAll, describe, expect, it, vi } from "vitest";
vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import { StudioApi } from "../src/app/api";
import { App } from "../src/app/app";
afterAll(() => vi.unstubAllGlobals());

const PROFILE = {
  name: "Test platform",
  baseUrl: "https://weave.invalid",
  tenantId: "t",
  projectId: "p",
  environmentId: "development",
};
const PERSON = {
  principal_id: "person-1",
  kind: "human",
  grants: [] as string[],
  workspaces: [],
  truncated: false,
};
const ASK = {
  bindings: {
    connection_revision_ids: {},
    connector_release_ids: {},
    worker_release_ids: {},
    assignment_binding_ids: {},
  },
  replaces: 1,
};

/** Studio with the activation dialog open for notify-team 1.2.0. */
async function opened() {
  const app = Object.create(App.prototype) as App;
  const sent: { path: string; revision: number | undefined }[] = [];
  const api = new StudioApi();
  const enter = (environmentId: string | undefined) =>
    api.adopt({
      paired: true,
      csrfToken: "x",
      version: "1",
      mode: "connected",
      profile: { ...PROFILE, environmentId },
    } as Parameters<StudioApi["adopt"]>[0]);
  enter("development");
  api.expectedPrincipal = () => app.identity?.principal_id ?? null;
  api.mutate = async (path, _method, _body, revision) => {
    sent.push({ path, revision });
    return { id: "a-new" } as never;
  };
  Object.assign(app, {
    api,
    identity: PERSON,
    published: {
      id: "v-1",
      digest: "d",
      name: "notify-team",
      version: "1.2.0",
    },
    cdr: { markForCheck() {} },
    ensureApplied: async () => true,
    busy: "",
    activatedId: "",
  });
  await app.activate();
  expect(app.showActivation).toBe(true);
  return { app, sent, enter };
}

describe("activating from an open dialog", () => {
  it("activates where and as who it was opened, whatever a newer check grants", async () => {
    const { app, sent } = await opened();
    Object.assign(app, { identity: { ...PERSON, grants: ["run.read"] } });
    await app.confirmActivation(ASK);
    expect(app.activationError).toBeNull();
    expect(app.activatedId).toBe("a-new");
    expect(sent).toEqual([
      {
        path: "/studio/api/api/v1/tenants/t/projects/p/environments/development/activations",
        revision: 1,
      },
    ]);
  });

  it.each<[string, (o: Awaited<ReturnType<typeof opened>>) => void]>([
    ["another environment", ({ enter }) => enter("staging")],
    ["no environment", ({ enter }) => enter(undefined)],
    [
      "another account",
      ({ app }) =>
        Object.assign(app, {
          identity: { ...PERSON, principal_id: "person-2" },
        }),
    ],
    ["no account", ({ app }) => Object.assign(app, { identity: null })],
  ])("activates nothing in %s", async (_what, leave) => {
    const o = await opened();
    leave(o);
    for (let press = 0; press < 2; press++) {
      await o.app.confirmActivation(ASK);
      expect(o.app.activationError).toEqual({
        message:
          "notify-team 1.2.0 wasn't activated: the environment or the account changed since you opened this. Close this and activate again.",
        code: "",
        reopen: true,
      });
    }
    expect(o.sent).toEqual([]);
    expect(o.app.activatedId).toBe("");
    // Opened again, it activates where it is now.
    o.app.closeActivation();
    o.enter("staging");
    await o.app.activate();
    await o.app.confirmActivation(ASK);
    expect(o.sent.map(({ path }) => path)).toEqual([
      "/studio/api/api/v1/tenants/t/projects/p/environments/staging/activations",
    ]);
  });
});
