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
// Component styles shared by the API action builder's parts. They only use
// the design tokens from styles.css, so the builder follows the app theme.
export const builderStyles = `
  :host {
    display: block;
    min-width: 0;
  }
  h3 {
    font-size: 14px;
    margin: 0 0 4px;
  }
  .hb-section {
    border-top: 1px solid var(--line);
    padding-top: 16px;
    margin-top: 16px;
  }
  .hb-section:first-child {
    border-top: 0;
    padding-top: 0;
    margin-top: 0;
  }
  .hb-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr));
    gap: 12px;
  }
  .hb-field {
    margin-top: 12px;
    min-width: 0;
  }
  .hb-field > label,
  .hb-legend {
    display: block;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 6px;
  }
  .hb-field > input:not([type="checkbox"]):not([type="radio"]),
  .hb-field > select,
  .hb-field > textarea {
    width: 100%;
  }
  .hb-field textarea {
    min-height: 110px;
    font-family: var(--font-mono);
    font-size: 12px;
  }
  fieldset {
    border: 0;
    margin: 0;
    padding: 0;
    min-width: 0;
  }
  .hb-help {
    font-size: 12px;
    color: var(--muted);
    margin: 4px 0 0;
    line-height: 1.45;
  }
  .hb-error {
    font-size: 12px;
    color: var(--danger-ink);
    margin: 4px 0 0;
    overflow-wrap: anywhere;
  }
  .hb-warning {
    font-size: 12px;
    color: var(--warning-ink);
    margin: 4px 0 0;
    overflow-wrap: anywhere;
  }
  .hb-info {
    font-size: 12px;
    color: var(--muted);
    margin: 4px 0 0;
    overflow-wrap: anywhere;
  }
  .hb-hint {
    display: block;
    color: var(--muted);
  }
  [aria-invalid="true"] {
    border-color: var(--danger);
    box-shadow: inset 3px 0 0 var(--danger);
  }
  .hb-inline {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 16px;
  }
  .hb-check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 500;
  }
  .hb-check input {
    width: 16px;
    height: 16px;
    margin: 0;
    accent-color: var(--accent);
  }
  .hb-effect {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    margin-top: 8px;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line);
    background: var(--raised);
    font-size: 12px;
    line-height: 1.45;
  }
  .hb-effect.write {
    background: var(--warning-bg);
    border-color: var(--warning-bd);
    color: var(--warning-ink);
  }
  .hb-effect weave-icon {
    width: 16px;
    height: 16px;
    --icon-stroke: 1.5;
    margin-top: 1px;
  }
  .hb-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    font-weight: 600;
    border-radius: var(--radius-pill);
    border: 1px solid var(--neutral-bd);
    padding: 1px 8px;
    background: var(--neutral-bg);
    color: var(--text);
    white-space: nowrap;
  }
  .hb-chip.write {
    background: var(--warning-bg);
    border-color: var(--warning-bd);
    color: var(--warning-ink);
  }
  .hb-chip.bad {
    background: var(--danger-bg);
    border-color: var(--danger-bd);
    color: var(--danger-ink);
  }
  .hb-method {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    color: var(--text);
  }
  .hb-rows {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
    display: grid;
    gap: 8px;
  }
  .hb-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    padding: 8px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    min-width: 0;
  }
  .hb-row.invalid {
    border-color: var(--danger-bd);
    background: var(--danger-bg);
  }
  .hb-row > .grow {
    flex: 1 1 160px;
    min-width: 0;
  }
  .hb-row select {
    width: auto;
    min-width: 96px;
  }
  .hb-row .hb-row-message {
    flex-basis: 100%;
    margin: 0;
  }
  .hb-row-tag {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    min-width: 52px;
  }
  .hb-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
    align-items: center;
  }
  .hb-status {
    font-size: 12px;
    margin: 12px 0 0;
    display: flex;
    gap: 8px;
    align-items: center;
    overflow-wrap: anywhere;
  }
  .hb-status weave-icon {
    width: 16px;
    height: 16px;
    --icon-stroke: 1.5;
    flex: none;
  }
  .hb-status.ok weave-icon {
    color: var(--success-ink);
  }
  .hb-status.bad {
    color: var(--danger-ink);
  }
  .hb-list {
    margin: 8px 0 0;
    padding-left: 18px;
    font-size: 12px;
  }
  .hb-list li {
    margin: 4px 0;
    overflow-wrap: anywhere;
  }
  .hb-code {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--muted);
  }
  button.hb-small {
    min-height: var(--control-sm);
    padding: 0 10px;
    font-size: 13px;
  }
  button.hb-remove {
    min-height: var(--control-sm);
    width: var(--control-sm);
    padding: 0;
    border-color: transparent;
    background: transparent;
  }
  button.hb-remove weave-icon {
    width: 16px;
    height: 16px;
    --icon-stroke: 1.5;
  }
`;
