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
// Loading the test data saved in a file. Everyone who can test the workflow
// reads it, so its input is kept as one a person enters is: without its
// secret fields and without the IDs of a real platform event. An input that
// names the environment of the run it came from keeps its value: who may read
// it is who may read that run. Any file can say what its source is, so that
// isn't asked.
import type { JsonObject } from "../../forms/core/json";
import { keptInput } from "../triggers/event-sample";
import type { StudioTestData } from "./test-data";
import { entriesOf } from "./test-data-ops";
import type { ChangeOutcome, TestDataService } from "./test-data-service";

/** Keeps a file's test data in the open workflow, every entry checked; or says why not. */
export async function loadFileTestData(
  service: Pick<TestDataService, "change" | "sampleIds">,
  data: StudioTestData,
  schema: unknown,
  fields: () => JsonObject | null,
): Promise<ChangeOutcome> {
  let next = data;
  if (data.input && !data.input.runEnvironmentId) {
    const { input, ...rest } = data;
    const kept = keptInput(schema, input.value, fields, service.sampleIds);
    if ("problem" in kept) return { ok: false, message: kept.problem };
    next =
      kept.value === undefined
        ? rest
        : { ...data, input: { ...input, value: kept.value } };
  }
  return service.change(
    next,
    entriesOf(next).map((entry) => entry.path),
  );
}
