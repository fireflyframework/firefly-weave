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
// Moving step details from one step to another: Previous and Next step, the
// neighbor buttons and their shortcuts.
import type { StepDetailsHost } from "./step-details-host";
import type { StepDetailsRequest } from "./step-details-service";

/**
 * Moves step details to another step through the editor's own opener, which
 * applies pending inspector edits and keeps the inspector on the selection.
 * When the editor refuses, its reason shows as a toast above step details:
 * the shell's banner is under step details' inert layer.
 */
export async function moveToStep(
  host: Pick<StepDetailsHost, "openStepDetails" | "notify" | "error">,
  id: string,
  request: Partial<StepDetailsRequest>,
  current: () => boolean,
): Promise<boolean> {
  const opened = await host.openStepDetails(id, request);
  if (!opened && current())
    host.notify(host.error || `Couldn't open ${id}.`, undefined, "danger");
  return opened;
}
