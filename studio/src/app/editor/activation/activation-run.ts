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
// What an activation runs, read by its ID: the workflow, and its version.
import type { StudioApi } from "../../api";
import { isJsonObject } from "../../forms/core/json";

const text = (value: unknown) => (typeof value === "string" ? value : "");

/** `environment` is the environment's path; the version is one of the open project's. */
export async function activationRun(
  api: Pick<StudioApi, "project" | "request">,
  environment: string,
  id: string,
): Promise<{ workflow: string; version: string }> {
  const project = api.project;
  const activation = await api.request<Record<string, unknown>>(
    `${environment}/activations/${encodeURIComponent(id)}`,
  );
  const request = activation["request"];
  const version = await api.request<Record<string, unknown>>(
    `${project}/workflows/${encodeURIComponent(text(isJsonObject(request) ? request["version_id"] : ""))}`,
  );
  return {
    workflow: text(activation["name"]),
    version: text(version["version"]),
  };
}
