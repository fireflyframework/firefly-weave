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
import "@angular/compiler";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../src/app/api";
import {
  eventRemovalQuestion,
  REMOVAL_WAIT_MS,
} from "../src/app/editor/triggers/event-removal";
import { freshWorkflow, type Workflow } from "../src/app/model";
import { ToastService } from "../src/app/toast";
import type { TriggerIntent } from "../src/app/editor/state/canvas-sidecar";

const PROJECT = "/studio/api/api/v1/tenants/t/projects/p";
const DEVELOPMENT = `${PROJECT}/environments/env-dev`;
const STAGING = `${PROJECT}/environments/env-staging`;
const view = (id: string, name: string, activation: string, extra = {}) => ({
  id,
  name,
  activation_id: activation,
  events: ["transaction.in_doubt"],
  may_reconcile: false,
  workflow_name: "notify-team",
  principal_id: "principal",
  revision: 3,
  disabled: false,
  created_at: "2026-11-20T10:00:00Z",
  ...extra,
});
const ANSWERS: Record<string, unknown> = {
  [`${DEVELOPMENT}/event-triggers/et-2`]: view("et-2", "notify-team-2", "a-2"),
  [`${DEVELOPMENT}/activations/a-2`]: { request: { version_id: "v-2" } },
  [`${PROJECT}/workflows/v-2`]: { version: "1.2.0" },
  [`${STAGING}/event-triggers/et-1`]: view("et-1", "notify-team", "a-1"),
  [`${STAGING}/activations/a-1`]: { request: { version_id: "v-1" } },
  [`${PROJECT}/workflows/v-1`]: { version: "1.1.0" },
};
const missing = (status: number, code: string) =>
  new ApiError(status, { code, message: "Not here." });
/** The platform with these answers; a path without one answers 404, an error is thrown. */
const host = (answers: Record<string, unknown> = ANSWERS) => ({
  api: {
    project: PROJECT,
    request: async (path: string) => {
      const answer = answers[path];
      if (answer === undefined) throw missing(404, "WV-NOT-FOUND");
      if (answer instanceof Error) throw answer;
      return answer;
    },
  } as never,
  identity: {
    workspaces: [
      {
        id: "t",
        name: "Acme",
        projects: [
          {
            id: "p",
            name: "Operations",
            environments: [
              { id: "env-dev", name: "development" },
              { id: "env-staging", name: "staging" },
            ],
          },
          {
            id: "p-2",
            name: "Billing",
            environments: [{ id: "env-billing", name: "staging" }],
          },
        ],
      },
    ],
  },
  toasts: new ToastService(),
});
const intent = (resources?: Record<string, string>): TriggerIntent => ({
  id: "trigger-0000000e",
  kind: "event",
  ...(resources ? { resources } : {}),
});
const reacting = (
  inputSchema: unknown = { $ref: "weave.platform-event-v1" },
) => {
  const workflow: Workflow = freshWorkflow();
  workflow.spec["inputSchema"] = inputSchema;
  workflow.spec["eventTrigger"] = { events: ["transaction.in_doubt"] };
  return workflow;
};
const BOTH = { "env-dev": "et-2", "env-staging": "et-1" };

afterEach(() => void vi.useRealTimers());

describe("the question before a platform event trigger goes", () => {
  it("isn't asked when no trigger was created for it", async () => {
    expect(await eventRemovalQuestion(host(), intent(), reacting())).toBeNull();
    expect(
      await eventRemovalQuestion(host(), intent({}), reacting()),
    ).toBeNull();
  });

  it("names the triggers that keep reacting, by environment and version, and starts on Cancel", async () => {
    expect(
      await eventRemovalQuestion(host(), intent(BOTH), reacting()),
    ).toEqual({
      title: "Remove the platform event trigger?",
      message:
        "Triggers already created keep reacting on their versions until you stop them when you next activate.\n\nThe workflow's input stays the platform event. Change it in the workflow settings if this workflow starts another way.",
      items: [
        { icon: "signal", text: "notify-team-2", aside: "development · 1.2.0" },
        { icon: "signal", text: "notify-team", aside: "staging · 1.1.0" },
      ],
      itemStyle: "rows",
      confirmLabel: "Remove",
      danger: true,
      startOnCancel: true,
    });
  });

  it("says nothing about the input when the workflow kept its own", async () => {
    const question = await eventRemovalQuestion(
      host(),
      intent(BOTH),
      reacting({ type: "object", properties: { reference: {} } }),
    );
    expect(question?.message).toBe(
      "Triggers already created keep reacting on their versions until you stop them when you next activate.",
    );
  });

  it("leaves out a trigger that was stopped or is gone, and isn't asked when none is left", async () => {
    const stopped = {
      ...ANSWERS,
      [`${DEVELOPMENT}/event-triggers/et-2`]: view("et-2", "x", "a-2", {
        disabled: true,
      }),
    };
    expect(
      (
        await eventRemovalQuestion(host(stopped), intent(BOTH), reacting())
      )?.items?.map((item) => item.text),
    ).toEqual(["notify-team"]);
    expect(
      await eventRemovalQuestion(
        host(stopped),
        intent({ "env-dev": "et-2", "env-staging": "et-gone" }),
        reacting(),
      ),
    ).toBeNull();
  });

  it("keeps a trigger in another project's environment, which this project can't find", async () => {
    const question = await eventRemovalQuestion(
      host(),
      intent({ "env-billing": "et-9", "env-unknown": "et-8" }),
      reacting(),
    );
    expect(question?.items).toEqual([
      { icon: "signal", text: "A trigger", aside: "staging in Billing" },
      { icon: "signal", text: "A trigger", aside: "Environment env-unknown" },
    ]);
  });

  it("asks with the environment alone when the trigger can't be read", async () => {
    const question = await eventRemovalQuestion(
      host({
        ...ANSWERS,
        [`${STAGING}/event-triggers/et-1`]: missing(403, "WV-FORBIDDEN"),
        [`${PROJECT}/workflows/v-2`]: missing(403, "WV-FORBIDDEN"),
      }),
      intent(BOTH),
      reacting(),
    );
    expect(question?.items).toEqual([
      { icon: "signal", text: "notify-team-2", aside: "development" },
      { icon: "signal", text: "A trigger", aside: "staging" },
    ]);
  });

  it("asks without details when no workspace is open", async () => {
    const question = await eventRemovalQuestion(
      {
        api: {
          get project(): string {
            throw Error("Choose an authorized workspace before this action.");
          },
        } as never,
        identity: host().identity,
        toasts: new ToastService(),
      },
      intent({
        "0d9f7e61-0000-4000-8000-000000000000": "et-1",
        "env-dev": "et-2",
      }),
      reacting(),
    );
    expect(question?.items).toEqual([
      { icon: "signal", text: "A trigger", aside: "Environment 0d9f7e61" },
      { icon: "signal", text: "A trigger", aside: "development in Operations" },
    ]);
  });

  it("doesn't wait long for a platform that doesn't answer", async () => {
    vi.useFakeTimers();
    const slow = host();
    (slow.api as { request: unknown }).request = () => new Promise(() => {});
    const asked = eventRemovalQuestion(
      slow,
      intent({ "env-dev": "et-2" }),
      reacting(),
    );
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS);
    expect((await asked)?.items).toEqual([
      { icon: "signal", text: "A trigger", aside: "development" },
    ]);
  });

  it("says nothing of a wait too short to notice", async () => {
    vi.useFakeTimers();
    const quick = host();
    const shown = vi.spyOn(quick.toasts, "show");
    await eventRemovalQuestion(quick, intent(BOTH), reacting());
    await eventRemovalQuestion(quick, intent(), reacting());
    expect(shown).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("leaves another toast that came meanwhile", async () => {
    vi.useFakeTimers();
    const slow = host();
    (slow.api as { request: unknown }).request = () => new Promise(() => {});
    const asked = eventRemovalQuestion(
      slow,
      intent({ "env-dev": "et-2" }),
      reacting(),
    );
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS / 2);
    slow.toasts.show({ text: "Saved." });
    await vi.advanceTimersByTimeAsync(REMOVAL_WAIT_MS / 2);
    await asked;
    expect(slow.toasts.current()?.text).toBe("Saved.");
  });
});
