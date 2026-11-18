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
// What step details say when a step runs for real in an environment: why it
// didn't start, and how it ended when it didn't succeed. Every sentence names
// the step and the environment, and never a value of the step's data.
import { OMISSION_WORDS } from "../../operate/run-contracts";
import type { StepTestOmission, StepTestView } from "../state/execution";
import type { StepTestRefusal } from "../state/step-test-client";

/** The platform's code for an email whose delivery can't be confirmed either way. */
export const UNCLEAR_MAIL_CODES: ReadonlySet<string> = new Set([
  "EMAIL_ACCEPTANCE_UNKNOWN",
]);

/** The action's "needs data from earlier steps" reason: Execute previous steps is the way out. */
export const upstreamMissing = (reason: string): boolean =>
  reason.endsWith(
    "needs data from earlier steps. Execute previous steps first.",
  );

/** How a step test ended, when it didn't simply succeed; null for a success. */
export function realEndWords(
  step: string,
  environment: string,
  view: Pick<StepTestView, "status" | "error">,
  timeoutSeconds: number,
): string | null {
  switch (view.status) {
    case "succeeded":
      return null;
    case "cancelled":
      return "Canceled.";
    case "timed_out":
      return `${step} didn't finish in ${environment} within ${timeoutSeconds} s.`;
    default: {
      const error = view.error;
      if (error && UNCLEAR_MAIL_CODES.has(error.code))
        return "The mail server's answer was unclear. Weave won't resend it; check the mailbox.";
      return error
        ? `${step} failed in ${environment}: ${error.code}. ${error.message}`
        : `${step} failed in ${environment}.`;
    }
  }
}

/** Why the platform left a real output out. */
export const omissionWords = (reason: StepTestOmission): string =>
  OMISSION_WORDS[reason];

/** Why a step test didn't start (the refusals that need no question of their own). */
export function refusalWords(
  refusal: StepTestRefusal,
  step: string,
  environment: string,
): string {
  switch (refusal.kind) {
    case "disabled":
      return `Test calls are off in ${environment}. Choose an environment where they're on to execute steps there.`;
    case "forbidden":
      return `Your account can't execute steps in ${environment}. Ask an administrator for the step tester role there.`;
    case "not-found":
      return `${environment} doesn't have what ${step} needs: its saved draft, its action or its connection. Save the workflow and check the step's action and connection.`;
    case "kind":
      return refusal.message;
    case "input": {
      const paths = refusal.diagnostics.map((item) => item.path || "/");
      return `${step}'s input doesn't match what its action accepts (${[...new Set(paths)].join(", ")}). Fix those fields, then try again.`;
    }
    case "secret":
      return "This input holds a value marked secret; Weave won't send it.";
    case "busy":
      return refusal.retryAfterMs > 0
        ? `You already have 4 steps executing in ${environment}. Try again when one finishes.`
        : `Another step is executing in ${environment}. Wait for it to finish, or stop it.`;
    case "account-changed":
      return "You're now signed in as someone else, so Studio sent nothing. Check the account in Settings.";
    case "offline":
      return `Studio can't reach ${environment}, so nothing ran.`;
    case "lost":
      return `Studio couldn't tell whether ${step} started in ${environment}. Find it in Operate › Runs with Include test runs before trying again.`;
    case "failed":
      return refusal.message;
    case "confirm":
      return `${step} changes data in ${environment}. Repeating it repeats the change.`;
    case "release":
      return `Choose which release runs ${step} in ${environment}.`;
    case "connection":
      return (
        Object.values(refusal.slots)[0] ??
        `A connection for ${step} can't be used.`
      );
  }
}
