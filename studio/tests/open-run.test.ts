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
// Debug in editor from Operate opens the run's workflow, then hands the run
// and the request to its Runs tab: a request whose tab didn't open never
// stays behind for a later run to start.
import { afterEach, describe, expect, it } from "vitest";
import type { Workflow } from "../src/app/model";
import {
  openRunInEditor,
  type OpenRunHost,
} from "../src/app/editor/runs-tab/open-run";
import {
  dropDebug,
  takeDebug,
  takeLinkedRun,
} from "../src/app/editor/runs-tab/run-links";

const RUN = "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12";
const VERSION = "7e57d0c5-1b2a-4c3d-8e4f-5a6b7c8d9e0f";
const DRAFT = "00000003-1111-4111-8111-111111111111";
const FENCE = { scope: "3:principal-a", environment: "env-dev" };

const workflow: Workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version: "1.3.0" },
  spec: { steps: [{ id: "send", kind: "action", action: "crm.lookup@2.0.0" }] },
};
const run = {
  id: RUN,
  activation: { request: { version_id: VERSION } },
  state: { status: "failed", steps: {} },
};

/** The run's workflow open in the editor as a platform draft; `selectTab` as given. */
function editorWith(selectTab: OpenRunHost["selectTab"]) {
  const notified: string[] = [];
  const host: OpenRunHost = {
    api: {
      project: "/projects/p",
      environment: "/projects/p/environments/env-dev",
      request: async <T>(path: string) =>
        (path.endsWith(`/runs/${RUN}`)
          ? run
          : { document: structuredClone(workflow) }) as T,
      page: async () => ({ items: [], next_cursor: null }),
      mutate: async <T>() => ({}) as T,
    },
    profile: { environmentId: FENCE.environment },
    stepDataScope: FENCE.scope,
    draftId: DRAFT,
    draftRevision: 2,
    model: { definition: workflow },
    dialogs: { confirm: async () => true },
    notify: (text) => void notified.push(text),
    restoreWorkflow: async () => true,
    navigate: async () => undefined,
    selectTab,
  };
  return { host, notified };
}

describe("Debug in editor from Operate", () => {
  afterEach(() => {
    dropDebug();
    takeLinkedRun(DRAFT);
  });

  it("hands the run and the request to the Runs tab it opens", async () => {
    const { host } = editorWith(async () => true);
    await openRunInEditor(host, RUN, "debug");
    expect(takeLinkedRun(DRAFT)).toBe(RUN);
    expect(takeDebug(DRAFT, RUN, FENCE)).toBe("debug");
  });

  it("forgets the request when the Runs tab doesn't open", async () => {
    const { host } = editorWith(async () => false);
    await openRunInEditor(host, RUN, "debug");
    expect(takeDebug(DRAFT, RUN, FENCE)).toBeNull();
  });
});
