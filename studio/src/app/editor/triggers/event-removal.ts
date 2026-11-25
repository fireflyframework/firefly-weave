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
// The question before a platform event trigger goes: triggers an activation
// already created keep reacting on their versions, so it names each one with
// its environment and version, as far as the platform says in time.
import { ApiError, type StudioApi } from "../../api";
import type { Identity } from "../../connection";
import type { DialogItem, DialogOptions } from "../../dialog";
import { isJsonObject } from "../../forms/core/json";
import { shortId } from "../../format";
import type { Workflow } from "../../model";
import type { ToastService } from "../../toast";
import type { EventTriggerView } from "../activation/event-bindings";
import type { TriggerIntent } from "../state/canvas-sidecar";
import { isPlatformEventInput } from "./platform-event";

/** How long the question waits for the triggers' names before asking without them. */
export const REMOVAL_WAIT_MS = 3_000;
/** A wait this short isn't noticed, so nothing is said of it. */
const UNNOTICED_MS = 300;

/** What the question reads from the editor's shell. */
export interface RemovalHost {
  readonly api: Pick<StudioApi, "project" | "request">;
  readonly identity: Pick<Identity, "workspaces"> | null;
  readonly toasts: Pick<ToastService, "show" | "dismiss" | "current">;
}

const ICON = "signal";

/** The triggers activations created for this choice: environment ID and trigger ID. */
function storedTriggers(intent: TriggerIntent): [string, string][] {
  const resources = intent["resources"];
  return isJsonObject(resources)
    ? Object.entries(resources).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      )
    : [];
}

/** The open project's path; "" while none is. */
function openProject(host: RemovalHost): string {
  try {
    return host.api.project;
  } catch {
    return "";
  }
}

/** Where an environment is, as far as the account's workspaces say: its name, and whether the open project has it. */
function place(host: RemovalHost, id: string): { name: string; here: boolean } {
  for (const tenant of host.identity?.workspaces ?? [])
    for (const project of tenant.projects)
      for (const environment of project.environments)
        if (environment.id === id) {
          const here = openProject(host).endsWith(
            `/tenants/${encodeURIComponent(tenant.id)}/projects/${encodeURIComponent(project.id)}`,
          );
          return {
            name: here
              ? environment.name
              : `${environment.name} in ${project.name}`,
            here,
          };
        }
  return { name: `Environment ${shortId(id)}`, here: false };
}

/** The version an activation runs, or null when it can't be read. */
async function versionOf(
  host: RemovalHost,
  base: string,
  activationId: string,
): Promise<string | null> {
  try {
    const activation = await host.api.request<{
      request: { version_id: string };
    }>(`${base}/activations/${encodeURIComponent(activationId)}`);
    const version = await host.api.request<{ version: string }>(
      `${host.api.project}/workflows/${encodeURIComponent(activation.request.version_id)}`,
    );
    return version.version;
  } catch {
    return null;
  }
}

/** A trigger's row; null when it no longer reacts (stopped or gone). */
async function triggerRow(
  host: RemovalHost,
  environmentId: string,
  triggerId: string,
): Promise<DialogItem | null> {
  const environment = place(host, environmentId);
  const unread = { icon: ICON, text: "A trigger", aside: environment.name };
  const read = async (): Promise<DialogItem | null> => {
    try {
      const base = `${host.api.project}/environments/${encodeURIComponent(environmentId)}`;
      const view = await host.api.request<EventTriggerView>(
        `${base}/event-triggers/${encodeURIComponent(triggerId)}`,
      );
      if (view.disabled) return null;
      const version = await versionOf(host, base, view.activation_id);
      return {
        icon: ICON,
        text: view.name,
        aside: version ? `${environment.name} · ${version}` : environment.name,
      };
    } catch (error) {
      // This project can't find a trigger in another project's environment:
      // it may still react there.
      return error instanceof ApiError &&
        error.status === 404 &&
        environment.here
        ? null
        : unread;
    }
  };
  let timer: ReturnType<typeof setTimeout> | undefined;
  return Promise.race([
    read(),
    new Promise<DialogItem>((done) => {
      timer = setTimeout(() => done(unread), REMOVAL_WAIT_MS);
    }),
  ]).finally(() => clearTimeout(timer));
}

/** Says that the triggers are being read, while a noticeable read lasts. */
async function noticed<T>(host: RemovalHost, read: Promise<T>): Promise<T> {
  let told: number | undefined;
  const timer = setTimeout(() => {
    host.toasts.show({ text: "Checking which triggers still react…" });
    told = host.toasts.current()?.id;
  }, UNNOTICED_MS);
  try {
    return await read;
  } finally {
    clearTimeout(timer);
    if (told !== undefined) host.toasts.dismiss(told);
  }
}

/** The question, or null when no trigger created for this choice still reacts. */
export async function eventRemovalQuestion(
  host: RemovalHost,
  intent: TriggerIntent,
  workflow: Workflow,
): Promise<DialogOptions | null> {
  const rows = await noticed(
    host,
    Promise.all(
      storedTriggers(intent).map(([environment, trigger]) =>
        triggerRow(host, environment, trigger),
      ),
    ),
  );
  const items = rows.filter((row): row is DialogItem => row !== null);
  if (!items.length) return null;
  return {
    title: "Remove the platform event trigger?",
    message: [
      "Triggers already created keep reacting on their versions until you stop them when you next activate.",
      ...(isPlatformEventInput(workflow.spec["inputSchema"])
        ? [
            "The workflow's input stays the platform event. Change it in the workflow settings if this workflow starts another way.",
          ]
        : []),
    ].join("\n\n"),
    items,
    itemStyle: "rows",
    confirmLabel: "Remove",
    danger: true,
    startOnCancel: true,
  };
}
