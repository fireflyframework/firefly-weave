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
// Whether a key press is someone typing. Its own module, so the shell's key
// handler reads it without loading the shortcut table.

/**
 * Whether a key or clipboard event's target is where someone types: a
 * field, anything inside one, or editable text (contenteditable in any of
 * its forms, plaintext-only included, and every element inside it).
 */
export function typingIn(target: unknown): boolean {
  if (!target || typeof target !== "object") return false;
  const element = target as {
    isContentEditable?: boolean;
    closest?: (selector: string) => unknown;
  };
  return (
    element.isContentEditable === true ||
    !!element.closest?.("input, textarea, select")
  );
}
