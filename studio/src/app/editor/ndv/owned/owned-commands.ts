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
// Publish…, Simulate and Save to file, on the shell's side, for workflows
// whose Call an API, email and file steps call actions the workflow owns:
// published actions changed since (in Source, for example) take their next
// version, the actions that aren't published yet go through step details'
// publish dialog first, and Save to file says what its files leave out.
// DOM-free and loaded lazily; the shell passes itself as the host.
import type { StructuredCanvasAdapter } from "../../../model";
import type {
  OwnedPublishOutcome,
  OwnedPublishPurpose,
  OwnedPublishRequest,
  StepDetailsService,
} from "../step-details-service";
import {
  driftedOwned,
  forkEdited,
  ownedExportNote,
  pendingOwned,
  publishRefusal,
  publishedVersionsOf,
  versionMoveText,
  type VersionMove,
} from "./owned-versions";

/** What these commands use of the shell. */
export interface OwnedCommandHost {
  readonly model: StructuredCanvasAdapter;
  readonly editingLocked: boolean;
  readonly actionVersions: readonly Record<string, unknown>[];
  readonly stepDetails: Pick<StepDetailsService, "ownedPublish" | "mounted">;
  error: string;
  errorCode: string;
  busy: string;
  can(capability: string): boolean;
  /** Runs a model edit: marks the workflow changed, or shows the failure. */
  perform(edit: () => void): void;
  notify(text: string): void;
  refreshView(): void;
}
export type OwnedFirst = "none" | "published" | "stopped";

/**
 * Opens the publish dialog in the designer and resolves when it closes; at
 * once, with the reason, when no designer can show it or one is open.
 */
export function publishOwned(
  details: Pick<StepDetailsService, "ownedPublish" | "mounted">,
  request: OwnedPublishRequest,
): Promise<OwnedPublishOutcome> {
  const reason = !details.mounted
    ? "Open the workflow in the designer to publish its actions."
    : details.ownedPublish()
      ? "Wait for the current command to finish."
      : "";
  if (reason) return Promise.resolve({ done: false, published: 0, reason });
  return new Promise((settle) => details.ownedPublish.set({ request, settle }));
}

/** What Save to file adds while some of the workflow's actions aren't published. */
export const exportNote = (model: StructuredCanvasAdapter): string =>
  ownedExportNote(pendingOwned(model).length);

/** Shows why the command stopped, in the banner. */
function stop(host: OwnedCommandHost, reason: string): void {
  host.error = reason;
  host.errorCode = "";
}

/**
 * Gives the published actions that changed outside step details their next
 * version, with every step that calls them, as one undo step, so the
 * platform check and the publish dialog see what will be published. False,
 * with the reason in the banner, when that can't happen now.
 */
export function versionChangedOwned(host: OwnedCommandHost): boolean {
  if (!driftedOwned(host.model).length) return true;
  if (host.editingLocked) {
    stop(
      host,
      "Stop the simulation first: actions that changed after they were published need new versions.",
    );
    return false;
  }
  let moves: VersionMove[] = [];
  let done = false;
  host.perform(() => {
    host.model.batch(() => {
      moves = forkEdited(host.model, (name) =>
        publishedVersionsOf(host.actionVersions, name),
      );
    });
    done = true;
  });
  if (done) for (const move of moves) host.notify(versionMoveText(move));
  return done;
}

/**
 * Before Publish… or Simulate goes on: gives changed published actions
 * their next version, runs `check` (Publish… validates the workflow), then
 * publishes the actions that aren't published yet in step details' dialog.
 * "none" when there were none, "stopped" when the check failed or any action
 * is left unpublished (the check, the dialog or the banner says why). Also
 * says how many actions it published.
 */
export async function publishOwnedFirst(
  host: OwnedCommandHost,
  purpose: OwnedPublishPurpose,
  check: () => Promise<boolean>,
): Promise<{ result: OwnedFirst; published: number }> {
  if (!versionChangedOwned(host) || !(await check()))
    return { result: "stopped", published: 0 };
  if (!pendingOwned(host.model).length) return { result: "none", published: 0 };
  if (!host.can("definition.publish")) {
    stop(host, publishRefusal(purpose));
    return { result: "stopped", published: 0 };
  }
  const { name, version } = host.model.definition.metadata;
  host.busy = purpose;
  try {
    const outcome = await publishOwned(host.stepDetails, {
      purpose,
      workflow: { name, version },
    });
    if (outcome.reason) stop(host, outcome.reason);
    return {
      result: !outcome.done
        ? "stopped"
        : outcome.published
          ? "published"
          : "none",
      published: outcome.published,
    };
  } finally {
    host.busy = "";
    host.refreshView();
  }
}
