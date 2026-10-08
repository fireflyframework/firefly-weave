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
// The limits the Operate pages share: how many pages of a list a page reads
// in one go, and the longest reason the platform accepts.

/**
 * The most pages a list reads in one go: a poll reloads at most 500 workers
 * or incidents (pages of 50), and the approvals inbox reads at most 1,000
 * plans (pages of 100).
 */
export const MAX_PAGES = 10;

/** The longest reason (or evidence reference) the platform accepts. */
export const REASON_LIMIT = 2000;

/** "Keep the reason to 2,000 characters or fewer." */
export function tooLongMessage(what: string): string {
  return `Keep the ${what} to ${REASON_LIMIT.toLocaleString("en-US")} characters or fewer.`;
}
