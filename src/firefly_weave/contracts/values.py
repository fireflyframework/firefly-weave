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

"""The interoperable JSON value domain shared by all wire contracts."""

import math
from typing import Annotated

from pydantic import BeforeValidator, Field

MAX_SAFE_INTEGER = 9_007_199_254_740_991


def validate_unicode(value: object) -> object:
    if isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("JSON strings must contain Unicode scalar values")
    return value


type UnicodeString = Annotated[str, BeforeValidator(validate_unicode)]
type SafeInteger = Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
type FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
type JsonValue = (
    None | bool | SafeInteger | FiniteFloat | UnicodeString | list[JsonValue] | dict[UnicodeString, JsonValue]
)
type JsonObject = dict[str, JsonValue]


def validate_json_value(value: object) -> object:
    """Reject Python-only values before a union can reinterpret an unsafe integer."""
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("JSON integers must be within the interoperable safe range")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
    elif type(value) is str:
        validate_unicode(value)
    elif type(value) is list:
        for item in value:
            validate_json_value(item)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            validate_unicode(key)
            validate_json_value(item)
    else:
        raise ValueError("Value is outside the JSON domain")
    return value


type JsonData = Annotated[JsonValue, BeforeValidator(validate_json_value)]
type JsonObjectData = Annotated[JsonObject, BeforeValidator(validate_json_value)]
