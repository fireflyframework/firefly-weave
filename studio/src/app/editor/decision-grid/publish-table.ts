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
// Publish table: a table draft goes to the platform through its existing
// decision table publish (which checks the role on the server); the
// idempotency key stays the same across an answer Studio didn't get.
import { stringify } from "yaml";
import type { StudioApi } from "../../api";
import type { PublishedVersion } from "../../integrations/http-action-client";
import type { TableDocument } from "../state/table-drafts";

export const CONNECT_TO_PUBLISH =
  "Connect to a platform to publish this table.";
export const ROLE_CANT_PUBLISH =
  "Your role can't publish tables in this project.";
export const UNKNOWN_OUTCOME =
  "Studio couldn't tell whether the table was published. Publish table again: it's safe to repeat.";

export function publishTable(
  api: Pick<StudioApi, "mutate" | "project">,
  document: TableDocument,
  key: string,
): Promise<PublishedVersion> {
  return api.mutate<PublishedVersion>(
    `${api.project}/decision-tables`,
    "POST",
    { source: stringify(document), format: "yaml" },
    undefined,
    key,
  );
}

/** What a failed publish means: the role, a version taken meanwhile, an answer never received, or the platform's words. */
export function publishFailure(
  error: unknown,
):
  | { kind: "role" }
  | { kind: "taken" }
  | { kind: "unknown" }
  | { kind: "refused"; message: string } {
  const status =
    error && typeof error === "object" && "status" in error
      ? Number((error as { status: unknown }).status)
      : NaN;
  if (Number.isNaN(status) || status === 0 || status >= 500)
    return { kind: "unknown" };
  if (status === 401 || status === 403) return { kind: "role" };
  if (status === 409) return { kind: "taken" };
  return {
    kind: "refused",
    message: error instanceof Error ? error.message : String(error),
  };
}
