from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from . import FORMAT_VERSION, FORMAT_NAME, APP_NAME, APP_VERSION


class UnsupportedFormatError(RuntimeError):
    pass


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def migrate_document(raw: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(raw)

    if "format" not in data:
        data = migrate_0_1_to_0_2(data)

    version = str(data.get("format", {}).get("version", "0.0.0"))

    if version > FORMAT_VERSION:
        raise UnsupportedFormatError(
            f"Dateiformat {version} ist neuer als die unterstützte Version {FORMAT_VERSION}."
        )

    if version != FORMAT_VERSION:
        raise UnsupportedFormatError(
            f"Für das Dateiformat {version} ist noch keine Migration vorhanden."
        )

    # V2.0 ergänzt die einheitlichen Inhaltsfelder auch bei bestehenden
    # Projekten, ohne das Dateiformat inkompatibel zu ändern.
    for obj in data.get("objects", {}).values():
        if not isinstance(obj, dict):
            continue
        obj.setdefault("note", "")
        obj.setdefault("attachments", [])
        obj.setdefault("tags", [])
        obj.setdefault("status", "")
        obj.setdefault("node_type", "topic")

    # Freie grafische Annotationen sind map-spezifisch und optional.
    for map_data in data.get("maps", {}).values():
        if isinstance(map_data, dict):
            map_data.setdefault("drawings", {})

    data.setdefault("generator", {})["name"] = APP_NAME
    data["generator"]["version"] = APP_VERSION
    return data


def migrate_0_1_to_0_2(old: dict[str, Any]) -> dict[str, Any]:
    """Übernimmt einfache V0.1-Dateien mit einer Liste `nodes` und `connections`."""
    map_id = _id("map")
    objects: dict[str, Any] = {}
    object_states: dict[str, Any] = {}

    old_nodes = old.get("nodes", old.get("objects", []))
    if isinstance(old_nodes, dict):
        old_nodes = list(old_nodes.values())

    for node in old_nodes:
        object_id = str(node.get("id") or _id("object"))
        objects[object_id] = {
            "id": object_id,
            "type": node.get("type", "node"),
            "title": node.get("title", node.get("text", "Knoten")),
            "note": node.get("note", ""),
            "progress": node.get("progress", 0),
            "markers": node.get("markers", []),
            "tags": node.get("tags", []),
            "attachments": node.get("attachments", []),
            "status": node.get("status", ""),
            "node_type": node.get("node_type", "topic"),
        }
        object_states[object_id] = {
            "x": float(node.get("x", 0)),
            "y": float(node.get("y", 0)),
            "width": float(node.get("width", 180)),
            "height": float(node.get("height", 54)),
            "collapsed": bool(node.get("collapsed", False)),
            "visible": True,
        }

    relations: dict[str, Any] = {}
    old_relations = old.get("connections", old.get("relations", []))
    if isinstance(old_relations, dict):
        old_relations = list(old_relations.values())

    for relation in old_relations:
        relation_id = str(relation.get("id") or _id("relation"))
        relations[relation_id] = {
            "id": relation_id,
            "source_id": relation.get("source_id", relation.get("source")),
            "target_id": relation.get("target_id", relation.get("target")),
            "type": relation.get("type", "tree"),
            "label": relation.get("label", ""),
        }

    return {
        "format": {
            "name": FORMAT_NAME,
            "version": FORMAT_VERSION,
        },
        "generator": {
            "name": APP_NAME,
            "version": APP_VERSION,
        },
        "project": {
            "id": old.get("project_id", _id("project")),
            "title": old.get("title", "Importiertes Projekt"),
            "created": old.get("created", ""),
            "modified": old.get("modified", ""),
            "active_map_id": map_id,
        },
        "objects": objects,
        "relations": relations,
        "maps": {
            map_id: {
                "id": map_id,
                "name": "Hauptmap",
                "root_object_id": next(iter(objects), None),
                "object_states": object_states,
                "view": {
                    "zoom": 1.0,
                    "center_x": 0.0,
                    "center_y": 0.0,
                },
            }
        },
        "attachments": {},
    }
