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
// Shows one lazily loaded page in place, with the shell as its host input.
// The shell's pages need nothing more, so the first page carries this small
// directive rather than NgComponentOutlet. The same component stays in place
// across views (the record lists); another one replaces it and starts at the
// top of the area.
import {
  Directive,
  Input,
  ViewContainerRef,
  inject,
  type ComponentRef,
  type OnChanges,
  type Type,
} from "@angular/core";

@Directive({ selector: "[weavePage]" })
export class PageOutlet implements OnChanges {
  @Input({ required: true }) weavePage!: Type<unknown>;
  @Input({ required: true }) weavePageHost!: unknown;
  private readonly slot = inject(ViewContainerRef);
  /** The page shown: the shell reads the Clusters page's attachments from it. */
  page: ComponentRef<unknown> | null = null;

  ngOnChanges() {
    if (this.page?.componentType !== this.weavePage) {
      this.slot.clear();
      // One area holds every page: another page starts at its top.
      (this.slot.element.nativeElement as Node).parentElement?.scrollTo(0, 0);
      this.page = this.slot.createComponent(this.weavePage);
    }
    this.page.setInput("host", this.weavePageHost);
  }
}
