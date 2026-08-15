from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from . import APP_NAME, APP_VERSION, FORMAT_NAME, FORMAT_VERSION


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ProjectModel:
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, title: str = "Unbenannt") -> "ProjectModel":
        now = iso_now()
        main_map_id = new_id("map")
        return cls({
            "format": {
                "name": FORMAT_NAME,
                "version": FORMAT_VERSION,
            },
            "generator": {
                "name": APP_NAME,
                "version": APP_VERSION,
            },
            "project": {
                "id": new_id("project"),
                "title": title,
                "created": now,
                "modified": now,
                "active_map_id": main_map_id,
            },
            "objects": {},
            "relations": {},
            "maps": {
                main_map_id: {
                    "id": main_map_id,
                    "name": "Neue Map",
                    "root_object_id": None,
                    "object_states": {},
                    "drawings": {},
                    "view": {
                        "zoom": 1.0,
                        "center_x": 0.0,
                        "center_y": 0.0,
                    },
                }
            },
            "attachments": {},
        })

    @property
    def active_map_id(self) -> str:
        return self.data["project"]["active_map_id"]

    @property
    def active_map(self) -> dict[str, Any]:
        return self.data["maps"][self.active_map_id]

    def add_map(self, name: str | None = None) -> str:
        """Legt eine leere Map an und aktiviert sie."""
        map_id = new_id("map")
        if not name:
            existing = {str(m.get("name", "")) for m in self.data["maps"].values()}
            base = "Neue Map"
            name = base
            number = 2
            while name in existing:
                name = f"{base} {number}"
                number += 1
        self.data["maps"][map_id] = {
            "id": map_id,
            "name": name,
            "root_object_id": None,
            "object_states": {},
            "drawings": {},
            "view": {
                "zoom": 1.0,
                "center_x": 0.0,
                "center_y": 0.0,
            },
        }
        self.data["project"]["active_map_id"] = map_id
        self.touch()
        return map_id

    def rename_map(self, map_id: str, name: str) -> None:
        clean = name.strip()
        if map_id in self.data["maps"] and clean:
            self.data["maps"][map_id]["name"] = clean
            self.touch()

    def remove_map(self, map_id: str) -> None:
        """Entfernt eine Map und bereinigt nicht mehr verwendete Objekte."""
        if map_id not in self.data["maps"]:
            return
        removed_ids = set(self.data["maps"][map_id].get("object_states", {}))
        del self.data["maps"][map_id]

        still_used: set[str] = set()
        for map_data in self.data["maps"].values():
            still_used.update(map_data.get("object_states", {}).keys())
        orphaned = removed_ids - still_used
        for object_id in orphaned:
            self.data["objects"].pop(object_id, None)
        for relation_id, relation in list(self.data["relations"].items()):
            if relation.get("source_id") in orphaned or relation.get("target_id") in orphaned:
                del self.data["relations"][relation_id]

        if self.data["maps"]:
            if self.data["project"].get("active_map_id") not in self.data["maps"]:
                self.data["project"]["active_map_id"] = next(iter(self.data["maps"]))
        self.touch()

    def add_object(self, title: str, x: float, y: float) -> str:
        object_id = new_id("object")
        self.data["objects"][object_id] = {
            "id": object_id,
            "type": "node",
            "content_role": "topic",
            "node_type": "topic",
            "symbol": "",
            "symbols": [],
            "status": "",
            "title": title,
            "rich_html": "",
            "note": "",
            "progress": 0,
            "markers": [],
            "tags": [],
            "attachments": [],
            "created": iso_now(),
            "modified": iso_now(),
        }
        self.active_map["object_states"][object_id] = {
            "x": x,
            "y": y,
            "width": 180.0,
            "height": 54.0,
            "collapsed": False,
            "visible": True,
            "layout_direction": "right",
            "default_branch_type": 1,
            "shape": "rounded",
            "fill_color": "#f7f7f7",
            "border_color": "#4f5d75",
            "text_color": "#202020",
            "border_width": 1.5,
            "corner_radius": 12.0,
            "layout_mode": "auto",
            "size_mode": "auto",
        }
        if self.active_map["root_object_id"] is None:
            self.active_map["root_object_id"] = object_id
        self.touch()
        return object_id


    def set_layout_mode(self, object_id: str, mode: str) -> None:
        if mode not in {"auto", "manual"}:
            raise ValueError(f"Unbekannter Layoutmodus: {mode}")
        state = self.active_map["object_states"].get(object_id)
        if state is None:
            return
        state["layout_mode"] = mode
        self.touch()

    def layout_mode(self, object_id: str) -> str:
        state = self.active_map["object_states"].get(object_id, {})
        return str(state.get("layout_mode", "auto"))

    def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str = "tree",
        branch_type: int | None = None,
    ) -> str:
        relation_id = new_id("relation")
        order = 0

        if relation_type == "tree":
            sibling_orders = [
                int(relation.get("order", 0))
                for relation in self.data["relations"].values()
                if relation.get("type") == "tree"
                and relation.get("source_id") == source_id
            ]
            order = (max(sibling_orders) + 1) if sibling_orders else 0

            if branch_type is None:
                source_state = self.active_map["object_states"].get(source_id, {})
                branch_type = int(source_state.get("default_branch_type", 1))
            if branch_type not in {1, 2, 3, 4}:
                raise ValueError(f"Ungültiger Asttyp für eine Baumrelation: {branch_type}")
        else:
            branch_type = 0 if branch_type is None else int(branch_type)

        self.data["relations"][relation_id] = {
            "id": relation_id,
            "source_id": source_id,
            "target_id": target_id,
            "type": relation_type,
            "branch_type": int(branch_type),
            "label": "",
            "order": order,
            "collapsed": False,
        }
        self.touch()
        return relation_id

    def remove_relation(self, relation_id: str) -> bool:
        """Löscht nur die Beziehung; die Knoten bleiben bestehen."""
        if relation_id not in self.data["relations"]:
            return False
        del self.data["relations"][relation_id]
        self.touch()
        return True

    def reset_relation_route(self, relation_id: str) -> bool:
        relation = self.data["relations"].get(relation_id)
        if relation is None:
            return False
        relation.pop("route_x", None)
        relation.pop("route_y", None)
        self.touch()
        return True

    @staticmethod
    def branch_type_to_direction(branch_type: int) -> str:
        return {1: "right", 2: "left", 3: "down", 4: "up"}.get(int(branch_type), "free")

    @staticmethod
    def direction_to_branch_type(direction: str) -> int:
        mapping = {"right": 1, "left": 2, "down": 3, "up": 4, "free": 0}
        if direction not in mapping:
            raise ValueError(f"Unbekannte Ast-Richtung: {direction}")
        return mapping[direction]

    def tree_relation_for_child(self, object_id: str) -> tuple[str, dict[str, Any]] | None:
        for relation_id, relation in self.data["relations"].items():
            if (
                relation.get("type") == "tree"
                and relation.get("target_id") == object_id
            ):
                return relation_id, relation
        return None

    def branch_type_of(self, object_id: str) -> int:
        relation_info = self.tree_relation_for_child(object_id)
        if relation_info is None:
            return 0
        return int(relation_info[1].get("branch_type", 1))

    def set_branch_type(self, object_id: str, branch_type: int) -> bool:
        branch_type = int(branch_type)
        if branch_type == 0:
            return self.detach_from_parent(object_id)
        if branch_type not in {1, 2, 3, 4}:
            raise ValueError(f"Unbekannter Asttyp: {branch_type}")

        relation_info = self.tree_relation_for_child(object_id)
        if relation_info is None:
            return False
        relation_info[1]["branch_type"] = branch_type
        self.touch()
        return True

    def set_default_branch_type(self, object_id: str, branch_type: int) -> None:
        branch_type = int(branch_type)
        if branch_type not in {1, 2, 3, 4}:
            raise ValueError(f"Unbekannter Standard-Asttyp: {branch_type}")
        state = self.active_map["object_states"][object_id]
        if state.get("shape") == "underline" and branch_type not in {1, 2}:
            raise ValueError("Unterstrichene Knoten erlauben nur Äste nach links oder rechts.")
        state["default_branch_type"] = branch_type
        state["layout_direction"] = self.branch_type_to_direction(branch_type)

        # Der Menüpunkt wirkt bewusst auf alle vorhandenen direkten Äste.
        for relation in self.data["relations"].values():
            if (
                relation.get("type") == "tree"
                and relation.get("source_id") == object_id
            ):
                relation["branch_type"] = branch_type

        self.arrange_children(object_id)
        self.touch()

    def infer_branch_type(self, parent_id: str, child_id: str) -> int:
        states = self.active_map["object_states"]
        parent = states[parent_id]
        child = states[child_id]

        pcx = float(parent.get("x", 0.0)) + float(parent.get("width", 180.0)) / 2.0
        pcy = float(parent.get("y", 0.0)) + float(parent.get("height", 54.0)) / 2.0
        ccx = float(child.get("x", 0.0)) + float(child.get("width", 180.0)) / 2.0
        ccy = float(child.get("y", 0.0)) + float(child.get("height", 54.0)) / 2.0

        dx = ccx - pcx
        dy = ccy - pcy

        parent_left = float(parent.get("x", 0.0))
        parent_top = float(parent.get("y", 0.0))
        parent_right = parent_left + float(parent.get("width", 180.0))
        parent_bottom = parent_top + float(parent.get("height", 54.0))
        child_left = float(child.get("x", 0.0))
        child_top = float(child.get("y", 0.0))
        child_right = child_left + float(child.get("width", 180.0))
        child_bottom = child_top + float(child.get("height", 54.0))

        # Liegt der Kindknoten vollständig neben dem Elternknoten, hat diese
        # eindeutige Seitenlage Vorrang. Dadurch bleibt ein nach rechts
        # geschobener Knoten nicht fälschlich am unteren Astabgang hängen.
        side_margin = 10.0
        if child_left >= parent_right - side_margin:
            return 1
        if child_right <= parent_left + side_margin:
            return 2
        if child_top >= parent_bottom - side_margin:
            return 3
        if child_bottom <= parent_top + side_margin:
            return 4

        # Nur in den überlappenden Eckbereichen entscheidet die dominante
        # Entfernung der Mittelpunkte.
        if abs(dx) >= abs(dy):
            return 1 if dx >= 0 else 2
        return 3 if dy >= 0 else 4

    def resort_outgoing_relations_from_geometry(self, parent_id: str) -> bool:
        """Äste eines Elternknotens aus der aktuellen Geometrie neu sortieren.

        Keine Knotenposition wird verändert.
        Alte manuelle Führungspunkte werden verworfen, da sie zur alten
        Liniengeometrie gehören.
        """
        changed = False
        states = self.active_map["object_states"]
        if parent_id not in states:
            return False

        for relation in self.data["relations"].values():
            if relation.get("type") != "tree":
                continue
            if relation.get("source_id") != parent_id:
                continue

            child_id = relation.get("target_id")
            if child_id not in states:
                continue

            inferred = self.infer_branch_type(parent_id, child_id)
            if int(relation.get("branch_type", 1)) != int(inferred):
                relation["branch_type"] = int(inferred)
                changed = True

            if "route_x" in relation or "route_y" in relation:
                relation.pop("route_x", None)
                relation.pop("route_y", None)
                changed = True

        if changed:
            self.touch()
        return changed

    def refresh_branch_type_from_position(self, object_id: str) -> bool:
        """Passt die Anschlussseite eines Kindes an seine aktuelle Lage an."""
        parent_id = self.tree_parent(object_id)
        if parent_id is None:
            return False
        inferred = self.infer_branch_type(parent_id, object_id)
        relation_info = self.tree_relation_for_child(object_id)
        if relation_info is None:
            return False
        relation = relation_info[1]
        if int(relation.get("branch_type", 1)) == inferred:
            return False
        relation["branch_type"] = inferred
        self.touch()
        return True

    def tree_parent(self, object_id: str) -> str | None:
        for relation in self.data["relations"].values():
            if (
                relation.get("type") == "tree"
                and relation.get("target_id") == object_id
            ):
                return relation.get("source_id")
        return None

    def tree_children(self, object_id: str) -> list[str]:
        return [
            relation["target_id"]
            for relation in self.data["relations"].values()
            if relation.get("type") == "tree"
            and relation.get("source_id") == object_id
        ]

    def is_descendant(self, possible_descendant_id: str, object_id: str) -> bool:
        pending = list(self.tree_children(object_id))
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == possible_descendant_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.tree_children(current))
        return False

    def set_tree_parent(self, object_id: str, new_parent_id: str) -> bool:
        if object_id == new_parent_id:
            return False
        if self.is_descendant(new_parent_id, object_id):
            return False
        if self.tree_parent(object_id) == new_parent_id:
            return False

        relation_ids = [
            relation_id
            for relation_id, relation in self.data["relations"].items()
            if relation.get("type") == "tree"
            and relation.get("target_id") == object_id
        ]
        for relation_id in relation_ids:
            self.data["relations"].pop(relation_id, None)

        self.add_relation(
            new_parent_id,
            object_id,
            "tree",
            branch_type=self.infer_branch_type(new_parent_id, object_id),
        )
        if self.active_map.get("root_object_id") == object_id:
            self.active_map["root_object_id"] = new_parent_id
        self.touch()
        return True

    def remove_object(self, object_id: str) -> None:
        """Entfernt genau einen Knoten und seine direkten Relationen.

        Für Benutzeraktionen ist normalerweise ``remove_subtrees`` zu verwenden,
        damit ein Elternknoten nicht versehentlich verwaiste Kinder zurücklässt.
        """
        self._remove_object_without_touch(object_id)
        self.touch()

    def _remove_object_without_touch(self, object_id: str) -> None:
        self.data["objects"].pop(object_id, None)
        for map_data in self.data["maps"].values():
            map_data.get("object_states", {}).pop(object_id, None)
            if map_data.get("root_object_id") == object_id:
                map_data["root_object_id"] = None

        relation_ids = [
            relation_id
            for relation_id, relation in self.data["relations"].items()
            if relation["source_id"] == object_id or relation["target_id"] == object_id
        ]
        for relation_id in relation_ids:
            self.data["relations"].pop(relation_id, None)

    def remove_subtrees(self, object_ids: set[str] | list[str]) -> set[str]:
        """Entfernt die ausgewählten Teilbäume als atomare Benutzeraktion.

        Sind Eltern und Kind gleichzeitig markiert, wird nur der Elternknoten als
        Löschwurzel behandelt. Freie, unverbundene Knoten werden einzeln gelöscht.
        """
        selected = {object_id for object_id in object_ids if object_id in self.data["objects"]}
        if not selected:
            return set()

        roots = {
            object_id
            for object_id in selected
            if self.tree_parent(object_id) not in selected
        }
        removed: set[str] = set()
        for root_id in roots:
            removed.update(self.subtree_ids(root_id))

        for object_id in list(removed):
            self._remove_object_without_touch(object_id)

        # Falls die bisherige Map-Wurzel gelöscht wurde, einen verbleibenden
        # wurzellosen Knoten als neue technische Wurzel wählen. Das ändert keine
        # sichtbaren Verbindungen, verhindert aber einen inkonsistenten Zustand.
        states = self.active_map.get("object_states", {})
        if self.active_map.get("root_object_id") not in states:
            remaining_roots = [
                object_id for object_id in states
                if self.tree_parent(object_id) is None
            ]
            self.active_map["root_object_id"] = remaining_roots[0] if remaining_roots else None

        self.touch()
        return removed

    def child_ids(self, parent_id: str) -> list[str]:
        children: list[tuple[int, str]] = []
        for relation in self.data["relations"].values():
            if relation.get("type") != "tree":
                continue
            if relation.get("source_id") != parent_id:
                continue
            children.append((int(relation.get("order", 0)), relation["target_id"]))
        children.sort(key=lambda item: item[0])
        return [child_id for _, child_id in children]

    def last_child_id(
        self,
        parent_id: str,
        branch_type: int | None = None,
    ) -> str | None:
        candidates: list[tuple[int, str]] = []
        for relation in self.data["relations"].values():
            if relation.get("type") != "tree":
                continue
            if relation.get("source_id") != parent_id:
                continue
            if (
                branch_type is not None
                and int(relation.get("branch_type", 1)) != int(branch_type)
            ):
                continue
            candidates.append(
                (int(relation.get("order", 0)), relation["target_id"])
            )

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    def apply_child_template(
        self,
        source_child_id: str,
        target_child_id: str,
    ) -> None:
        """Übernimmt Inhaltsart und Darstellung des letzten Geschwisters."""
        source_object = self.data["objects"].get(source_child_id)
        target_object = self.data["objects"].get(target_child_id)
        if source_object is None or target_object is None:
            return

        target_object["content_role"] = source_object.get(
            "content_role", "topic"
        )
        target_object["node_type"] = source_object.get("node_type", "topic")
        target_object["symbol"] = source_object.get("symbol", "")
        target_object["symbols"] = list(source_object.get("symbols", []))
        target_object["status"] = ""

        states = self.active_map["object_states"]
        source_state = states.get(source_child_id)
        target_state = states.get(target_child_id)
        if source_state is None or target_state is None:
            return

        for key in (
            "shape",
            "fill_color",
            "border_color",
            "text_color",
        ):
            if key in source_state:
                target_state[key] = deepcopy(source_state[key])

        # Eine manuell gewählte Textbreite darf als Vorlage dienen. Die Höhe
        # wird nie vererbt: Ein neuer Knoten beginnt immer mit Mindesthöhe und
        # wächst anschließend ausschließlich mit seinem eigenen Inhalt.
        if source_state.get("size_mode", "auto") == "manual":
            target_state["width"] = deepcopy(source_state.get("width", 180.0))
            target_state["size_mode"] = "manual"
        else:
            target_state["width"] = 180.0
            target_state["size_mode"] = "auto"
        target_state["height"] = 54.0

        self.touch()

    def apply_visual_template(
        self,
        source_id: str,
        target_id: str,
    ) -> None:
        """Übernimmt nur die visuelle Darstellung eines Knotens."""
        states = self.active_map["object_states"]
        source_state = states.get(source_id)
        target_state = states.get(target_id)
        if source_state is None or target_state is None:
            return

        for key in (
            "shape",
            "fill_color",
            "border_color",
            "text_color",
        ):
            if key in source_state:
                target_state[key] = deepcopy(source_state[key])

        # Eine manuell gewählte Textbreite darf als Vorlage dienen. Die Höhe
        # wird nie vererbt: Ein neuer Knoten beginnt immer mit Mindesthöhe und
        # wächst anschließend ausschließlich mit seinem eigenen Inhalt.
        if source_state.get("size_mode", "auto") == "manual":
            target_state["width"] = deepcopy(source_state.get("width", 180.0))
            target_state["size_mode"] = "manual"
        else:
            target_state["width"] = 180.0
            target_state["size_mode"] = "auto"
        target_state["height"] = 54.0

        self.touch()

    def child_ids_by_branch(self, parent_id: str, branch_type: int) -> list[str]:
        children: list[tuple[int, str]] = []
        for relation in self.data["relations"].values():
            if relation.get("type") != "tree":
                continue
            if relation.get("source_id") != parent_id:
                continue
            if int(relation.get("branch_type", 1)) != int(branch_type):
                continue
            children.append((int(relation.get("order", 0)), relation["target_id"]))
        children.sort(key=lambda item: item[0])
        return [child_id for _, child_id in children]

    def subtree_ids(self, object_id: str) -> list[str]:
        result: list[str] = []
        pending = [object_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            pending.extend(self.child_ids(current))
        return result

    def shift_subtree(self, object_id: str, dx: float, dy: float) -> None:
        states = self.active_map["object_states"]
        for child_id in self.subtree_ids(object_id):
            state = states.get(child_id)
            if state is None:
                continue
            state["x"] = float(state.get("x", 0.0)) + dx
            state["y"] = float(state.get("y", 0.0)) + dy
        self.touch()

    @staticmethod
    def _rects_overlap(
        ax: float, ay: float, aw: float, ah: float,
        bx: float, by: float, bw: float, bh: float,
        margin: float = 18.0,
    ) -> bool:
        return not (
            ax + aw + margin <= bx
            or bx + bw + margin <= ax
            or ay + ah + margin <= by
            or by + bh + margin <= ay
        )

    def make_space_for_node(
        self,
        x: float,
        y: float,
        width: float = 180.0,
        height: float = 54.0,
        direction: str = "right",
        excluded_ids: set[str] | None = None,
    ) -> tuple[float, float]:
        """Schiebt vorhandene Teilbäume aus dem Zielbereich eines neuen Knotens."""
        excluded = excluded_ids or set()
        states = self.active_map["object_states"]
        step_x = width + 80.0
        step_y = height + 36.0

        # Mehrfach prüfen, da ein verschobener Teilbaum den nächsten berühren kann.
        for _ in range(max(1, len(states) + 2)):
            conflict_id: str | None = None
            for object_id, state in states.items():
                if object_id in excluded:
                    continue
                # Unterstrichene Knoten sind kompakte Listenpunkte und
                # werden nicht als Kollisionshindernis behandelt.
                if state.get("shape", "rounded") == "underline":
                    continue
                if self._rects_overlap(
                    x, y, width, height,
                    float(state.get("x", 0.0)),
                    float(state.get("y", 0.0)),
                    float(state.get("width", 180.0)),
                    float(state.get("height", 54.0)),
                ):
                    conflict_id = object_id
                    break

            if conflict_id is None:
                return x, y

            # Bei horizontalen Ästen wird nach unten Platz geschaffen,
            # bei nach unten laufenden Ästen nach rechts.
            if direction == "down":
                self.shift_subtree(conflict_id, step_x, 0.0)
            else:
                self.shift_subtree(conflict_id, 0.0, step_y)

        return x, y

    def detach_from_parent(self, object_id: str) -> bool:
        relation_ids = [
            relation_id
            for relation_id, relation in self.data["relations"].items()
            if relation.get("type") == "tree"
            and relation.get("target_id") == object_id
        ]
        if not relation_ids:
            return False
        for relation_id in relation_ids:
            self.data["relations"].pop(relation_id, None)
        self.touch()
        return True

    def set_node_size(self, object_id: str, width: float, height: float) -> None:
        state = self.active_map["object_states"][object_id]
        state["width"] = max(80.0, float(width))
        state["height"] = max(36.0, float(height))
        self.touch()

    def subtree_bounds(self, object_id: str) -> tuple[float, float, float, float]:
        """Liefert die gespeicherte Begrenzung eines kompletten Teilbaums."""
        states = self.active_map["object_states"]
        ids = [node_id for node_id in self.subtree_ids(object_id) if node_id in states]
        if not ids:
            return 0.0, 0.0, 0.0, 0.0

        left = min(float(states[node_id].get("x", 0.0)) for node_id in ids)
        top = min(float(states[node_id].get("y", 0.0)) for node_id in ids)
        right = max(
            float(states[node_id].get("x", 0.0))
            + float(states[node_id].get("width", 180.0))
            for node_id in ids
        )
        bottom = max(
            float(states[node_id].get("y", 0.0))
            + float(states[node_id].get("height", 54.0))
            for node_id in ids
        )
        return left, top, right, bottom

    def arrange_children(
        self,
        parent_id: str,
        horizontal_gap: float = 86.0,
        vertical_gap: float = 42.0,
    ) -> None:
        """Ordnet rechte, linke und untere Äste unabhängig und ausgewogen an."""
        states = self.active_map["object_states"]
        parent = states.get(parent_id)
        if parent is None:
            return

        px = float(parent.get("x", 0.0))
        py = float(parent.get("y", 0.0))
        pw = float(parent.get("width", 180.0))
        ph = float(parent.get("height", 54.0))
        parent_cx = px + pw / 2.0
        parent_cy = py + ph / 2.0

        for branch_type in (1, 2):
            children = [
                child_id
                for child_id in self.child_ids_by_branch(parent_id, branch_type)
                if child_id in states
            ]
            if not children:
                continue

            bounds = {child_id: self.subtree_bounds(child_id) for child_id in children}
            heights = [max(ph, bounds[c][3] - bounds[c][1]) for c in children]
            total_height = sum(heights) + vertical_gap * max(0, len(children) - 1)
            cursor_y = parent_cy - total_height / 2.0

            for child_id, subtree_height in zip(children, heights):
                left, top, right, bottom = bounds[child_id]
                subtree_width = right - left
                desired_top = cursor_y
                if branch_type == 1:
                    desired_left = px + pw + horizontal_gap
                else:
                    desired_left = px - horizontal_gap - subtree_width

                self.shift_subtree(child_id, desired_left - left, desired_top - top)
                cursor_y += subtree_height + vertical_gap

        down_children = [
            child_id
            for child_id in self.child_ids_by_branch(parent_id, 3)
            if child_id in states
        ]
        if down_children:
            bounds = {child_id: self.subtree_bounds(child_id) for child_id in down_children}
            widths = [max(pw, bounds[c][2] - bounds[c][0]) for c in down_children]
            total_width = sum(widths) + horizontal_gap * max(0, len(down_children) - 1)
            cursor_x = parent_cx - total_width / 2.0

            for child_id, subtree_width in zip(down_children, widths):
                left, top, right, bottom = bounds[child_id]
                desired_left = cursor_x
                desired_top = py + ph + vertical_gap
                self.shift_subtree(child_id, desired_left - left, desired_top - top)
                cursor_x += subtree_width + horizontal_gap

        up_children = [
            child_id
            for child_id in self.child_ids_by_branch(parent_id, 4)
            if child_id in states
        ]
        if up_children:
            bounds = {child_id: self.subtree_bounds(child_id) for child_id in up_children}
            widths = [max(pw, bounds[c][2] - bounds[c][0]) for c in up_children]
            total_width = sum(widths) + horizontal_gap * max(0, len(up_children) - 1)
            cursor_x = parent_cx - total_width / 2.0

            for child_id, subtree_width in zip(up_children, widths):
                left, top, right, bottom = bounds[child_id]
                desired_left = cursor_x
                desired_bottom = py - vertical_gap
                self.shift_subtree(child_id, desired_left - left, desired_bottom - bottom)
                cursor_x += subtree_width + horizontal_gap

        self.touch()

    def arrange_hierarchy_from(
        self,
        changed_parent_id: str,
    ) -> None:
        """
        Ordnet nach einer Strukturänderung alle betroffenen Ebenen neu.

        Zuerst wird der unmittelbar gewachsene Teilbaum angeordnet. Danach
        werden seine Vorfahren von innen nach außen aktualisiert. Dadurch
        schaffen Geschwister-Teilbäume automatisch Platz, sobald ein Kind
        weitere Kinder erhält.
        """
        current_id: str | None = changed_parent_id
        visited: set[str] = set()

        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            self.arrange_children(current_id)
            current_id = self.tree_parent(current_id)

    def effective_child_branch_type(self, parent_id: str) -> int:
        """Bestimmt die natürliche Wachstumsrichtung für neue Kinder.

        Ein normaler Hierarchieast läuft durch einen Knoten hindurch weiter:
        Kommt der Ast von rechts an, wachsen dessen Kinder nach links; kommt
        er von links an, wachsen sie nach rechts. Im Datenmodell entspricht
        das derselben Branch-Richtung wie die eingehende Elternrelation.

        Bereits vorhandene Geschwister haben Vorrang, damit alle direkten
        Kinder eines Knotens auf derselben Seite angelegt werden. Die Wurzel
        verwendet weiterhin ihre frei wählbare Standardrichtung.
        """
        existing = self.child_ids(parent_id)
        if existing:
            relation = self.tree_relation_for_child(existing[-1])
            if relation is not None:
                branch_type = int(relation[1].get("branch_type", 1))
                if branch_type in {1, 2, 3, 4}:
                    return branch_type

        incoming = self.branch_type_of(parent_id)
        if incoming in {1, 2, 3, 4}:
            return incoming

        state = self.active_map["object_states"][parent_id]
        branch_type = int(state.get("default_branch_type", 1))
        return branch_type if branch_type in {1, 2, 3, 4} else 1

    def next_child_position(
        self,
        parent_id: str,
        horizontal_gap: float = 86.0,
        vertical_gap: float = 42.0,
        branch_type: int | None = None,
    ) -> tuple[float, float]:
        parent = self.active_map["object_states"][parent_id]
        if branch_type is None:
            branch_type = self.effective_child_branch_type(parent_id)
        branch_type = int(branch_type)
        px = float(parent.get("x", 0.0))
        py = float(parent.get("y", 0.0))
        pw = float(parent.get("width", 180.0))
        ph = float(parent.get("height", 54.0))

        if branch_type == 2:
            return px - horizontal_gap - 180.0, py
        if branch_type == 3:
            return px, py + ph + vertical_gap
        if branch_type == 4:
            return px, py - vertical_gap - 54.0
        return px + pw + horizontal_gap, py

    NODE_TYPE_PRESETS = {
        "topic": {
            "label": "Thema",
            "symbol": "",
            "symbols": [],
            "fill_color": "#f7f7f7",
            "border_color": "#4f5d75",
            "text_color": "#202020",
        },
        "task": {
            "label": "Aufgabe",
            "symbol": "✓",
            "fill_color": "#fff3bf",
            "border_color": "#b58900",
            "text_color": "#202020",
        },
        "idea": {
            "label": "Idee",
            "symbol": "💡",
            "fill_color": "#fff7cc",
            "border_color": "#d4a017",
            "text_color": "#202020",
        },
        "document": {
            "label": "Dokument",
            "symbol": "📄",
            "fill_color": "#eef1f5",
            "border_color": "#687386",
            "text_color": "#202020",
        },
        "link": {
            "label": "Verweis",
            "symbol": "🔗",
            "fill_color": "#e8f1ff",
            "border_color": "#3973b7",
            "text_color": "#17365d",
        },
        "risk": {
            "label": "Risiko",
            "symbol": "⚠",
            "fill_color": "#ffe3e3",
            "border_color": "#c92a2a",
            "text_color": "#7f1d1d",
        },
        "electronics": {
            "label": "Elektronik",
            "symbol": "⚡",
            "fill_color": "#e6f7df",
            "border_color": "#4b8f29",
            "text_color": "#1f3d16",
        },
        "software": {
            "label": "Software",
            "symbol": "💻",
            "fill_color": "#e4f0ff",
            "border_color": "#3266a8",
            "text_color": "#17365d",
        },
        "mechanics": {
            "label": "Mechanik",
            "symbol": "⚙",
            "fill_color": "#fff0df",
            "border_color": "#bd6b20",
            "text_color": "#5d3212",
        },
        "measurement": {
            "label": "Messung",
            "symbol": "📈",
            "fill_color": "#e2f7f4",
            "border_color": "#23877c",
            "text_color": "#174d48",
        },
        "image": {
            "label": "Bild",
            "symbol": "🖼",
            "fill_color": "#f2e8ff",
            "border_color": "#7950b3",
            "text_color": "#45246b",
        },
        "beekeeping": {
            "label": "Imkerei",
            "symbol": "🐝",
            "fill_color": "#fff1b8",
            "border_color": "#b77900",
            "text_color": "#533800",
        },
    }

    def set_node_type(self, object_id: str, node_type: str, apply_preset: bool = True) -> None:
        if node_type not in self.NODE_TYPE_PRESETS:
            raise ValueError(f"Unbekannter Knotentyp: {node_type}")
        obj = self.data["objects"][object_id]
        obj["node_type"] = node_type
        if apply_preset:
            preset = self.NODE_TYPE_PRESETS[node_type]
            obj["symbol"] = preset["symbol"]
            obj["symbols"] = [preset["symbol"]] if preset["symbol"] else []
            state = self.active_map["object_states"][object_id]
            state["shape"] = "rounded"
            state["fill_color"] = preset["fill_color"]
            state["border_color"] = preset["border_color"]
            state["text_color"] = preset["text_color"]
        obj["modified"] = iso_now()
        self.touch()

    def node_type(self, object_id: str) -> str:
        return self.data["objects"].get(object_id, {}).get("node_type", "topic")

    def symbols(self, object_id: str) -> list[str]:
        """Liefert alle Symbole eines Knotens, inklusive alter Projektdateien."""
        obj = self.data["objects"].get(object_id, {})
        stored = obj.get("symbols")
        if isinstance(stored, list):
            return [str(value) for value in stored if str(value)]
        legacy = str(obj.get("symbol", ""))
        return [legacy] if legacy else []

    def set_symbols(self, object_id: str, symbols: list[str]) -> None:
        obj = self.data["objects"][object_id]
        normalized: list[str] = []
        for symbol in symbols:
            value = str(symbol)
            if value and value not in normalized:
                normalized.append(value)
        obj["symbols"] = normalized
        # Das alte Feld bleibt für ältere Programmversionen lesbar.
        obj["symbol"] = "".join(normalized)
        obj["modified"] = iso_now()
        self.touch()

    def toggle_symbol(self, object_id: str, symbol: str) -> None:
        current = self.symbols(object_id)
        if symbol in current:
            current.remove(symbol)
        elif symbol:
            current.append(symbol)
        self.set_symbols(object_id, current)

    def clear_symbols(self, object_id: str) -> None:
        self.set_symbols(object_id, [])

    def set_symbol(self, object_id: str, symbol: str) -> None:
        """Kompatible Einzelsymbol-Schnittstelle für ältere Aufrufer."""
        self.set_symbols(object_id, [symbol] if symbol else [])

    def symbol(self, object_id: str) -> str:
        return "".join(self.symbols(object_id))

    def set_status(self, object_id: str, status: str | None) -> None:
        normalized = "" if status in {None, "", "none", "open"} else str(status)
        if normalized not in {"", "working", "done", "waiting"}:
            raise ValueError(f"Unbekannter Status: {status}")
        obj = self.data["objects"][object_id]
        obj["status"] = normalized
        obj["modified"] = iso_now()
        self.touch()

    def status(self, object_id: str) -> str:
        status = self.data["objects"].get(object_id, {}).get("status", "")
        # V0.9.1-Dateien verwendeten "open" als Zwangsstandard.
        # Ab V0.9.2 bedeutet dieser Altwert: kein Statussymbol.
        return "" if status in {None, "", "none", "open"} else str(status)

    def set_content_role(self, object_id: str, role: str) -> None:
        if role not in {"topic", "section", "list_item", "note", "reference"}:
            raise ValueError(f"Unbekannte Inhaltsart: {role}")

        obj = self.data["objects"][object_id]
        obj["content_role"] = role
        obj["modified"] = iso_now()
        self.touch()

    def content_role(self, object_id: str) -> str:
        return self.data["objects"].get(object_id, {}).get("content_role", "topic")

    def set_title(self, object_id: str, title: str) -> None:
        title = title.strip() or "Unbenannter Knoten"
        obj = self.data["objects"][object_id]
        obj["title"] = title
        obj["modified"] = iso_now()
        self.touch()

    def rich_html(self, object_id: str) -> str:
        return str(self.data["objects"].get(object_id, {}).get("rich_html", "") or "")

    def set_rich_text(self, object_id: str, html: str, plain_text: str) -> None:
        plain_text = plain_text.strip() or "Unbenannter Knoten"
        obj = self.data["objects"][object_id]
        obj["title"] = plain_text
        obj["rich_html"] = str(html or "")
        obj["modified"] = iso_now()
        self.touch()


    # ------------------------------------------------------------------
    # V2.0: Einheitliche Inhalts-API fuer alle Knoten
    # ------------------------------------------------------------------
    def ensure_node_content(self, object_id: str) -> dict[str, Any]:
        """Stellt die stabilen Inhaltsfelder eines Knotens bereit.

        Auch ältere Projektdateien werden damit beim ersten Zugriff sicher
        ergänzt. GUI und Grafik greifen ab V2.0 nur noch über diese API auf
        Notizen und Anhänge zu.
        """
        obj = self.data.get("objects", {}).get(object_id)
        if obj is None:
            raise KeyError(f"Unbekannter Knoten: {object_id}")
        obj.setdefault("note", "")
        obj.setdefault("note_html", "")
        obj.setdefault("attachments", [])
        obj.setdefault("tags", [])
        obj.setdefault("status", "")
        obj.setdefault("node_type", "topic")
        return obj

    def note(self, object_id: str) -> str:
        """Plain-text shadow of the note, kept for search/old projects."""
        return str(self.ensure_node_content(object_id).get("note", "") or "")

    def note_html(self, object_id: str) -> str:
        return str(self.ensure_node_content(object_id).get("note_html", "") or "")

    def set_note(self, object_id: str, text: str) -> None:
        """Compatibility setter for old/plain callers."""
        obj = self.ensure_node_content(object_id)
        value = str(text or "")
        if str(obj.get("note", "") or "") == value and not obj.get("note_html"):
            return
        obj["note"] = value
        # Plain edits intentionally clear stale rich markup.
        obj["note_html"] = ""
        obj["modified"] = iso_now()
        self.touch()

    def set_note_rich(self, object_id: str, html: str, plain_text: str) -> None:
        """Store rich note HTML plus a plain-text shadow for compatibility."""
        obj = self.ensure_node_content(object_id)
        html_value = str(html or "")
        plain_value = str(plain_text or "")
        if (
            str(obj.get("note_html", "") or "") == html_value
            and str(obj.get("note", "") or "") == plain_value
        ):
            return
        obj["note_html"] = html_value
        obj["note"] = plain_value
        obj["modified"] = iso_now()
        self.touch()

    def attachment_count(self, object_id: str) -> int:
        return len(self.attachments(object_id))


    def attachments(self, object_id: str) -> list[dict[str, Any]]:
        obj = self.ensure_node_content(object_id)
        value = obj.get("attachments", [])
        if not isinstance(value, list):
            value = []
            obj["attachments"] = value
        return value

    def add_attachment(self, object_id: str, attachment_type: str, target: str, label: str) -> str:
        attachment_id = new_id("attachment")
        entry = {
            "id": attachment_id,
            "type": str(attachment_type or "file"),
            "target": str(target),
            "label": str(label or target),
            "created": iso_now(),
        }
        self.attachments(object_id).append(entry)
        self.data["objects"][object_id]["modified"] = iso_now()
        self.touch()
        return attachment_id

    def remove_attachment(self, object_id: str, attachment_id: str) -> bool:
        obj = self.data["objects"].get(object_id)
        if not obj:
            return False
        entries = self.attachments(object_id)
        before = len(entries)
        obj["attachments"] = [entry for entry in entries if entry.get("id") != attachment_id]
        changed = len(obj["attachments"]) != before
        if changed:
            obj["modified"] = iso_now()
            self.touch()
        return changed

    def set_layout_direction(self, object_id: str, direction: str) -> None:
        branch_type = self.direction_to_branch_type(direction)
        if branch_type == 0:
            raise ValueError("Ein freier Knoten ist keine Unterknoten-Anordnung.")
        self.set_default_branch_type(object_id, branch_type)

    def set_node_shape(self, object_id: str, shape: str) -> None:
        if shape not in {"none", "underline", "rect", "rounded"}:
            raise ValueError(f"Unbekannte Knotenform: {shape}")
        state = self.active_map["object_states"][object_id]
        old_shape = state.get("shape", "rounded")

        # Beim Wechsel Unterstrichen -> normal bleibt die bisherige
        # Sammelstamm-Verbindung erhalten. Der Typwechsel ändert damit die
        # Darstellung, nicht die bereits sinnvolle Anschluss-Topologie.
        relation_info = self.tree_relation_for_child(object_id)
        if old_shape == "underline" and shape != "underline" and relation_info is not None:
            relation_info[1]["keep_group_trunk"] = True

            # Ein Rechteck braucht mehr vertikalen Platz als eine kompakte
            # Unterstrichen-Zeile. Alles, was in derselben Seitengruppe
            # geometrisch unterhalb liegt, wächst nur nach unten weiter.
            parent_id = relation_info[1].get("source_id")
            branch_type = int(relation_info[1].get("branch_type", 1))
            current_y = float(state.get("y", 0.0))
            compact_pitch = 30.0
            normal_pitch = float(state.get("height", 54.0)) + 34.0
            extra_space = max(0.0, normal_pitch - compact_pitch)

            if parent_id is not None and extra_space > 0.0:
                for sibling_id in self.child_ids_by_branch(parent_id, branch_type):
                    if sibling_id == object_id:
                        continue
                    sibling = self.active_map["object_states"].get(sibling_id)
                    if sibling is None:
                        continue
                    if float(sibling.get("y", 0.0)) > current_y + 0.5:
                        sibling["y"] = float(sibling.get("y", 0.0)) + extra_space

        elif shape == "underline" and relation_info is not None:
            # Wird der Knoten wieder Unterstrichen, darf er erneut ganz normal
            # Teil der kompakten Unterstrichen-Gruppe sein.
            relation_info[1].pop("keep_group_trunk", None)

        state["shape"] = shape
        if shape == "underline":
            current = int(state.get("default_branch_type", 1))
            if current not in {1, 2}:
                state["default_branch_type"] = 1
                state["layout_direction"] = "right"
            for relation in self.data["relations"].values():
                if relation.get("type") == "tree" and relation.get("source_id") == object_id:
                    if int(relation.get("branch_type", 1)) not in {1, 2}:
                        relation["branch_type"] = 1
            self.arrange_children(object_id)
        self.touch()

    def set_node_color(self, object_id: str, key: str, color: str) -> None:
        if key not in {"fill_color", "border_color", "text_color"}:
            raise ValueError(f"Unbekannte Farbeigenschaft: {key}")
        self.active_map["object_states"][object_id][key] = color
        self.touch()

    def set_position(self, object_id: str, x: float, y: float) -> None:
        state = self.active_map["object_states"][object_id]
        state["x"] = float(x)
        state["y"] = float(y)
        self.touch()

    def toggle_collapsed(self, object_id: str) -> bool:
        """Kompatibilität: schaltet alle direkten Teiläste eines Knotens."""
        outgoing = [
            relation
            for relation in self.data["relations"].values()
            if relation.get("type") == "tree"
            and relation.get("source_id") == object_id
        ]
        new_state = not all(bool(relation.get("collapsed", False)) for relation in outgoing)
        for relation in outgoing:
            relation["collapsed"] = new_state
        self.active_map["object_states"][object_id]["collapsed"] = False
        self.touch()
        return new_state

    def toggle_relation_collapsed(self, relation_id: str) -> bool:
        """Blendet genau den Teilbaum hinter einem Austrittspunkt ein oder aus."""
        relation = self.data["relations"].get(relation_id)
        if relation is None or relation.get("type") != "tree":
            return False
        relation["collapsed"] = not bool(relation.get("collapsed", False))
        self.touch()
        return bool(relation["collapsed"])

    def touch(self) -> None:
        self.data["project"]["modified"] = iso_now()
        self.data["generator"] = {
            "name": APP_NAME,
            "version": APP_VERSION,
        }
