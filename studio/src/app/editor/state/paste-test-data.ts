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
// The test data pasted steps bring: their pinned output and their hand-set
// simulated failures (never what runs recorded: the paste plan left those
// out). Loaded when steps are pasted, not with the first page.
import { readTestData } from "./test-data-read";
import { emptyTestData, withFailure, withPin } from "./test-data-ops";
import type { TestDataService } from "./test-data-service";

const PINS = "Some pinned output wasn't pasted: ";

const escape = (key: string) => key.replace(/~/g, "~0").replace(/\//g, "~1");

/**
 * Writes the pasted entries into the test data. What came from the
 * clipboard is read as test data first: only well-formed entries go on.
 * Each is checked as any entry is; one refused is dropped and the rest go
 * on. Returns the pinned steps' keys and, when something was dropped, the
 * toast's words, once.
 */
export async function pasteTestData(
  service: TestDataService,
  entries: {
    pins: Readonly<Record<string, unknown>>;
    errors?: Readonly<Record<string, unknown>>;
  },
): Promise<{ pinned: string[]; refusal: string }> {
  const errors = entries.errors ?? {};
  const reading = readTestData({
    ...emptyTestData(new Date().toISOString()),
    pins: entries.pins,
    ...(Object.keys(errors).length ? { errors } : {}),
  });
  if (!reading.ok)
    return {
      pinned: [],
      refusal: `${Object.keys(errors).length ? "Some test data wasn't pasted: " : PINS}${reading.reason}`,
    };
  let refusal = "";
  let waiting = [
    ...Object.entries(reading.data.pins ?? {}).map(
      ([key, entry]) =>
        ({ pointer: `/pins/${escape(key)}`, key, pin: entry }) as const,
    ),
    ...Object.entries(reading.data.errors ?? {}).map(
      ([key, entry]) =>
        ({ pointer: `/errors/${escape(key)}`, key, failure: entry }) as const,
    ),
  ];
  while (waiting.length) {
    const at = new Date().toISOString();
    let next = service.data();
    for (const item of waiting)
      next =
        "pin" in item
          ? withPin(next, item.key, item.pin, at)
          : withFailure(next, item.key, item.failure, at);
    const outcome = await service.change(
      next,
      waiting.map((item) => item.pointer),
    );
    if (outcome.ok)
      return {
        pinned: waiting.flatMap((item) => ("pin" in item ? [item.key] : [])),
        refusal,
      };
    const refused = waiting.find((item) => item.pointer === outcome.pointer);
    // Only a pin's value is checked by the host: a refused entry is a pin.
    refusal ||= `${PINS}${outcome.message}`;
    // Nothing to drop by pointer (the document changed meanwhile, say): none is pasted.
    if (!refused) return { pinned: [], refusal };
    waiting = waiting.filter((item) => item !== refused);
  }
  return { pinned: [], refusal };
}
