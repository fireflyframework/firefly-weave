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
// A platform draft's studio documents: its canvas and its test data, saved
// with the draft (`{PROJECT}/drafts/{id}/studio/{kind}`). The platform
// derives whether test data holds run data and refuses secret values with
// document pointers only; a stale revision is a conflict for the person.
import type { StudioApi } from "../../api";
import type {
  StudioDocument,
  StudioDocumentKind,
  StudioDocumentSave,
} from "./test-data";

export const studioDocumentPath = (
  project: string,
  draftId: string,
  kind: StudioDocumentKind,
): string => `${project}/drafts/${encodeURIComponent(draftId)}/studio/${kind}`;

type Failure = {
  status?: number;
  detail?: { code?: string; result?: { paths?: unknown } };
};

export async function readStudioDocument(
  api: Pick<StudioApi, "request" | "project">,
  draftId: string,
  kind: StudioDocumentKind,
): Promise<StudioDocument | null> {
  try {
    const answer = await api.request<StudioDocument>(
      studioDocumentPath(api.project, draftId, kind),
    );
    // Only a document of this kind counts; anything else reads as none, never half-used.
    const document = (answer as { document?: unknown } | null)?.document;
    return answer?.kind === kind &&
      typeof answer.revision === "number" &&
      typeof document === "object" &&
      document !== null &&
      !Array.isArray(document)
      ? answer
      : null;
  } catch (error) {
    if ((error as Failure).status === 404) return null;
    throw error;
  }
}

export async function saveStudioDocument(
  api: Pick<StudioApi, "mutate" | "project">,
  draftId: string,
  kind: StudioDocumentKind,
  body: StudioDocumentSave,
  revision?: number,
): Promise<
  | { ok: true; document: StudioDocument }
  | { ok: false; status: number; code: string; paths: string[] }
> {
  try {
    const document = await api.mutate<StudioDocument>(
      studioDocumentPath(api.project, draftId, kind),
      "PUT",
      body,
      revision,
    );
    return { ok: true, document };
  } catch (error) {
    const failure = error as Failure;
    if (!failure.status || failure.status >= 500 || failure.status === 401)
      throw error;
    const paths = failure.detail?.result?.paths;
    return {
      ok: false,
      status: failure.status,
      code: failure.detail?.code ?? "",
      paths: Array.isArray(paths)
        ? paths.filter((path): path is string => typeof path === "string")
        : [],
    };
  }
}
