# Copyright 2026 Firefly Software Foundation.
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
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Operator-controlled bounds and the published user-schema keyword profile."""

from dataclasses import dataclass

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SUPPORTED_FORMATS = frozenset({"date-time", "uuid", "uri", "email"})
REJECTED_KEYWORDS = frozenset({"$dynamicRef", "$dynamicAnchor", "unevaluatedItems", "unevaluatedProperties"})
SCHEMA_MAPS = frozenset({"$defs", "properties", "patternProperties", "dependentSchemas"})
SCHEMA_ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
SCHEMA_SINGLE = frozenset({"items", "additionalProperties", "contains", "propertyNames", "not", "if", "then", "else"})
SUPPORTED_KEYWORDS = (
    SCHEMA_MAPS
    | SCHEMA_ARRAYS
    | SCHEMA_SINGLE
    | frozenset(
        {
            "$schema",
            "$id",
            "$ref",
            "$comment",
            "type",
            "enum",
            "const",
            "format",
            "pattern",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minContains",
            "maxContains",
            "required",
            "minProperties",
            "maxProperties",
            "dependentRequired",
            "title",
            "description",
            "default",
            "examples",
            "deprecated",
            "readOnly",
            "writeOnly",
            "x-secret",
        }
    )
)


@dataclass(frozen=True)
class SchemaLimits:
    max_schema_bytes: int = 1_048_576
    max_schema_nodes: int = 10_000
    max_schema_depth: int = 128
    max_expansion_nodes: int = 100_000
    max_ref_depth: int = 64
    max_validation_work: int = 100_000
    max_payload_bytes: int = 1_048_576
    max_document_nodes: int = 100_000
    max_depth: int = 32
    max_diagnostics: int = 100
    max_pattern_length: int = 1024
    regex_timeout_seconds: float = 0.01
    max_regex_seconds: float = 0.1

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if name.endswith("seconds"):
                if type(value) not in {int, float} or not 0 < value < float("inf"):
                    raise ValueError(f"{name} must be positive and finite")
            elif type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


# Generated recursive unions require more work than bounded author-authored schemas.
DEFAULT_CONTRACT_LIMITS = SchemaLimits(max_validation_work=5_000_000)
