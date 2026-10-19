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
import { ApiError, capacityRefusal } from "../../src/app/api";

/**
 * Whether Studio keeps this answer from the platform. A burst of reads can
 * find every admission slot taken; the platform refuses the extra ones with a
 * capacity code and Studio sends them again, so a test waiting for the
 * platform's answer waits past those refusals. An unreadable body is kept, so
 * the test reports it.
 */
export async function keptAnswer(response: {
  status(): number;
  json(): Promise<unknown>;
}): Promise<boolean> {
  if (response.status() !== 429) return true;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return true;
  }
  return !capacityRefusal(429, new ApiError(429, body).code);
}
