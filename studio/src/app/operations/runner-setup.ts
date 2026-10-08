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
import { Component, input, output } from "@angular/core";
import type { Target } from "./deployment-contracts";
import { exportFile } from "../export-file";
import { runnerSetup } from "./deployment-onboarding";

@Component({
  selector: "weave-runner-setup",
  standalone: true,
  template: `
    <section aria-labelledby="runner-setup-heading">
      <h2 id="runner-setup-heading">Connect the outbound runner</h2>
      <p>
        Connect an existing destination. New clusters and networks need separate
        infrastructure setup.
      </p>
      <ol class="setup-steps">
        <li>
          <h3>1. Authorize the application</h3>
          <p>
            Link its verified machine identity. Grant
            <code>deployment_runner</code> only on this target.
          </p>
          <details>
            <summary>Target ID for the grant</summary>
            <code>{{ target().id }}</code>
          </details>
          <button type="button" (click)="administration.emit()">
            Open settings — People and access
          </button>
        </li>
        <li>
          <h3>2. Prepare the runner host</h3>
          <p>
            Install the platform's Weave version with its client extra and the
            provider CLI. Complete local policy and credential-file references.
          </p>
          <button type="button" class="primary" (click)="download()">
            Download setup template
          </button>
          <p class="hint">
            Known workspace and target IDs included. No credentials.
          </p>
        </li>
        <li>
          <h3>3. Check and connect</h3>
          <p>
            {{
              registered()
                ? "Runner: recent contact"
                : "Runner: waiting for registration"
            }}
          </p>
          <p>
            {{
              observed()
                ? "Observation: current and complete"
                : "Observation: not yet verified"
            }}
          </p>
          <p class="hint">
            Check locally, start the runner, then select Refresh on Clusters and
            observe the target. Contact alone does not prove health.
          </p>
        </li>
      </ol>
      @if (notice) {
        <p role="status">{{ notice }}</p>
      }
      <details>
        <summary>Local setup template</summary>
        <p>
          Save this nonsecret template as /opt/weave/private/runner.json on the
          runner host. Set private file permissions. Replace local paths and
          context; add exact component names, roles, configuration aliases,
          container names (Kubernetes/ACA), ceilings and allowed repositories.
          Create the referenced private OAuth file locally. Never paste
          credentials into Studio.
        </p>
        <pre aria-label="Nonsecret runner configuration">{{
          configuration
        }}</pre>
        <p>
          Or use the interactive local setup wizard with the workspace and
          target IDs above:
        </p>
        <pre>
weave operations runner setup --output /opt/weave/private/runner.json</pre
        >
        <p>Check provider access without applying a job:</p>
        <pre>
weave operations runner check --config /opt/weave/private/runner.json</pre
        >
        <p>
          After a successful check, supervise outgoing polling on that host:
        </p>
        <pre>
weave operations runner run --config /opt/weave/private/runner.json</pre
        >
      </details>
    </section>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-width: 100%;
      padding: 12px;
      background: var(--sunken);
    }
    code {
      overflow-wrap: anywhere;
    }
    .setup-steps {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
      gap: 12px;
      list-style: none;
      padding: 0;
    }
    .setup-steps li {
      min-width: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    h3 {
      margin: 0 0 10px;
      font-size: 14px;
    }
    p {
      overflow-wrap: anywhere;
    }
    .hint {
      font-size: 13px;
      color: var(--muted);
    }
    button {
      max-width: 100%;
      white-space: normal;
    }
    .setup-steps details {
      margin-bottom: 10px;
    }
    summary {
      cursor: pointer;
      font-weight: 600;
    }
  `,
})
export class RunnerSetup {
  target = input.required<Target>();
  baseUrl = input.required<string>();
  registered = input(false);
  observed = input(false);
  administration = output<void>();
  notice = "";
  download() {
    const delivery = exportFile(
      "weave-runner-setup.json",
      "application/json",
      this.configuration + "\n",
    );
    this.notice =
      delivery === "downloaded"
        ? "Setup template download started. Complete the local policy before checking the runner."
        : "Download could not start. Expand Local setup template and copy the configuration.";
  }
  get configuration() {
    return JSON.stringify(runnerSetup(this.target(), this.baseUrl()), null, 2);
  }
}
