# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Write one offline reviewed HTTP scaffold; never build, install, publish or send."""

import argparse
from pathlib import Path

from firefly_weave.sdk.connectors import scaffold_import
from firefly_weave.sdk.openapi_import import import_openapi


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    document = {
        "openapi": "3.1.1",
        "info": {"title": "Inventory example", "version": "1"},
        "servers": [{"url": "https://inventory.example.test/v1"}],
        "paths": {
            "/items/{id}": {
                "get": {
                    "operationId": "getItem",
                    "parameters": [
                        {"in": "path", "name": "id", "required": True, "schema": {"type": "string", "maxLength": 64}}
                    ],
                    "responses": {
                        "200": {
                            "description": "Inventory item",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                        "required": ["name"],
                                        "additionalProperties": False,
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    policy = {
        "name": "inventory-http",
        "version": "1.0.0",
        "auth": {"kind": "none"},
        "operations": {
            "getItem": {
                "name": "get-item",
                "sideEffect": "read_only",
                "server": "https://inventory.example.test/v1",
                "statuses": [200],
            }
        },
    }
    result = import_openapi(document, ["getItem"], policy)
    if not result.ok:
        raise SystemExit(", ".join(d.code for d in result.diagnostics))
    scaffold_import(args.target, result)
    print(f"Review the generated package at {args.target}. No request was sent.")


if __name__ == "__main__":
    main()
