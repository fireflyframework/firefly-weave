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
// What a <time> on the Operate pages shows for a platform timestamp: the ISO
// datetime, the full date as its title, and how long ago as its text.
import { absoluteTime, isoTime, relativeTime } from "../format";

/** The `datetime` attribute; none (null) for a value that isn't a date. */
export function timeIso(value: unknown): string | null {
  return isoTime(value) || null;
}

/** The title: "Sat 3 Oct, 17:00" in the viewer's time zone (or `timeZone`). */
export function timeAbsolute(value: unknown, timeZone?: string): string {
  return absoluteTime(value, timeZone);
}

/** The text: "2 min ago", or `fallback` for a value that isn't a date. */
export function timeRelative(
  value: unknown,
  fallback = "Unknown",
  now: Date = new Date(),
): string {
  return relativeTime(value, now) || fallback;
}
