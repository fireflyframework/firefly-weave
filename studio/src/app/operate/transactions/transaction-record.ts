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
// What Operate says about a transaction: the tone of its state's pill. Pure.
import type { TransactionState } from "../../editor/transactions/transaction-words";
import type { Tone } from "../../status-labels";

const STATE_TONES: Readonly<Record<TransactionState, Tone>> = {
  open: "neutral",
  deciding: "info",
  committing: "info",
  completed: "success",
  compensating: "info",
  compensated: "warning",
  cancelled: "warning",
  in_doubt: "danger",
  reconciled: "neutral",
  force_ended: "danger",
};
/** A transaction pill's tone: the running states read as Operate's Running. */
export function stateTone(state: TransactionState): Tone {
  return STATE_TONES[state];
}
