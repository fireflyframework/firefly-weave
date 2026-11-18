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
// What a step's Connection field shows when Studio is connected to an
// environment, read in one place by the field and the line under it.
import type { BindableItem } from "./connections-api";
import type { BindableList, ConnectionsPort } from "./connections-port";
import type { DevBinding } from "./dev-bindings";

export interface ConnectionView {
  port: ConnectionsPort;
  environment: { id: string; name: string };
  /** The connector the step's action needs; null before an action is chosen. */
  connector: string | null;
  workflow: string;
  /** The slot the step names; "" before a connection is chosen. */
  slot: string;
  /** The connector's connections here; null while they load. */
  list: BindableList | null;
  /** The slot's connection kept on this computer for this environment. */
  binding: DevBinding | null;
  /** The bound connection as the platform lists it now. */
  item: BindableItem | null;
  /** Why the kept connection can't be used any more. */
  stale: string | null;
  testing: boolean;
  /** Why the last test failed. */
  failure: string | null;
  canManage: boolean;
}
