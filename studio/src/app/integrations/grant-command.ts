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
// The last step after creating a connection with secrets: let the connector
// release use it. One helper, so every screen shows the same command with the
// real connection revision ID filled in.

/** The capability that lets the HTTP connector read through a connection. */
export const httpReadCapability = "weave-connector-http-read@2.0.0";

export interface GrantStep {
  /** The command to run. */
  command: string;
  /** The request file the command reads (shared platforms only). */
  request: string;
  /** The request file's name, when there is one. */
  file: string;
}

/**
 * The grant for one connection revision. On a platform running on this
 * computer the local CLI grants it directly; on a shared platform an
 * administrator grants the connector release with a request file.
 */
export function grantCommand(revisionId: string, local: boolean): GrantStep {
  if (local)
    return {
      command: `weave platform integrations grant --connection ${revisionId} --access read`,
      request: "",
      file: "",
    };
  return {
    command: "weave workers grant --request grant.json",
    request:
      JSON.stringify(
        {
          release_id: "RELEASE_ID",
          connection_revision_id: revisionId,
          capability: httpReadCapability,
        },
        null,
        2,
      ) + "\n",
    file: "grant.json",
  };
}
