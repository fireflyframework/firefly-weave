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
// Instance keys, the identity of one run of a step: the step ID, then [i]
// per enclosing loop, #seg.seg for a repeated activation and ~n for a yield
// (`send`, `send[2]`, `support#2.1`). Test data is keyed by them: a step ID
// applies to every iteration, a longer key to one. The pattern is the
// platform's, bound included; a shared fixture keeps the two equal.

const MAX_SAFE = "9007199254740991";

/** Regular-expression source for the decimals 1 to `limit`, without sign or leading zeros. */
function positiveAtMost(limit: string): string {
  const branches =
    limit.length > 1 ? [`[1-9][0-9]{0,${limit.length - 2}}`] : [];
  for (let position = 0; position < limit.length; position++) {
    const digit = Number(limit[position]);
    const lowest = position === 0 ? 1 : 0;
    if (digit > lowest) {
      const below =
        digit - 1 === lowest ? String(lowest) : `[${lowest}-${digit - 1}]`;
      const rest = limit.length - position - 1;
      branches.push(
        limit.slice(0, position) +
          below +
          (rest > 1 ? `[0-9]{${rest}}` : "[0-9]".repeat(rest)),
      );
    }
  }
  branches.push(limit);
  return branches.join("|");
}

const NODE_ID = "@?[A-Za-z0-9][A-Za-z0-9_.:@-]*";
const INDEX = `0|${positiveAtMost(MAX_SAFE)}`;
const COUNT = positiveAtMost(MAX_SAFE);
const SEGMENT = "[A-Za-z0-9]+";

/** The platform's unanchored pattern source (`INSTANCE_KEY_PATTERN`). */
export const INSTANCE_KEY_PATTERN = `${NODE_ID}(?:\\[(?:${INDEX})\\])*(?:#${SEGMENT}(?:\\.${SEGMENT})*)?(?:~(?:${COUNT}))?`;
const KEY = new RegExp(`^(?:${INSTANCE_KEY_PATTERN})$`, "u");

/** A canonical instance key of at most 512 characters. */
export function isInstanceKey(value: unknown): value is string {
  return typeof value === "string" && value.length <= 512 && KEY.test(value);
}

/** The step a key belongs to: the key cut at its first `[`, `#` or `~`. */
export function instanceStep(key: string): string {
  const cut = key.search(/[[#~]/);
  return cut < 0 ? key : key.slice(0, cut);
}

/** The key with its step renamed: `send[2]` becomes `notify[2]` when send becomes notify. */
export function renameInstance(key: string, from: string, to: string): string {
  return instanceStep(key) === from ? to + key.slice(from.length) : key;
}
