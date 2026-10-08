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
// Every module that registers step kinds, parameter components or AI agent
// slots, imported lazily. A new step kind appends its module here; the editor
// awaits loadKindRegistrations() before it reads the registry.

/** A registration module: importing it registers nothing until register() runs. */
export interface KindRegistrationModule {
  register(): void;
}
export type KindRegistrationLoader = () => Promise<KindRegistrationModule>;

export const kindRegistrations: readonly KindRegistrationLoader[] = [
  () => import("./builtin"),
  () => import("./llm"),
];

/**
 * Loads and registers each module once. A module that fails to load (a lost
 * chunk) is tried again on the next call; modules that registered stay
 * registered and are not registered twice.
 */
export function createKindLoader(
  modules: readonly KindRegistrationLoader[],
): () => Promise<void> {
  const registered = new Set<number>();
  let loading: Promise<void> | null = null;
  return () => {
    loading ??= Promise.allSettled(
      modules.map(async (load, index) => {
        if (registered.has(index)) return;
        (await load()).register();
        registered.add(index);
      }),
    ).then((results) => {
      const failure = results.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
      if (failure) {
        loading = null;
        throw failure.reason;
      }
    });
    return loading;
  };
}

export const loadKindRegistrations = createKindLoader(kindRegistrations);
