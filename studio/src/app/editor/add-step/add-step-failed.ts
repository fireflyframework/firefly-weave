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
// Drawn in place of the Add a step panel when its code can't load (Studio
// was updated on the server, or the network dropped): it closes the panel's
// request, which also stops holding the keys pressed while it opened, and
// says why.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  type OnInit,
} from "@angular/core";

/** What the fallback needs from the shell. */
export interface AddStepFailedHost {
  closePicker(): void;
  notify(text: string): void;
}

export const PANEL_FAILED =
  "Studio couldn't load Add a step. Reload the page to try again.";

/** Closes the panel's request (which lets go of the held keys) and says why. */
export function panelFailed(host: AddStepFailedHost) {
  host.closePicker();
  host.notify(PANEL_FAILED);
}

@Component({
  selector: "weave-add-step-failed",
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: "",
})
export class AddStepFailed implements OnInit {
  host = input.required<AddStepFailedHost>();
  ngOnInit() {
    const host = this.host();
    // After this render: closing removes this element's own block.
    queueMicrotask(() => panelFailed(host));
  }
}
