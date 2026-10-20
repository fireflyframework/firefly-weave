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
// Before Studio keeps a test data entry, the Studio host checks it against
// the schema it belongs to (`POST /studio/local/testdata/check`). When the
// host can't answer, Studio applies its own conservative rule: a value whose
// schema mentions a secret anywhere, or whose schema it doesn't know, isn't
// kept. Only pointers and codes come back; values never leave this request.
import type { StudioApi } from "../../api";
import { mentionsSecret } from "../../run/run-memory";
import type { Json } from "../ndv/registry";
import type { CheckCode, CheckTestDataResult } from "./execution";
import type { StudioTestData } from "./test-data";

export const CHECK_WORDS: Record<CheckCode, string> = {
  "WV-SCHEMA-SECRET_VALUE":
    "This value is marked secret in the schema. Test data can't keep secrets; leave the field out.",
  "WV-SCHEMA-CLASSIFICATION":
    "Weave can't tell whether this value holds a secret, so test data can't keep it.",
  "WV-STUDIO-TESTDATA-SCHEMA":
    "This value doesn't fit the step's output fields.",
  "WV-STUDIO-TESTDATA-UNRESOLVED":
    "Studio doesn't know this step's output fields yet, so it can't check the value. Add a response sample or connect to the platform.",
};
export interface CheckInputs {
  source: string;
  definitions: Json[];
  schemaOf(pointer: string): Json | undefined;
}
export type EntryVerdict =
  | { ok: true }
  | { ok: false; code: CheckCode; message: string };

const refused = (code: CheckCode): EntryVerdict => ({
  ok: false,
  code,
  message: CHECK_WORDS[code],
});

export async function checkEntries(
  api: Pick<StudioApi, "request">,
  data: StudioTestData,
  pointers: string[],
  inputs: CheckInputs,
): Promise<Map<string, EntryVerdict>> {
  const verdicts = new Map<string, EntryVerdict>();
  try {
    const answer = await api.request<CheckTestDataResult>(
      "/studio/local/testdata/check",
      "POST",
      {
        source: inputs.source,
        format: "json",
        definitions: inputs.definitions,
        catalog: "none",
        testData: data,
      },
    );
    const byPath = new Map(answer.entries.map((entry) => [entry.path, entry]));
    for (const pointer of pointers) {
      const entry = byPath.get(pointer);
      verdicts.set(
        pointer,
        !entry
          ? refused("WV-STUDIO-TESTDATA-UNRESOLVED")
          : entry.ok || !entry.code
            ? { ok: true }
            : refused(entry.code),
      );
    }
  } catch (error) {
    // A refused request (the body itself) is not "the host can't answer".
    if ((error as { status?: number }).status === 422) throw error;
    for (const pointer of pointers) {
      const schema = inputs.schemaOf(pointer);
      verdicts.set(
        pointer,
        schema === undefined
          ? refused("WV-STUDIO-TESTDATA-UNRESOLVED")
          : mentionsSecret(schema)
            ? refused("WV-SCHEMA-SECRET_VALUE")
            : { ok: true },
      );
    }
  }
  return verdicts;
}
