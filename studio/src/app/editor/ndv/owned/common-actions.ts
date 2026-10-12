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
// Steps Studio offers by what they do rather than by kind: each inserts an
// action step that calls an action the workflow owns (Call an API, email and
// file steps).
import type { Workflow } from "../../../model";
import { slotsOf } from "../params/slots";
import {
  EMAIL_CONNECTOR,
  newConnectorRecipe,
  newHttpRecipe,
  type OwnedRecipe,
} from "./owned-actions";

/** The file servers whose actions a file step can call; the host offers every operation on each. */
export const FILE_CONNECTORS: ReadonlySet<string> = new Set([
  "weave-ftp@1.0.0",
  "weave-ftps@1.0.0",
  "weave-sftp@1.0.0",
  "weave-microsoft-drive@1.0.0",
  "weave-google-drive@1.0.0",
]);
const DEFAULT_FILE_CONNECTOR = "weave-sftp@1.0.0";

export interface CommonAction {
  id: string;
  label: string;
  description: string;
  /** A `<weave-icon>` name. */
  icon: string;
  keywords: string;
  /** The new step's ID, and for email and files the action's name after the workflow's. */
  idPrefix: string;
  recipe(): OwnedRecipe;
}

const file = (
  id: string,
  label: string,
  description: string,
  icon: string,
  action: string,
  keywords: string,
): CommonAction => ({
  id,
  label,
  description,
  icon,
  keywords: `file files folder ftp sftp drive ${keywords}`,
  idPrefix: id,
  recipe: () => newConnectorRecipe(DEFAULT_FILE_CONNECTOR, action),
});

export const COMMON_ACTIONS: readonly CommonAction[] = [
  {
    id: "call-api",
    label: "Call an API",
    description: "Send an HTTP request to a service and use its answer",
    icon: "webhook",
    keywords: "http https rest request get post put patch url api endpoint",
    idPrefix: "call-api",
    recipe: newHttpRecipe,
  },
  {
    id: "send-email",
    label: "Send email",
    description: "Send an email from a mailbox connection",
    icon: "email",
    keywords: "mail message smtp notify",
    idPrefix: "send-email",
    recipe: () => newConnectorRecipe(EMAIL_CONNECTOR, "send"),
  },
  {
    id: "reply-email",
    label: "Reply to an email",
    description: "Answer in an email conversation",
    icon: "email",
    keywords: "mail answer respond thread",
    idPrefix: "reply-email",
    recipe: () => newConnectorRecipe(EMAIL_CONNECTOR, "reply"),
  },
  file(
    "list-files",
    "List files",
    "List the files in a folder",
    "typeList",
    "list",
    "list directory",
  ),
  file(
    "read-file",
    "Read a file",
    "Read a file's text or data",
    "typeFile",
    "read",
    "read open",
  ),
  file(
    "download-file",
    "Download a file",
    "Get a file for later steps",
    "download",
    "download",
    "download get fetch",
  ),
  file(
    "write-file",
    "Write a file",
    "Save a file to a folder",
    "upload",
    "write",
    "write upload save put",
  ),
  file(
    "move-file",
    "Move a file",
    "Move or rename a file",
    "output",
    "move",
    "move rename",
  ),
  file(
    "delete-file",
    "Delete a file",
    "Remove a file",
    "trash",
    "delete",
    "delete remove",
  ),
];

/** The workflow's file server when its slots name exactly one, else SFTP. */
export function fileConnector(workflow: Workflow): string {
  const servers = [
    ...new Set(slotsOf(workflow).map((slot) => slot.connector)),
  ].filter((connector) => FILE_CONNECTORS.has(connector));
  return servers.length === 1 ? servers[0] : DEFAULT_FILE_CONNECTOR;
}

/** The common action that offers this file operation ("write" is Write a file). */
export function fileAction(operation: string): CommonAction | undefined {
  return COMMON_ACTIONS.find((item) => {
    const recipe = item.recipe();
    return (
      recipe.kind === "connector" &&
      FILE_CONNECTORS.has(recipe.connector) &&
      recipe.action === operation
    );
  });
}
