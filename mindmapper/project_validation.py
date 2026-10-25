"""Structural checks before project data is handed to Qt graphics items."""
from math import isfinite
from typing import Any


class InvalidProjectError(ValueError):
    pass


def validate_project(data: Any) -> None:
    def require(condition, message):
        if not condition:
            raise InvalidProjectError(message)

    def mapping(value, name):
        require(isinstance(value, dict), f'{name}: Objekt erwartet.')
        return value

    def number(value, name, positive=False):
        require(isinstance(value, (int, float)) and not isinstance(value, bool)
                and isfinite(value) and (not positive or value > 0),
                f'{name}: ungültige Zahl.')

    mapping(data, 'Projektdatei')
    fmt = mapping(data.get('format'), 'Dateiformat')
    require(fmt.get('name') == 'MindMapper Project', 'Unbekannter Dateiformatname.')
    project = mapping(data.get('project'), 'Projekt')
    objects = mapping(data.get('objects'), 'Knoten')
    relations = mapping(data.get('relations'), 'Beziehungen')
    maps = mapping(data.get('maps'), 'Maps')
    require(bool(maps), 'Das Projekt enthält keine Map.')
    require(isinstance(project.get('active_map_id'), str)
            and project['active_map_id'] in maps, 'Die aktive Map fehlt.')
    for oid, obj in objects.items():
        mapping(obj, f'Knoten {oid}')
        for field in ('title', 'rich_html', 'note', 'note_html'):
            require(isinstance(obj.get(field, ''), str), f'Knoten {oid}: {field} ist kein Text.')
        entries = obj.get('attachments', [])
        require(isinstance(entries, list), f'Knoten {oid}: ungültige Anhänge.')
        for entry in entries:
            mapping(entry, f'Anhang in {oid}')
        require(isinstance(obj.get('symbols', []), list), f'Knoten {oid}: ungültige Symbole.')

    children = {oid: [] for oid in objects}
    parents = set()
    for rid, rel in relations.items():
        mapping(rel, f'Beziehung {rid}')
        source, target = rel.get('source_id'), rel.get('target_id')
        require(isinstance(source, str) and source in objects
                and isinstance(target, str) and target in objects,
                f'Beziehung {rid} verweist auf fehlende Knoten.')
        for field in ('order', 'branch_type'):
            value = rel.get(field, 0)
            require(isinstance(value, int) and not isinstance(value, bool),
                    f'Beziehung {rid}: ungültiges {field}.')
        for field in ('route_x', 'route_y'):
            if field in rel:
                number(rel[field], f'Beziehung {rid}: {field}')
        if rel.get('type') == 'tree':
            require(target not in parents, f'Knoten {target} hat mehrere Baum-Eltern.')
            parents.add(target)
            children[source].append(target)
    # Kahn traversal avoids recursion even for deeply nested or cyclic files.
    indegree = {oid: int(oid in parents) for oid in objects}
    pending = [oid for oid, degree in indegree.items() if degree == 0]
    visited = 0
    while pending:
        oid = pending.pop()
        visited += 1
        for child in children[oid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                pending.append(child)
    require(visited == len(objects), 'Die Baumverbindungen enthalten einen Kreis.')

    for mid, map_data in maps.items():
        mapping(map_data, f'Map {mid}')
        states = mapping(map_data.get('object_states'), f'Map {mid}: Knotenpositionen')
        root = map_data.get('root_object_id')
        require(root is None or (isinstance(root, str) and root in states),
                f'Map {mid}: Wurzelknoten fehlt.')
        for oid, state in states.items():
            require(oid in objects, f'Map {mid}: Knoten {oid} fehlt.')
            mapping(state, f'Position {oid}')
            for field in ('x', 'y', 'width', 'height'):
                if field in state:
                    number(state[field], f'Knoten {oid}: {field}', field in ('width', 'height'))
        drawings = mapping(map_data.get('drawings', {}), f'Map {mid}: Zeichnungen')
        for did, drawing in drawings.items():
            mapping(drawing, f'Zeichnung {did}')
            for field in ('x', 'y', 'x1', 'x2', 'y1', 'y2', 'width', 'height', 'z', 'opacity'):
                if field in drawing:
                    number(drawing[field], f'Zeichnung {did}: {field}')
            for field in ('html', 'text'):
                require(isinstance(drawing.get(field, ''), str), f'Zeichnung {did}: ungültiger Text.')
