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
// Which change of an Operate page's scope (scopeKeyOf: the environment, the
// person and whether their sign-in ended) keeps what the person typed.

/**
 * Whether only the person behind the same platform, environment and sign-in
 * became known. A page reads before Studio knows them, with the same session,
 * so nothing read or typed belongs to anyone else; any other change of scope
 * does.
 */
export function personLearned(before: string, after: string): boolean {
  if (!before) return false;
  const [environment, person, ended] = JSON.parse(before) as unknown[];
  const [nextEnvironment, nextPerson, nextEnded] = JSON.parse(
    after,
  ) as unknown[];
  return (
    person === null &&
    nextPerson !== null &&
    environment === nextEnvironment &&
    ended === nextEnded
  );
}
