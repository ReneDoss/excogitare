"""Zentrale Layout-Hilfen für MindMapper V1.

V1 hält die Benutzergeometrie der Teilbäume stabil. Sichtbarkeitsänderungen
werden über das Modell ausgelöst; bei Kollisionen dürfen ausschließlich fremde
Teilbäume verschoben werden. Diese Datei ist der erste klar abgegrenzte Kern für
weitere Layout-Algorithmen, ohne die bestehende freie Mindmap-Geometrie zu
ersetzen.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayoutPolicy:
    node_margin: float = 28.0
    preserve_subtree_geometry: bool = True
    move_only_foreign_subtrees: bool = True


DEFAULT_LAYOUT_POLICY = LayoutPolicy()
