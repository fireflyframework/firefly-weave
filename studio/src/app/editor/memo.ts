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
// Recomputes a value only when one of its inputs changed, compared by
// identity: the canvas and the Add a step panel derive their views with it.
export function memo<A extends unknown[], T>(
  compute: (...inputs: A) => T,
): (...inputs: A) => T {
  let last: A | null = null;
  let value!: T;
  return (...inputs: A) => {
    if (!last || inputs.some((item, i) => item !== last![i])) {
      last = inputs;
      value = compute(...inputs);
    }
    return value;
  };
}
