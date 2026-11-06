"""Zero-dependency recursive JSON Schema validator for API response validation."""

from __future__ import annotations

import re
from typing import Any


def _matches_type(instance: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return instance is None
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected_type == "string":
        return isinstance(instance, str)
    if expected_type == "array":
        return isinstance(instance, list)
    if expected_type == "object":
        return isinstance(instance, dict)
    raise ValueError(f"Unknown JSON Schema type: {expected_type!r}")


def validate_json_schema(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    """Validate `instance` against `schema` and return a list of validation error strings."""
    errors: list[str] = []

    if not isinstance(schema, dict):
        return [f"{path}: schema itself must be a dict, got {type(schema).__name__}"]

    # 1. Combinators: anyOf, oneOf, allOf
    if "anyOf" in schema:
        subschemas = schema["anyOf"]
        if not any(len(validate_json_schema(instance, sub, path)) == 0 for sub in subschemas):
            errors.append(f"{path}: value {instance!r} did not match anyOf schemas")
            return errors

    if "oneOf" in schema:
        subschemas = schema["oneOf"]
        matches = sum(1 for sub in subschemas if len(validate_json_schema(instance, sub, path)) == 0)
        if matches != 1:
            errors.append(f"{path}: value {instance!r} matched {matches} schemas in oneOf (expected 1)")
            return errors

    if "allOf" in schema:
        for sub in schema["allOf"]:
            errors.extend(validate_json_schema(instance, sub, path))

    # 2. Type check (supports nullable = true convenience flag as well as type = ["string", "null"])
    if schema.get("nullable") is True and instance is None:
        return errors

    if "type" in schema:
        expected_types = schema["type"]
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(_matches_type(instance, t) for t in expected_types):
            actual_type = "null" if instance is None else type(instance).__name__
            errors.append(
                f"{path}: expected type {expected_types}, got {actual_type} ({instance!r})"
            )
            return errors

    # 3. const & enum
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}, got {instance!r}")

    # 4. Numeric constraints
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} is less than minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} is greater than maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(
                f"{path}: {instance} is not greater than exclusiveMinimum {schema['exclusiveMinimum']}"
            )
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(
                f"{path}: {instance} is not less than exclusiveMaximum {schema['exclusiveMaximum']}"
            )

    # 5. String constraints
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(
                f"{path}: string length {len(instance)} < minLength {schema['minLength']}"
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(
                f"{path}: string length {len(instance)} > maxLength {schema['maxLength']}"
            )
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(
                f"{path}: string {instance!r} does not match pattern {schema['pattern']!r}"
            )

    # 6. Array constraints
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: array length {len(instance)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: array length {len(instance)} > maxItems {schema['maxItems']}")
        if "items" in schema and isinstance(schema["items"], dict):
            item_schema = schema["items"]
            for idx, item in enumerate(instance):
                errors.extend(validate_json_schema(item, item_schema, f"{path}[{idx}]"))

    # 7. Object constraints
    if isinstance(instance, dict):
        required_keys = schema.get("required", [])
        for req_key in required_keys:
            if req_key not in instance:
                errors.append(f"{path}: missing required property {req_key!r}")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for prop_name, prop_schema in properties.items():
                if prop_name in instance:
                    errors.extend(
                        validate_json_schema(
                            instance[prop_name], prop_schema, f"{path}.{prop_name}"
                        )
                    )

        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            extra = set(instance.keys()) - set(properties.keys())
            if extra:
                errors.append(
                    f"{path}: unexpected additional properties {sorted(extra)!r}"
                )

    return errors
