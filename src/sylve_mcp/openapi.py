"""Swagger 2.0 loader, $ref resolver, search, and operation schema inspection.

``docs/swagger.json`` (vendored from the Sylve project) documents 336 paths
and 441 operations. This module backs the three universal MCP tools
(``sylve_api_search``, ``sylve_api_schema``, ``sylve_api_call``) and the
optional ``--expose-tags`` / ``--expose-all`` dynamic tool generator.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Known looong Go package-path definitions keys, kept short in rendered output.
_REF_ALIASES = {
    "github_com_alchemillahq_sylve_internal_interfaces_services_network": "network",
    "github_com_alchemillahq_sylve_internal_interfaces_services_cluster": "cluster",
    "github_com_alchemillahq_sylve_internal_interfaces_services_zfs": "zfs",
    "github_com_alchemillahq_sylve_internal_interfaces_services_libvirt": "libvirt",
    "github_com_alchemillahq_sylve_internal_interfaces_services_jail": "jail",
    "github_com_alchemillahq_sylve_internal_services_zfs": "zfs",
    "github_com_alchemillahq_sylve_internal_db_models_jail": "jail",
    "github_com_alchemillahq_sylve_pkg_network_bridgevlan": "bridgevlan",
    "internal_handlers_zfs": "zfs",
    "internal_handlers_cluster": "cluster",
    "internal_handlers_network": "network",
    "internal_handlers_jail": "jail",
    "internal_handlers_vm": "vm",
    "internal_handlers_dropbox": "dropbox",
    "internal_handlers_info": "info",
    "internal_handlers_disk": "disk",
    "internal_handlers_utility": "utility",
    "internal_handlers_auth": "auth",
    "internal_handlers_system": "system",
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def default_spec_path() -> Path:
    """Return the bundled OpenAPI spec (package data, then repo ``docs/``)."""
    here = Path(__file__).resolve().parent
    pkg = here / "swagger.json"
    if pkg.exists():
        return pkg
    repo = here.parents[1] / "docs" / "swagger.json"
    if repo.exists():
        return repo
    raise RuntimeError("swagger.json not found; reinstall the package or pass --swagger.")


def slugify_path(path_template: str) -> str:
    """Turn ``/vm/{rid}/actions/{action}`` into ``vm_rid_actions_action``."""
    parts: list[str] = []
    for segment in path_template.strip("/").split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            parts.append(segment[1:-1])
        else:
            parts.append(re.sub(r"[^a-zA-Z0-9]+", "_", segment).strip("_"))
    return "_".join(part for part in parts if part)


def shorten_def_name(name: str) -> str:
    """Shorten long Go definition names for readable tool output."""
    for prefix, alias in _REF_ALIASES.items():
        if name.startswith(prefix):
            rest = name[len(prefix) :].lstrip(".")
            return f"{alias}.{rest}" if rest else alias
    return name


class OpenAPI:
    """Load a Swagger 2.0 document and query its operations."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_spec_path()
        self.spec = json.loads(self.path.read_text(encoding="utf-8"))
        self.paths: dict[str, dict[str, Any]] = self.spec.get("paths", {})
        self.definitions: dict[str, Any] = self.spec.get("definitions", {})

    def operations(self) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Yield ``(method, path_template, operation)`` for every operation."""
        for template, methods in self.paths.items():
            for method in HTTP_METHODS:
                op = methods.get(method)
                if isinstance(op, dict):
                    yield method.upper(), template, op

    def operation_count(self) -> int:
        return sum(1 for _ in self.operations())

    def find(
        self,
        query: str | None = None,
        tag: str | None = None,
        method: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search operations by free-text query, Swagger tag, and/or HTTP method."""
        results: list[dict[str, Any]] = []
        q = query.strip().lower() if query else ""
        t = tag.strip().lower() if tag else ""
        m = method.upper() if method else ""
        for op_method, template, op in self.operations():
            if m and op_method != m:
                continue
            if t and t not in {x.lower() for x in op.get("tags", [])}:
                continue
            if q:
                haystack = f"{op_method} {template} {op.get('summary', '')} {op.get('description', '')} {' '.join(op.get('tags', []))}".lower()
                if q not in haystack:
                    continue
            results.append(self.summary(op_method, template, op))
            if len(results) >= max(1, limit):
                break
        return results

    def plan(self, params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """Resolve ``(method, path_template, op)`` from an operation-stable descriptor."""
        method = str(params.get("method") or "").upper()
        path = str(params.get("path") or "").strip()
        if method.lower() not in HTTP_METHODS:
            raise ValueError(f"method must be one of {', '.join(m.upper() for m in HTTP_METHODS)}.")
        # Accept either the concrete path (with IDs substituted) or a template.
        for template in self.paths:
            if template == path:
                try:
                    return method, template, self._require_op(template, method)
                except ValueError:
                    continue
            pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
            if re.fullmatch(pattern, path):
                try:
                    return method, template, self._require_op(template, method)
                except ValueError:
                    continue
        raise ValueError(f"path '{path}' is not documented in the bundled swagger.json.")

    def _require_op(self, template: str, method: str) -> dict[str, Any]:
        op = self.paths.get(template, {}).get(method.lower())
        if not isinstance(op, dict):
            raise ValueError(f"{method.upper()} {template} is not documented.")
        return op

    def summary(self, method: str, template: str, op: dict[str, Any]) -> dict[str, Any]:
        """A compact, model-friendly description of one operation."""
        return {
            "method": method,
            "path": template,
            "name": f"{method.lower()}_{slugify_path(template)}",
            "summary": op.get("summary", ""),
            "description": op.get("description", "") or op.get("summary", ""),
            "tags": op.get("tags", []),
            "parameters": [
                {
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": bool(p.get("required")),
                    "type": describe_schema(p.get("schema") or {"type": p.get("type")}),
                }
                for p in op.get("parameters", [])
                if "$ref" not in p
            ],
        }

    def parameters(self, op: dict[str, Any]) -> dict[str, Any]:
        """Split an operation's parameters into ``path``, ``query``, and ``body``."""
        path_params: list[dict[str, Any]] = []
        query_params: list[dict[str, Any]] = []
        body: dict[str, Any] | None = None
        for p in op.get("parameters", []):
            if "$ref" in p:
                name = p["$ref"].split("/")[-1]
                p = self.definitions.get(name, p)
            where = p.get("in")
            if where == "path":
                path_params.append(
                    {
                        "name": p.get("name"),
                        "required": bool(p.get("required")),
                        "type": p.get("type") or "string",
                        "description": p.get("description", ""),
                    }
                )
            elif where == "query":
                query_params.append(
                    {
                        "name": p.get("name"),
                        "required": bool(p.get("required")),
                        "type": p.get("type") or "string",
                        "description": p.get("description", ""),
                    }
                )
            elif where == "body":
                body = self.resolve_schema(p.get("schema") or {})
        return {"path": path_params, "query": query_params, "body": body}

    def resolve_ref(self, ref: str) -> dict[str, Any]:
        if not ref.startswith("#/definitions/"):
            raise ValueError(f"Unsupported schema reference: {ref}")
        name = ref[len("#/definitions/") :]
        resolved = self.definitions.get(name)
        if not isinstance(resolved, dict):
            raise ValueError(f"Unknown schema definition: {name}")
        return resolved

    def resolve_schema(
        self, schema: dict[str, Any], _seen: set[str] | None = None
    ) -> dict[str, Any]:
        """Return a JSON-serializable copy of *schema* with $refs expanded."""
        seen = _seen if _seen is not None else set()
        if "$ref" in schema:
            name = schema["$ref"].split("/")[-1]
            if name in seen:
                return {"$ref": shorten_def_name(name)}
            return self.resolve_schema(self.resolve_ref(schema["$ref"]), seen | {name})  # type: ignore[arg-type]
        if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
            out = dict(schema)
            out["items"] = self.resolve_schema(schema["items"], seen)
            return out
        if schema.get("type") == "object":
            out = dict(schema)
            props = out.get("properties")
            if isinstance(props, dict):
                out["properties"] = {
                    key: self.resolve_schema(value, seen) for key, value in props.items()
                }
            return out
        if isinstance(schema.get("additionalProperties"), dict):
            out = dict(schema)
            out["additionalProperties"] = self.resolve_schema(schema["additionalProperties"], seen)
            return out
        return dict(schema)

    def operation_schema(self, method: str, template: str) -> dict[str, Any]:
        """Full resolved reference for one operation (for ``sylve_api_schema``)."""
        op = self._require_op(template, method)
        params = self.parameters(op)
        responses: dict[str, Any] = {}
        for code, resp in (op.get("responses") or {}).items():
            if not isinstance(resp, dict):
                continue
            entry: dict[str, Any] = {"description": resp.get("description", "")}
            if isinstance(resp.get("schema"), dict):
                entry["schema"] = self.resolve_schema(resp["schema"])
            responses[code] = entry
        return {
            "method": method,
            "path": template,
            "summary": op.get("summary", ""),
            "description": op.get("description", "") or op.get("summary", ""),
            "tags": op.get("tags", []),
            "path_parameters": params["path"],
            "query_parameters": params["query"],
            "request_body": params["body"],
            "responses": responses,
        }

    def tags_summary(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for _m, _p, op in self.operations():
            for tag in op.get("tags", ["untagged"]):
                counts[tag] = counts.get(tag, 0) + 1
        return [
            {"tag": tag, "operations": count}
            for tag, count in sorted(counts.items(), key=lambda item: -item[1])
        ]

    def dynamic_tool_specs(self, tags: set[str] | None = None) -> list[dict[str, Any]]:
        """Build tool descriptors for every operation, optionally filtered by tag."""
        specs: list[dict[str, Any]] = []
        wanted = {t.strip().lower() for t in tags} if tags else None
        for method, template, op in self.operations():
            op_tags = {t.lower() for t in op.get("tags", [])}
            if wanted and not op_tags & wanted:
                continue
            params = self.parameters(op)
            path_props: dict[str, Any] = {}
            path_required: list[str] = []
            for p in params["path"]:
                path_props[p["name"]] = {
                    "type": p["type"] or "string",
                    "description": p["description"],
                }
                if p["required"]:
                    path_required.append(p["name"])
            query_props: dict[str, Any] = {}
            for p in params["query"]:
                if p["name"] not in path_props:
                    query_props[p["name"]] = {
                        "type": p["type"] or "string",
                        "description": p["description"],
                    }
            input_schema: dict[str, Any] = {"type": "object", "properties": {}}
            if path_props:
                input_schema["properties"].update(path_props)
            if query_props:
                input_schema["properties"].update(query_props)
            if params["body"] is not None:
                input_schema["properties"]["body"] = {
                    "type": "object",
                    "description": (
                        f"Request body. Run sylve_api_schema with method={method} "
                        f"path={template} for the exact fields."
                    ),
                }
            input_schema["properties"]["node"] = {
                "type": "string",
                "description": "Target cluster node hostname or UUID.",
            }
            input_schema["properties"]["context"] = {
                "type": "string",
                "description": "Named login context from config.yaml.",
            }
            input_schema["properties"]["wait"] = {
                "type": "boolean",
                "description": "Poll a returned taskId to completion when present.",
                "default": True,
            }
            input_schema["required"] = path_required[:1] or []
            specs.append(
                {
                    "name": f"api_{method.lower()}_{slugify_path(template)}",
                    "description": (
                        f"{op.get('summary', method + ' ' + template)}. "
                        f"Sylve API operation {method.upper()} {template}."
                    ),
                    "input_schema": input_schema,
                    "method": method,
                    "path": template,
                }
            )
        return specs


def describe_schema(schema: dict[str, Any]) -> str:
    """Short human-readable description of a Swagger schema."""
    if "$ref" in schema:
        return shorten_def_name(schema["$ref"].split("/")[-1])
    t = schema.get("type") or "object"
    if t == "array":
        items = schema.get("items") or {}
        return f"array[{describe_schema(items)}]"
    if schema.get("enum"):
        return f"enum{schema['enum']}"
    return t
