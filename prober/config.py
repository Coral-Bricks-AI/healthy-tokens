"""TOML Scan Configuration loader with secure relative imports and ${var} interpolation."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prober.checkers import BaseChecker, create_checker
from prober.fetchers import BaseFetcher, create_fetcher


class ConfigError(Exception):
    """Raised when a scan configuration file is invalid or cannot be resolved."""


class ConfigSecurityError(ConfigError):
    """Raised when an import attempts to escape the entry configuration directory."""


_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:-(?P<alt>[^}]*)|:-(?P<default>[^}]*))?\}")


@dataclass(frozen=True)
class ProbeDefinition:
    """Fully resolved executable Probe definition."""

    name: str
    tags: list[str]
    fetcher: BaseFetcher
    checkers: list[BaseChecker]
    source_file: str


@dataclass(frozen=True)
class LoadedScanConfig:
    """Result of loading and resolving an entry scan-config and all its imports."""

    entry_path: Path
    variables: dict[str, Any]
    checkers: dict[str, BaseChecker]
    probes: list[ProbeDefinition]
    imported_files: list[Path] = field(default_factory=list)


def _validate_and_resolve_import(
    root_dir: Path,
    current_file: Path,
    raw_import: str,
) -> Path:
    """Resolve `raw_import` relative to `current_file.parent` while enforcing confinement to `root_dir`."""
    if not isinstance(raw_import, str) or not raw_import.strip():
        raise ConfigError(f"Empty or non-string import in {current_file}")

    cleaned = raw_import.strip()
    import_path = Path(cleaned)

    # Disallow absolute paths or Windows drive/root prefixes
    if import_path.is_absolute() or import_path.drive or cleaned.startswith(("/", "\\")):
        raise ConfigSecurityError(
            f"Security violation in {current_file}: absolute import path {raw_import!r} is forbidden. "
            f"Imports must be relative and stay within {root_dir}."
        )

    candidate = (current_file.parent / import_path).resolve()

    if not candidate.is_relative_to(root_dir):
        raise ConfigSecurityError(
            f"Security violation in {current_file}: import {raw_import!r} resolves to {candidate}, "
            f"which escapes root config directory {root_dir}."
        )

    if not candidate.exists() or not candidate.is_file():
        raise ConfigError(
            f"Imported scan config {raw_import!r} (resolved to {candidate}) from {current_file} does not exist."
        )

    return candidate


def _lookup_variable(
    name: str,
    default: str | None,
    variables: dict[str, Any],
    cli_vars: dict[str, str],
) -> str:
    # 1. CLI explicit override
    if name in cli_vars:
        return str(cli_vars[name])

    # 2. Config variables table (if non-empty string, or if no env fallback exists)
    cfg_val = variables.get(name)
    if cfg_val is not None and str(cfg_val) != "":
        return str(cfg_val)

    # 3. Environment variable (exact name or UPPERCASE)
    env_val = os.environ.get(name)
    if env_val is None:
        env_val = os.environ.get(name.upper())
    if env_val is not None and env_val != "":
        return env_val

    # 4. Inline default from ${var:-default}
    if default is not None:
        return default

    # 5. If explicitly defined in [variables] as empty string ""
    if name in variables:
        return str(variables[name])

    raise ConfigError(
        f"Unresolved variable '${{{name}}}': not found in CLI --var, [variables], or environment."
    )


def interpolate_value(
    value: Any,
    variables: dict[str, Any],
    cli_vars: dict[str, str],
) -> Any:
    """Recursively interpolate ${var} and ${var:-default} placeholders in strings, lists, and dicts."""
    if isinstance(value, str):
        def _replacer(match: re.Match[str]) -> str:
            var_name = match.group("name")
            default = match.group("default")
            if default is None:
                default = match.group("alt")
            return _lookup_variable(var_name, default, variables, cli_vars)

        # Perform up to 5 passes to resolve nested variable references (e.g. url = "${base_url}/models")
        current = value
        for _ in range(5):
            if "${" not in current:
                break
            next_val = _VAR_PATTERN.sub(_replacer, current)
            if next_val == current:
                break
            current = next_val
        return current

    if isinstance(value, list):
        return [interpolate_value(item, variables, cli_vars) for item in value]

    if isinstance(value, dict):
        return {k: interpolate_value(v, variables, cli_vars) for k, v in value.items()}

    return value


def _extract_raw_checkers(doc: dict[str, Any], file_path: Path) -> list[dict[str, Any]]:
    """Support both `[[checkers]]` (array of tables) and `[checkers.name]` (map of tables) in TOML."""
    raw = doc.get("checkers", doc.get("checker"))
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        result: list[dict[str, Any]] = []
        for name, body in raw.items():
            if not isinstance(body, dict):
                raise ConfigError(f"Invalid checker definition {name!r} in {file_path}")
            merged = dict(body)
            merged.setdefault("name", name)
            result.append(merged)
        return result
    raise ConfigError(f"Invalid 'checkers' section in {file_path}: expected array or table")


def load_scan_config(
    entry_config_path: str | Path,
    cli_vars: dict[str, str] | None = None,
) -> LoadedScanConfig:
    """Load an entry TOML scan configuration file, recursively resolving imports, variables, checkers, and probes."""
    cli_overrides = dict(cli_vars or {})
    entry_path = Path(entry_config_path).resolve()
    if not entry_path.exists() or not entry_path.is_file():
        raise ConfigError(f"Entry scan config file not found: {entry_config_path}")

    root_dir = entry_path.parent.resolve()

    visited_files: list[Path] = []
    visited_set: set[Path] = set()
    active_stack: list[Path] = []

    raw_variables: dict[str, Any] = {}
    raw_checkers: list[tuple[Path, dict[str, Any]]] = []
    raw_probes: list[tuple[Path, dict[str, Any]]] = []

    def _visit(file_path: Path) -> None:
        if file_path in active_stack:
            cycle = " -> ".join(str(p.name) for p in [*active_stack, file_path])
            raise ConfigError(f"Circular import detected in scan configs: {cycle}")
        if file_path in visited_set:
            return

        active_stack.append(file_path)
        try:
            with file_path.open("rb") as f:
                doc = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"TOML syntax error in {file_path}: {exc}") from exc

        # 1. Process imports first (depth-first) so parent files can override variables/checkers
        imports = doc.get("imports", doc.get("Imports", []))
        if not isinstance(imports, list):
            raise ConfigError(f"'imports' in {file_path} must be an array of relative file paths")

        for rel_import in imports:
            target = _validate_and_resolve_import(root_dir, file_path, rel_import)
            _visit(target)

        # 2. Collect variables
        vars_sec = doc.get("variables", doc.get("Variables", {}))
        if not isinstance(vars_sec, dict):
            raise ConfigError(f"'variables' in {file_path} must be a table/dict")
        raw_variables.update(vars_sec)

        # 3. Collect checkers
        for chk_dict in _extract_raw_checkers(doc, file_path):
            raw_checkers.append((file_path, chk_dict))

        # 4. Collect probes
        probes_sec = doc.get("probes", doc.get("Probes", []))
        if not isinstance(probes_sec, list):
            raise ConfigError(f"'probes' in {file_path} must be an array of tables ([[probes]])")
        for prb_dict in probes_sec:
            if not isinstance(prb_dict, dict):
                raise ConfigError(f"Probe entry in {file_path} must be a table")
            raw_probes.append((file_path, prb_dict))

        active_stack.pop()
        visited_set.add(file_path)
        visited_files.append(file_path)

    _visit(entry_path)

    # Resolve variable values (allowing variables to reference earlier variables or env vars)
    resolved_variables: dict[str, Any] = dict(raw_variables)
    resolved_variables.update(cli_overrides)
    for k, v in list(resolved_variables.items()):
        resolved_variables[k] = interpolate_value(v, resolved_variables, cli_overrides)

    # Instantiate named checkers
    checkers_map: dict[str, BaseChecker] = {}
    for src_file, raw_chk in raw_checkers:
        interpolated_chk = interpolate_value(raw_chk, resolved_variables, cli_overrides)
        try:
            checker_obj = create_checker(interpolated_chk)
        except Exception as exc:
            raise ConfigError(f"Invalid checker in {src_file}: {exc}") from exc
        checkers_map[checker_obj.name] = checker_obj

    # Instantiate probes
    probes_list: list[ProbeDefinition] = []
    for src_file, raw_prb in raw_probes:
        interpolated_prb = interpolate_value(raw_prb, resolved_variables, cli_overrides)
        probe_name = str(interpolated_prb.get("name", "")).strip()
        if not probe_name:
            raise ConfigError(f"A probe in {src_file} is missing a 'name'")

        raw_tags = interpolated_prb.get("tags", [])
        if not isinstance(raw_tags, list):
            raise ConfigError(f"Probe {probe_name!r} in {src_file}: 'tags' must be an array")
        normalized_tags = [str(t).strip().lower() for t in raw_tags if str(t).strip()]

        raw_fetcher = interpolated_prb.get("fetcher")
        if not isinstance(raw_fetcher, dict):
            raise ConfigError(
                f"Probe {probe_name!r} in {src_file} must specify a 'fetcher' table"
            )
        try:
            fetcher_obj = create_fetcher(raw_fetcher)
        except Exception as exc:
            raise ConfigError(
                f"Probe {probe_name!r} in {src_file} has invalid fetcher: {exc}"
            ) from exc

        raw_probe_checkers = interpolated_prb.get("checkers", [])
        if not isinstance(raw_probe_checkers, list) or not raw_probe_checkers:
            raise ConfigError(
                f"Probe {probe_name!r} in {src_file} must specify a non-empty 'checkers' list"
            )

        probe_checkers: list[BaseChecker] = []
        for idx, chk_ref in enumerate(raw_probe_checkers):
            if isinstance(chk_ref, str):
                ref_name = chk_ref.strip()
                if ref_name not in checkers_map:
                    raise ConfigError(
                        f"Probe {probe_name!r} in {src_file} references unknown checker {ref_name!r}. "
                        f"Available checkers: {sorted(checkers_map.keys())}"
                    )
                probe_checkers.append(checkers_map[ref_name])
            elif isinstance(chk_ref, dict):
                default_inline_name = f"{probe_name}#checker_{idx + 1}"
                try:
                    inline_chk = create_checker(chk_ref, default_name=default_inline_name)
                except Exception as exc:
                    raise ConfigError(
                        f"Probe {probe_name!r} in {src_file} has invalid inline checker: {exc}"
                    ) from exc
                probe_checkers.append(inline_chk)
            else:
                raise ConfigError(
                    f"Probe {probe_name!r} in {src_file}: checker entry must be a name string or inline table"
                )

        probes_list.append(
            ProbeDefinition(
                name=probe_name,
                tags=normalized_tags,
                fetcher=fetcher_obj,
                checkers=probe_checkers,
                source_file=str(src_file),
            )
        )

    return LoadedScanConfig(
        entry_path=entry_path,
        variables=resolved_variables,
        checkers=checkers_map,
        probes=probes_list,
        imported_files=visited_files,
    )
