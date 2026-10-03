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
// The quick-integration dialogs the shell opens: the API action builder (with
// the readiness checklist), "New API connection" and "New workflow from a
// template". Load it lazily (@defer) and place it before <weave-dialog-host>,
// so the builder's own confirmations open above it. Each dialog loads its
// own chunk when it first opens.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { AiProviderConnectionForm } from "./ai-provider-connection-form";
import { Modal } from "../dialog";
import type { EditorHost } from "./editor-host";
import { HttpActionBuilder, type HttpActionUse } from "./http-action-builder";
import {
  HttpConnectionForm,
  IntegrationReadiness,
} from "./http-connection-form";
import {
  TemplateGallery,
  type TemplateChoice,
} from "../templates/template-gallery";
import {
  insertApiAction,
  startFromTemplate,
  useApiAction,
} from "./editor-bridge";

@Component({
  selector: "weave-integration-dialogs",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [
    Modal,
    HttpActionBuilder,
    HttpConnectionForm,
    AiProviderConnectionForm,
    IntegrationReadiness,
    TemplateGallery,
  ],
  template: `@if (host().apiBuilder; as builder) {
      @defer (on immediate) {
        <weave-http-action-builder
          [api]="host().api"
          [connected]="!!host().profile"
          [canCompile]="host().can('compile')"
          [canPublish]="host().can('definition.publish')"
          [context]="builder.context"
          [startTab]="builder.tab"
          [readonly]="host().model.readonly"
          [publishedVersions]="actionRefs()"
          (useInStep)="use($event)"
          (insertIntoWorkflow)="insert($event)"
          (published)="published($event.uses)"
          (closed)="close('builder')"
        >
          @if (host().profile) {
            <weave-integration-readiness
              readiness
              [compact]="true"
              [api]="host().api"
              [identity]="host().identity"
            />
          }
        </weave-http-action-builder>
      }
    }
    @if (host().connectionDialog; as dialog) {
      @if (host().profile) {
        <weave-modal
          [heading]="
            dialog.kind === 'ai' ? 'New AI connection' : 'New API connection'
          "
          [wide]="true"
          (dismiss)="close('connection')"
        >
          @if (dialog.kind === "ai") {
            @defer (on immediate) {
              <weave-ai-provider-connection-form
                [api]="host().api"
                [canManage]="host().can('connection.manage')"
                (created)="created()"
                (finished)="close('connection')"
              />
            }
          } @else {
            @defer (on immediate) {
              <weave-http-connection-form
                [api]="host().api"
                [identity]="host().identity"
                heading=""
                [workspace]="host().workspaceText"
                [fromWorkflow]="!!dialog.fromWorkflow"
                [fromBuilder]="dialog.fromBuilder ?? null"
                (created)="created()"
                (cancel)="close('connection')"
                (finished)="close('connection')"
                (back)="backToWorkflow()"
              />
            } @placeholder {
              <p class="dialog-status" role="status">Loading…</p>
            }
          }
        </weave-modal>
      }
    }
    @if (host().showTemplates) {
      <weave-modal
        heading="New workflow from a template"
        [wide]="true"
        (dismiss)="close('templates')"
      >
        @defer (on immediate) {
          <weave-template-gallery
            heading="Templates"
            [local]="!host().profile"
            [showBlank]="true"
            (choose)="template($event)"
            (blank)="blank()"
          />
        } @placeholder {
          <p class="dialog-status" role="status">Loading…</p>
        }
      </weave-modal>
    }`,
})
export class IntegrationDialogs {
  /** The editor shell. */
  host = input.required<EditorHost>();
  private refs: { source: unknown; refs: string[] } = {
    source: null,
    refs: [],
  };

  /** Published name@version references, so the builder suggests a free version. */
  actionRefs() {
    const rows = this.host().actionVersions;
    if (this.refs.source !== rows)
      this.refs = {
        source: rows,
        refs: rows.map((row) => `${row["name"]}@${row["version"]}`),
      };
    return this.refs.refs;
  }
  close(dialog: "builder" | "connection" | "templates") {
    const host = this.host();
    if (dialog === "builder") host.apiBuilder = null;
    else if (dialog === "connection") host.connectionDialog = null;
    else host.showTemplates = false;
    host.refreshView();
  }
  use(action: HttpActionUse) {
    useApiAction(this.host(), action);
  }
  insert(action: HttpActionUse) {
    insertApiAction(this.host(), action);
  }
  /** A new action version: drop its cached contract and list the catalog again. */
  published(uses: string) {
    const host = this.host();
    host.cacheContract(uses, null);
    void host.loadActionCatalog(false, true);
  }
  created() {
    const host = this.host();
    // The dialog shows the success itself; the list behind it catches up.
    if (host.view === "connections") void host.refresh();
    host.refreshView();
  }
  /** "Back to the workflow": close the dialog and return to the inspector. */
  backToWorkflow() {
    const host = this.host();
    host.connectionDialog = null;
    host.showInspector = true;
    host.focusInspector();
    host.refreshView();
  }
  template(choice: TemplateChoice) {
    startFromTemplate(this.host(), choice);
  }
  blank() {
    const host = this.host();
    host.showTemplates = false;
    host.newWorkflow();
    host.refreshView();
  }
}
