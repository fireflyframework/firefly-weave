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
/** The same value names in field editors, schemas and data suggestions. */
export const typeLabels: Record<string, string> = {
  string: "Text",
  number: "Number",
  integer: "Whole number",
  boolean: "Yes/No",
  choice: "Choice",
  object: "Group",
  array: "List",
  "date-time": "Date and time",
  file: "File",
  any: "Any value",
  null: "Empty",
  advanced: "Advanced",
  fixed: "Fixed value",
};
export const pluralTypeLabels: Record<string, string> = {
  string: "text",
  number: "numbers",
  integer: "whole numbers",
  boolean: "yes/no values",
  object: "groups",
  array: "lists",
  null: "empty values",
};
