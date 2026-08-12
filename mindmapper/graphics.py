from __future__ import annotations

from copy import deepcopy
import base64
import json
from pathlib import Path

import shiboken6


from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QPointF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QFont, QKeyEvent, QKeySequence, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap,
    QTextCharFormat, QTextCursor, QTextListFormat
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsEllipseItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneMouseEvent,
    QGraphicsSimpleTextItem,
    QGraphicsTextItem,
    QColorDialog,
    QFileDialog,
    QInputDialog,
    QMenu,
)

from .model import ProjectModel, new_id
from .drawing import DrawingController


def _qt_valid(obj) -> bool:
    """True only while the wrapped Qt C++ object still exists."""
    if obj is None:
        return False
    try:
        return bool(shiboken6.isValid(obj))
    except (RuntimeError, ReferenceError):
        return False



def _make_detail_icon(kind: str, size: int = 15) -> QPixmap:
    """Erzeugt kleine, skalierbare Notiz- und Kettensymbole ohne Emoji-Font."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor("#667085"), 1.35)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if kind == "note":
        painter.drawRoundedRect(QRectF(2.5, 1.5, 10.0, 12.0), 1.4, 1.4)
        painter.drawLine(QPointF(4.5, 5.0), QPointF(10.5, 5.0))
        painter.drawLine(QPointF(4.5, 7.8), QPointF(10.5, 7.8))
        painter.drawLine(QPointF(4.5, 10.6), QPointF(8.5, 10.6))
    else:
        # Zwei liegende, ineinander greifende Kettenglieder.
        painter.save()
        painter.translate(7.5, 7.5)
        painter.rotate(-28.0)
        painter.drawRoundedRect(QRectF(-7.0, -2.4, 8.5, 4.8), 2.3, 2.3)
        painter.drawRoundedRect(QRectF(-1.5, -2.4, 8.5, 4.8), 2.3, 2.3)
        painter.drawLine(QPointF(-1.8, 0.0), QPointF(1.8, 0.0))
        painter.restore()
    painter.end()
    return pixmap

def _clipboard_image_html(max_width: int = 700) -> str | None:
    """Konvertiert das aktuelle Zwischenablagebild in selbständiges HTML.

    Das PNG wird als Data-URI eingebettet. Dadurch bleibt ein Screenshot auch
    nach Speichern, Schließen und erneutem Öffnen vollständig im Projekt.
    """
    clipboard = QApplication.clipboard()
    mime = clipboard.mimeData()
    if not mime.hasImage():
        return None

    image = clipboard.image()
    if image.isNull():
        return None

    display_width = image.width()
    display_height = image.height()
    if display_width > max_width:
        scale = max_width / float(display_width)
        display_width = max_width
        display_height = max(1, round(display_height * scale))

    encoded = QByteArray()
    buffer = QBuffer(encoded)
    if not buffer.open(QIODevice.WriteOnly):
        return None
    try:
        if not image.save(buffer, "PNG"):
            return None
    finally:
        buffer.close()

    payload = base64.b64encode(bytes(encoded)).decode("ascii")
    return (
        f'<img src="data:image/png;base64,{payload}" '
        f'width="{display_width}" height="{display_height}" />'
    )


class EditableNodeText(QGraphicsTextItem):
    """Rich-Text-Editor direkt im Knoten.

    F2 startet die Bearbeitung. Enter erzeugt eine neue Zeile,
    Ctrl+Enter beendet die Bearbeitung. Ctrl+B/Ctrl+I und Ctrl+Shift+L
    formatieren die aktuelle Auswahl beziehungsweise den Absatz.
    """

    def __init__(self, owner: "NodeItem", text: str) -> None:
        super().__init__(owner)
        self.owner = owner
        html = owner.model.rich_html(owner.object_id)
        if html:
            self.setHtml(html)
        else:
            self.setPlainText(text)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        state = owner.model.active_map["object_states"][owner.object_id]
        self.setDefaultTextColor(QColor(state.get("text_color", "#202020")))
        self._geometry_update_pending = False
        self.document().contentsChanged.connect(self._document_changed)
        # contentsChanged wird teilweise ausgelöst, bevor Qt das Rich-Text-Layout
        # vollständig neu berechnet hat. documentSizeChanged liefert die
        # tatsächlich gerenderte Dokumentgröße.
        self.document().documentLayout().documentSizeChanged.connect(
            self._document_size_changed
        )

        # Eingebettete Bilder werden beim Überfahren mit der Maus markiert.
        # Ein kleiner Griff unten rechts erlaubt proportionales Skalieren.
        self.setAcceptHoverEvents(True)

        self.image_hover_frame = QGraphicsRectItem(self)
        self.image_hover_frame.setPen(
            QPen(QColor("#2563eb"), 1.5, Qt.DashLine)
        )
        self.image_hover_frame.setBrush(Qt.NoBrush)
        self.image_hover_frame.setAcceptedMouseButtons(Qt.NoButton)
        self.image_hover_frame.setZValue(90)
        self.image_hover_frame.setVisible(False)

        self.image_resize_handle = ImageResizeHandle(self)

        owner.update_content_positions()

    def _document_changed(self) -> None:
        self._schedule_geometry_update()

    def _document_size_changed(self, _size) -> None:
        self._schedule_geometry_update()

    def _schedule_geometry_update(self) -> None:
        owner = getattr(self, "owner", None)
        if not _qt_valid(self) or not _qt_valid(owner) or self._geometry_update_pending:
            return
        self._geometry_update_pending = True
        QTimer.singleShot(0, self._apply_geometry_update)

    def _apply_geometry_update(self) -> None:
        # A queued document signal can arrive after scene.clear() deleted the
        # QGraphicsItems. Never dereference stale Python wrappers.
        try:
            self._geometry_update_pending = False
        except (RuntimeError, ReferenceError):
            return
        owner = getattr(self, "owner", None)
        if not _qt_valid(self) or not _qt_valid(owner):
            return
        try:
            owner.resize_to_document()
        except (RuntimeError, ReferenceError):
            return

    def _first_image(self):
        """Findet das erste eingebettete Bild im QTextDocument."""
        document = self.document()
        count = max(0, document.characterCount() - 1)

        for position in range(count):
            cursor = QTextCursor(document)
            cursor.setPosition(position)
            cursor.setPosition(position + 1, QTextCursor.KeepAnchor)
            fmt = cursor.charFormat()

            if fmt.isImageFormat():
                return cursor, fmt.toImageFormat()

        return None

    def _image_rect(self) -> QRectF | None:
        """Liefert die tatsächliche Position des ersten Inline-Bildes.

        Bilder liegen im QTextDocument wie ein einzelnes Textzeichen. Deshalb
        darf ihre Position nicht einfach mit dem Dokumentrand gleichgesetzt
        werden: Vor dem Bild kann Text stehen, wie z. B. "Petrol".
        """
        image = self._first_image()
        if image is None:
            return None

        cursor, image_format = image
        width = float(image_format.width())
        height = float(image_format.height())

        if width <= 0.0 or height <= 0.0:
            return None

        block = cursor.block()
        layout = block.layout()
        if layout is None:
            return None

        position_in_block = max(0, cursor.selectionStart() - block.position())
        line = layout.lineForTextPosition(position_in_block)
        if not line.isValid():
            return None

        # x-Position des Inline-Objekts innerhalb der Textzeile.
        cursor_x = line.cursorToX(position_in_block)
        # PySide6 liefert je nach Binding-Version entweder nur x oder
        # ein Tupel (x, trailingPosition) zurück.
        if isinstance(cursor_x, tuple):
            cursor_x = cursor_x[0]
        x = float(cursor_x)

        # blockBoundingRect() enthält die tatsächliche Dokumentposition des
        # Absatzes. line.y() berücksichtigt zusätzlich die Zeile im Absatz.
        block_rect = self.document().documentLayout().blockBoundingRect(block)
        y = float(block_rect.top() + line.y())

        return QRectF(x, y, width, height)

    def _set_image_size(self, width: float, height: float) -> None:
        image = self._first_image()
        if image is None:
            return

        cursor, image_format = image
        image_format.setWidth(float(width))
        image_format.setHeight(float(height))
        cursor.setCharFormat(image_format)

        self._update_image_hover()
        self._schedule_geometry_update()

    def _commit_image_resize(self) -> None:
        """Übernimmt die neue Bildgröße dauerhaft ins Projektmodell."""
        title = str(
            self.owner.model.data["objects"][self.owner.object_id].get(
                "title",
                "Bild",
            )
        )
        self.owner.model.set_rich_text(
            self.owner.object_id,
            self.toHtml(),
            title,
        )
        self.owner.resize_to_document()

    def _update_image_hover(self) -> None:
        rect = self._image_rect()
        if rect is None:
            self.image_hover_frame.setVisible(False)
            self.image_resize_handle.setVisible(False)
            return

        self.image_hover_frame.setRect(rect)
        self.image_hover_frame.setVisible(True)
        self.image_resize_handle.setPos(
            rect.right() - ImageResizeHandle.SIZE / 2.0,
            rect.bottom() - ImageResizeHandle.SIZE / 2.0,
        )
        self.image_resize_handle.setVisible(True)

    def _hide_image_hover(self) -> None:
        if getattr(self.image_resize_handle, "_dragging", False):
            return
        self.image_hover_frame.setVisible(False)
        self.image_resize_handle.setVisible(False)

    def hoverMoveEvent(self, event) -> None:
        rect = self._image_rect()
        if rect is not None and rect.contains(event.pos()):
            self._update_image_hover()
        elif not getattr(self.image_resize_handle, "_dragging", False):
            self._hide_image_hover()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self._hide_image_hover()
        super().hoverLeaveEvent(event)

    def begin_edit(self) -> None:
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus(Qt.OtherFocusReason)

        self.owner.scene_owner.editing_item = self
        self._undo_snapshot = self.owner.scene_owner.make_snapshot()
        self._original_html = self.toHtml()
        self._original_title = self.toPlainText().strip()

        QTimer.singleShot(0, self._select_all_text)

        cursor = self.textCursor()
        cursor.select(QTextCursor.Document)
        self.setTextCursor(cursor)
        self.owner.scene_owner.editing_item = self
        self._undo_snapshot = self.owner.scene_owner.make_snapshot()
        self._original_html = self.toHtml()
        self._original_title = self.toPlainText().strip()

    def _select_all_text(self) -> None:
        if not _qt_valid(self):
            return

        self.setFocus(Qt.OtherFocusReason)
        cursor = self.textCursor()
        cursor.select(QTextCursor.Document)
        self.setTextCursor(cursor)
        
    def finish_edit(self) -> None:
        plain = self.toPlainText().strip() or "Unbenannter Knoten"
        if not self.toPlainText().strip():
            self.setPlainText(plain)
        html = self.toHtml()
        if html != getattr(self, "_original_html", html):
            self.owner.scene_owner.commit_snapshot(getattr(self, "_undo_snapshot", None))
            self.owner.model.set_rich_text(self.owner.object_id, html, plain)
        # Keine alte Textmarkierung stehen lassen, sobald die Bearbeitung endet.
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.clearFocus()
        if self.owner.scene_owner.editing_item is self:
            self.owner.scene_owner.editing_item = None
        self.owner.resize_to_document()
        self.owner.scene_owner.rebuild()

    def toggle_bold(self) -> None:
        cursor = self.textCursor()
        current_weight = cursor.charFormat().fontWeight()
        is_bold = int(current_weight) >= int(QFont.Weight.Bold)

        fmt = QTextCharFormat()
        fmt.setFontWeight(
            QFont.Weight.Normal if is_bold else QFont.Weight.Bold
        )
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.textCursor().charFormat().fontItalic())
        cursor = self.textCursor()
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def toggle_underline(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self.textCursor().charFormat().fontUnderline())
        cursor = self.textCursor()
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def set_font_point_size(self, size: float) -> None:
        if size <= 0:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(size))
        cursor = self.textCursor()
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def set_text_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        cursor = self.textCursor()
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def insert_link(self, url: str) -> None:
        url = url.strip()
        if not url:
            return
        if "://" not in url and not url.lower().startswith(("mailto:", "file:")):
            url = "https://" + url
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setFontUnderline(True)
        fmt.setForeground(QColor("#1a5fb4"))
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(url, fmt)
        self.setTextCursor(cursor)

    def _toggle_list(self, style: QTextListFormat.Style) -> None:
        cursor = self.textCursor()
        current = cursor.currentList()
        if current is not None and current.format().style() == style:
            block_fmt = cursor.blockFormat()
            block_fmt.setIndent(0)
            cursor.setBlockFormat(block_fmt)
            current.remove(cursor.block())
            return
        fmt = QTextListFormat()
        fmt.setStyle(style)
        fmt.setIndent(1)
        cursor.createList(fmt)

    def toggle_bullet_list(self) -> None:
        self._toggle_list(QTextListFormat.ListDisc)

    def toggle_numbered_list(self) -> None:
        self._toggle_list(QTextListFormat.ListDecimal)

    def paste_from_clipboard(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        cursor = self.textCursor()

        # Bilder haben Vorrang vor HTML/Text. Das Windows-Snipping-Tool stellt
        # den Screenshot als Bild-MIME-Typ bereit.
        image_html = _clipboard_image_html()
        if image_html is not None:
            cursor.insertHtml(image_html)
        elif mime.hasHtml():
            cursor.insertHtml(mime.html())
        elif mime.hasText():
            cursor.insertText(mime.text())
        else:
            return False

        self.setTextCursor(cursor)
        self._schedule_geometry_update()
        return True

    def focusOutEvent(self, event) -> None:
        # Die statische Formatleiste darf benutzt werden, ohne dass die
        # Textbearbeitung beendet wird. Ein Klick zurück in die Zeichenfläche
        # beendet den Editor über MapScene.mousePressEvent.
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        modifiers = event.modifiers()
        control_only = modifiers == Qt.ControlModifier

        # Explizite Prüfung ist unter Windows zuverlässiger als ausschließlich
        # QKeySequence.Bold; insbesondere bei verschiedenen Tastaturlayouts.
        if control_only and event.key() == Qt.Key_B:
            self.toggle_bold(); event.accept(); return
        if event.matches(QKeySequence.Bold):
            self.toggle_bold(); event.accept(); return
        if event.matches(QKeySequence.Italic):
            self.toggle_italic(); event.accept(); return
        if event.matches(QKeySequence.Underline):
            self.toggle_underline(); event.accept(); return
        if event.matches(QKeySequence.Paste):
            self.paste_from_clipboard(); event.accept(); return
        if event.key() == Qt.Key_L and event.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier):
            self.toggle_bullet_list(); event.accept(); return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers()

            if modifiers == Qt.ControlModifier or modifiers == Qt.ShiftModifier:
                self.finish_edit()
                event.accept()
                return
          
        if event.key() == Qt.Key_Escape:
            self.setHtml(getattr(self, "_original_html", self.toHtml()))
            self.setTextInteractionFlags(Qt.NoTextInteraction)
            self.clearFocus()
            self.owner.scene_owner.editing_item = None
            self.owner.resize_to_document()
            self.owner.scene_owner.rebuild()
            event.accept(); return
        super().keyPressEvent(event)



class RelationGuideHandle(QGraphicsEllipseItem):
    """Verschiebbarer Führungspunkt einer Knotenverbindung."""

    def __init__(self, relation: "RelationItem") -> None:
        super().__init__(-6.0, -6.0, 12.0, 12.0, relation)
        self.relation = relation
        self.setBrush(QBrush(QColor("#ffffff")))
        self.setPen(QPen(QColor("#2563eb"), 1.5))
        self.setZValue(30)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.setCursor(Qt.SizeAllCursor)
        self.setVisible(False)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.relation.scene_owner.prepare_selection_click(
            self.relation,
            event.modifiers(),
            node_id=None,
        )
        self.relation.scene_owner.push_undo()
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.relation.set_manual_route(event.scenePos(), save=False)
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.relation.set_manual_route(event.scenePos(), save=True)
        event.accept()


class RelationItem(QGraphicsPathItem):
    def __init__(
        self,
        source: "NodeItem",
        target: "NodeItem",
        branch_type: int = 1,
        relation_id: str | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.target = target
        self.branch_type = int(branch_type)
        self.relation_id = relation_id
        self.scene_owner = source.scene_owner
        self.setZValue(-10)
        self.setPen(QPen(QColor("#607080"), 2.0))
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.guide_handle = RelationGuideHandle(self)
        source.relations.append(self)
        target.relations.append(self)

        # Ein neuer Ast verändert die harmonische Verteilung aller
        # Austrittspunkte am selben Elternknoten.
        self.update_source_group()

    @staticmethod
    def _side_center(rect: QRectF, side: str) -> QPointF:
        if side == "left":
            return QPointF(rect.left(), rect.center().y())
        if side == "right":
            return QPointF(rect.right(), rect.center().y())
        if side == "top":
            return QPointF(rect.center().x(), rect.top())
        return QPointF(rect.center().x(), rect.bottom())

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _source_siblings(self) -> list["RelationItem"]:
        """Alle sichtbaren Äste desselben Elternknotens und derselben Richtung."""
        siblings = [
            relation
            for relation in self.source.relations
            if relation.source is self.source
            and int(relation.branch_type) == int(self.branch_type)
            and relation.isVisible()
        ]

        if int(self.branch_type) in {3, 4}:
            siblings.sort(
                key=lambda relation: relation.target.sceneBoundingRect().center().x()
            )
        else:
            siblings.sort(
                key=lambda relation: relation.target.sceneBoundingRect().center().y()
            )
        return siblings

    def _dynamic_source_anchor(self, source_rect: QRectF) -> QPointF:
        """
        Der Elternanschluss wandert entlang der passenden Kante.

        Mehrere Geschwister werden in der Reihenfolge ihrer räumlichen Lage
        harmonisch über die Kante verteilt. Bei einem einzelnen Kind folgt
        der Anschluss direkt dessen Lage. Die Ecken bleiben frei.
        """
        margin = min(12.0, max(6.0, min(source_rect.width(), source_rect.height()) * 0.18))
        siblings = self._source_siblings()
        count = len(siblings)

        try:
            index = siblings.index(self)
        except ValueError:
            index = 0

        branch_type = int(self.branch_type)

        if branch_type in {3, 4}:
            minimum = source_rect.left() + margin
            maximum = source_rect.right() - margin

            if count <= 1:
                coordinate = self._clamp(
                    self.target.sceneBoundingRect().center().x(),
                    minimum,
                    maximum,
                )
            else:
                coordinate = minimum + (maximum - minimum) * index / (count - 1)

            edge_y = source_rect.bottom() if branch_type == 3 else source_rect.top()
            return QPointF(coordinate, edge_y)

        minimum = source_rect.top() + margin
        maximum = source_rect.bottom() - margin

        if count <= 1:
            coordinate = self._clamp(
                self.target.sceneBoundingRect().center().y(),
                minimum,
                maximum,
            )
        else:
            coordinate = minimum + (maximum - minimum) * index / (count - 1)

        if branch_type == 2:
            return QPointF(source_rect.left(), coordinate)
        return QPointF(source_rect.right(), coordinate)

    @staticmethod
    def _underline_anchor(item: "NodeItem", branch_type: int, outgoing: bool) -> QPointF | None:
        """Liefert bei unterstrichenen Knoten den Anschluss am Linienende."""
        state = item.model.active_map["object_states"][item.object_id]
        if state.get("shape", "rounded") != "underline":
            return None

        rect = item.sceneBoundingRect()
        y = item.mapToScene(QPointF(0.0, item.underline_y())).y()
        branch_type = int(branch_type)

        if outgoing:
            if branch_type == 2:
                return QPointF(rect.left(), y)
            if branch_type == 1:
                return QPointF(rect.right(), y)
        else:
            # Der Zielanschluss liegt jeweils auf der dem Elternknoten
            # zugewandten Seite der Unterstreichung.
            if branch_type == 2:
                return QPointF(rect.right(), y)
            if branch_type == 1:
                return QPointF(rect.left(), y)
        return None

    @classmethod
    def _target_anchor(
        cls, target: "NodeItem", target_rect: QRectF, branch_type: int
    ) -> QPointF:
        underline = cls._underline_anchor(target, branch_type, outgoing=False)
        if underline is not None:
            return underline
        if branch_type == 2:
            return cls._side_center(target_rect, "right")
        if branch_type == 3:
            return cls._side_center(target_rect, "top")
        if branch_type == 4:
            return cls._side_center(target_rect, "bottom")
        return cls._side_center(target_rect, "left")

    @classmethod
    def make_path(
        cls,
        source_rect: QRectF,
        target_rect: QRectF,
        branch_type: int = 1,
        start: QPointF | None = None,
    ) -> QPainterPath:
        branch_type = int(branch_type)

        if branch_type == 2:
            start = start or cls._side_center(source_rect, "left")
            end = cls._side_center(target_rect, "right")
            dx = max(55.0, abs(end.x() - start.x()) * 0.42)
            c1 = QPointF(start.x() - dx, start.y())
            c2 = QPointF(end.x() + dx, end.y())
        elif branch_type == 3:
            start = start or cls._side_center(source_rect, "bottom")
            end = cls._side_center(target_rect, "top")
            dy = max(55.0, abs(end.y() - start.y()) * 0.42)
            c1 = QPointF(start.x(), start.y() + dy)
            c2 = QPointF(end.x(), end.y() - dy)
        elif branch_type == 4:
            start = start or cls._side_center(source_rect, "top")
            end = cls._side_center(target_rect, "bottom")
            dy = max(55.0, abs(end.y() - start.y()) * 0.42)
            c1 = QPointF(start.x(), start.y() - dy)
            c2 = QPointF(end.x(), end.y() + dy)
        else:
            start = start or cls._side_center(source_rect, "right")
            end = cls._side_center(target_rect, "left")
            dx = max(55.0, abs(end.x() - start.x()) * 0.42)
            c1 = QPointF(start.x() + dx, start.y())
            c2 = QPointF(end.x() - dx, end.y())

        path = QPainterPath(start)
        path.cubicTo(c1, c2, end)
        return path

    def shape(self) -> QPainterPath:
        # Breite unsichtbare Trefferfläche; die sichtbare Linie bleibt schlank.
        stroker = QPainterPathStroker()
        stroker.setWidth(max(14.0, self.pen().widthF() + 12.0))
        return stroker.createStroke(self.path())

    def _relation_data(self) -> dict | None:
        if self.relation_id is None:
            return None
        return self.source.model.data["relations"].get(self.relation_id)

    def _manual_route(self) -> QPointF | None:
        data = self._relation_data()
        if not data or "route_x" not in data or "route_y" not in data:
            return None
        return QPointF(float(data["route_x"]), float(data["route_y"]))

    def set_manual_route(self, scene_pos: QPointF, save: bool = True) -> None:
        data = self._relation_data()
        if data is None:
            return
        data["route_x"] = float(scene_pos.x())
        data["route_y"] = float(scene_pos.y())
        if save:
            self.source.model.touch()
        self.update_path()

    def reset_manual_route(self) -> None:
        if self.relation_id is None:
            return
        self.scene_owner.push_undo()
        self.source.model.reset_relation_route(self.relation_id)
        self.update_path()

    def delete_relation(self) -> None:
        if self.relation_id is None:
            return
        self.scene_owner.push_undo()
        self.source.model.remove_relation(self.relation_id)
        self.scene_owner.rebuild()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged and hasattr(self, "guide_handle"):
            self.guide_handle.setVisible(bool(value))
            if bool(value):
                route = self._manual_route()
                self.guide_handle.setPos(route if route is not None else self.path().pointAtPercent(0.5))
        return super().itemChange(change, value)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.scene_owner.prepare_selection_click(
                self,
                event.modifiers(),
                node_id=None,
            )
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        reset_action = menu.addAction("Verbindung zurücksetzen")
        delete_action = menu.addAction("Verbindung löschen")
        chosen = menu.exec(event.screenPos())
        if chosen is reset_action:
            self.reset_manual_route()
        elif chosen is delete_action:
            self.delete_relation()
        event.accept()

    def update_path(self) -> None:
        source_rect = self.source.sceneBoundingRect()
        target_rect = self.target.sceneBoundingRect()

        if self.relation_id is not None:
            start = self.source.mapToScene(
                self.source.relation_exit_point(self.relation_id)
            )
        else:
            start = self._underline_anchor(self.source, self.branch_type, outgoing=True)
            if start is None:
                start = self._dynamic_source_anchor(source_rect)
        end = self._target_anchor(self.target, target_rect, self.branch_type)

        branch_type = int(self.branch_type)
        if branch_type == 2:
            dx = max(55.0, abs(end.x() - start.x()) * 0.42)
            c1 = QPointF(start.x() - dx, start.y())
            c2 = QPointF(end.x() + dx, end.y())
        elif branch_type == 3:
            dy = max(55.0, abs(end.y() - start.y()) * 0.42)
            c1 = QPointF(start.x(), start.y() + dy)
            c2 = QPointF(end.x(), end.y() - dy)
        elif branch_type == 4:
            dy = max(55.0, abs(end.y() - start.y()) * 0.42)
            c1 = QPointF(start.x(), start.y() - dy)
            c2 = QPointF(end.x(), end.y() + dy)
        else:
            dx = max(55.0, abs(end.x() - start.x()) * 0.42)
            c1 = QPointF(start.x() + dx, start.y())
            c2 = QPointF(end.x() - dx, end.y())

        route = self._manual_route()
        path = QPainterPath(start)
        if route is None:
            path.cubicTo(c1, c2, end)
        else:
            # Zwei weiche Teilkurven laufen exakt durch den verschobenen Führungspunkt.
            if branch_type in {1, 2}:
                c1a = QPointF((start.x() + route.x()) * 0.5, start.y())
                c2a = QPointF((start.x() + route.x()) * 0.5, route.y())
                c1b = QPointF((route.x() + end.x()) * 0.5, route.y())
                c2b = QPointF((route.x() + end.x()) * 0.5, end.y())
            else:
                c1a = QPointF(start.x(), (start.y() + route.y()) * 0.5)
                c2a = QPointF(route.x(), (start.y() + route.y()) * 0.5)
                c1b = QPointF(route.x(), (route.y() + end.y()) * 0.5)
                c2b = QPointF(end.x(), (route.y() + end.y()) * 0.5)
            path.cubicTo(c1a, c2a, route)
            path.cubicTo(c1b, c2b, end)
        self.setPath(path)
        if self.isSelected():
            self.guide_handle.setPos(route if route is not None else path.pointAtPercent(0.5))

    def update_source_group(self) -> None:
        """Aktualisiert alle Austrittspunkte dieses Elternknotens rekursiv korrekt."""
        for relation in list(self.source.relations):
            if (
                relation.source is self.source
                and int(relation.branch_type) == int(self.branch_type)
            ):
                relation.update_path()


class BranchExitHandle(QGraphicsRectItem):
    """Klickfläche direkt auf einem Austrittspunkt des Elternknotens.

    Jeder Baumast besitzt seinen eigenen Zustand. Im ausgeklappten Zustand ist
    die Fläche unsichtbar; im eingeklappten Zustand erscheint dort ein Plus.
    """

    def __init__(self, owner: "NodeItem", relation_id: str) -> None:
        super().__init__(-13.0, -13.0, 26.0, 26.0, owner)
        self.owner = owner
        self.relation_id = relation_id
        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setZValue(50)
        self.setCursor(Qt.PointingHandCursor)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        relation = self.owner.model.data["relations"].get(self.relation_id, {})
        if not relation.get("collapsed", False):
            return
        state = self.owner.model.active_map["object_states"][self.owner.object_id]
        marker_color = QColor(state.get("border_color", "#4f5d75"))
        pen = QPen(marker_color, max(1.6, float(state.get("border_width", 1.5))))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        arm = 4.5
        painter.drawLine(QPointF(-arm, 0.0), QPointF(arm, 0.0))
        painter.drawLine(QPointF(0.0, -arm), QPointF(0.0, arm))

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.owner.toggle_relation_collapsed(self.relation_id)
            event.accept()
            return
        super().mousePressEvent(event)


class ImageResizeHandle(QGraphicsRectItem):
    """Ziehgriff unten rechts an einem eingebetteten Bild."""

    SIZE = 10.0

    def __init__(self, editor: "EditableNodeText") -> None:
        half = self.SIZE / 2.0
        super().__init__(-half, -half, self.SIZE, self.SIZE, editor)
        self.editor = editor
        self.setZValue(100)
        self.setBrush(QColor("#ffffff"))
        self.setPen(QPen(QColor("#2563eb"), 1.5))
        self.setCursor(Qt.SizeFDiagCursor)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setVisible(False)

        self._snapshot = None
        self._dragging = False
        self._start_width = 0.0
        self._start_height = 0.0

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        image = self.editor._first_image()
        if image is None:
            return

        _cursor, image_format = image
        self._start_width = max(1.0, float(image_format.width()))
        self._start_height = max(1.0, float(image_format.height()))
        self.editor.owner.scene_owner.prepare_selection_click(
            self.editor.owner,
            event.modifiers(),
            node_id=self.editor.owner.object_id,
        )
        self._snapshot = self.editor.owner.scene_owner.make_snapshot()
        self._dragging = True
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._dragging:
            return

        rect = self.editor._image_rect()
        if rect is None:
            return

        local = self.editor.mapFromScene(event.scenePos())
        new_width = max(30.0, local.x() - rect.left())
        aspect = (
            self._start_height / self._start_width
            if self._start_width > 0.0
            else 1.0
        )
        new_height = max(20.0, new_width * aspect)
        self.editor._set_image_size(new_width, new_height)
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._dragging:
            return

        self._dragging = False
        self.editor._commit_image_resize()
        self.editor.owner.scene_owner.commit_snapshot(self._snapshot)
        self._snapshot = None
        self.editor._update_image_hover()
        event.accept()


class NodeResizeHandle(QGraphicsRectItem):
    """Ziehpunkt unten rechts zum manuellen Vergrößern und Verkleinern."""

    SIZE = 10.0

    def __init__(self, owner: "NodeItem") -> None:
        half = self.SIZE / 2.0
        super().__init__(-half, -half, self.SIZE, self.SIZE, owner)
        self.owner = owner
        self.setZValue(50)
        self.setBrush(QColor("#ffffff"))
        self.setPen(QPen(QColor("#2563eb"), 1.5))
        self.setCursor(Qt.SizeFDiagCursor)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setVisible(False)
        self._snapshot = None

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self.owner.scene_owner.prepare_selection_click(
            self.owner,
            event.modifiers(),
            node_id=self.owner.object_id,
        )
        self._snapshot = self.owner.scene_owner.make_snapshot()
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        local = self.owner.mapFromScene(event.scenePos())
        self.owner.resize_manually(local.x(), local.y())
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.owner.scene_owner.commit_snapshot(self._snapshot)
        self._snapshot = None
        event.accept()


class AttachmentRowItem(QGraphicsRectItem):
    """Kleine anklickbare Anhangszeile innerhalb eines Knotens."""

    ICONS = {
        "pdf": ("PDF", "#c62828"),
        "url": ("◎", "#1565c0"),
        "image": ("▧", "#2e7d32"),
        "folder": ("▰", "#d17b00"),
        "python": ("Py", "#366994"),
        "link": ("∞", "#1976d2"),
        "file": ("∞", "#1976d2"),
    }

    def __init__(self, owner: "NodeItem", entry: dict, width: float) -> None:
        super().__init__(0.0, 0.0, max(80.0, width), 20.0, owner)
        self.owner = owner
        self.entry = entry
        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)

        kind = owner.attachment_kind(entry)
        icon, color = self.ICONS.get(kind, self.ICONS["link"])
        self.icon_item = QGraphicsSimpleTextItem(icon, self)
        icon_font = QFont(QApplication.font())
        icon_font.setPointSize(7 if kind in {"pdf", "python"} else 9)
        icon_font.setBold(kind in {"pdf", "python"})
        self.icon_item.setFont(icon_font)
        self.icon_item.setBrush(QColor(color))
        self.icon_item.setAcceptedMouseButtons(Qt.NoButton)
        self.icon_item.setPos(2.0, 1.0)

        self.label_item = QGraphicsSimpleTextItem(str(entry.get("label", "Anhang")), self)
        label_font = QFont(QApplication.font())
        label_font.setPointSize(8)
        self.label_item.setFont(label_font)
        self.label_item.setBrush(QColor("#202020"))
        self.label_item.setAcceptedMouseButtons(Qt.NoButton)
        self.label_item.setPos(28.0, 1.0)

    def hoverEnterEvent(self, event) -> None:
        self.label_item.setBrush(QColor("#1565c0"))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.label_item.setBrush(QColor("#202020"))
        super().hoverLeaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.owner.open_attachment(self.entry)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.owner.open_attachment(self.entry)
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        open_action = menu.addAction("Öffnen")
        remove_action = menu.addAction("Anhang entfernen")
        chosen = menu.exec(event.screenPos())
        if chosen is open_action:
            self.owner.open_attachment(self.entry)
        elif chosen is remove_action:
            self.owner.remove_attachment(str(self.entry.get("id", "")))
        event.accept()


class NodeItem(QGraphicsRectItem):
    def __init__(self, object_id: str, model: ProjectModel, scene_owner: "MapScene") -> None:
        self.object_id = object_id
        self.model = model
        self.scene_owner = scene_owner
        state = model.active_map["object_states"][object_id]
        super().__init__(0, 0, state.get("width", 180), state.get("height", 54))

        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )

        # Diese Container muessen vor apply_style() existieren, weil
        # apply_style() bereits die Position der Austrittsgriffe aktualisiert.
        self.relations: list[RelationItem] = []
        self.branch_exit_handles: dict[str, BranchExitHandle] = {}

        self.apply_style()
        self._drag_start_pos = QPointF(state["x"], state["y"])
        self.setAcceptHoverEvents(True)
        self.setAcceptDrops(True)
        self.setPos(state["x"], state["y"])

        self.symbol_item = QGraphicsSimpleTextItem("", self)
        self.symbol_item.setAcceptedMouseButtons(Qt.NoButton)

        title = model.data["objects"][object_id].get("title", "Knoten")
        self.label = EditableNodeText(self, title)
        # Datei-Drops gehoeren zum Knoten und duerfen nicht als URI-Text
        # in den Knotentitel eingefuegt werden.
        self.label.setAcceptDrops(False)

        self.status_item = QGraphicsSimpleTextItem("", self)
        self.status_item.setAcceptedMouseButtons(Qt.NoButton)
        self.resize_handle = NodeResizeHandle(self)
        self.note_indicator = QGraphicsPixmapItem(_make_detail_icon("note"), self)
        self.note_indicator.setAcceptedMouseButtons(Qt.NoButton)
        self.attachment_indicator = QGraphicsPixmapItem(_make_detail_icon("attachment"), self)
        self.attachment_indicator.setAcceptedMouseButtons(Qt.NoButton)
        self.attachment_count_indicator = QGraphicsSimpleTextItem("", self)
        self.attachment_count_indicator.setAcceptedMouseButtons(Qt.NoButton)
        indicator_font = self.attachment_count_indicator.font()
        indicator_font.setPointSize(8)
        self.attachment_count_indicator.setFont(indicator_font)
        self.attachment_count_indicator.setBrush(QBrush(QColor("#667085")))
        self._note_indicator_rect = QRectF()
        self._attachment_indicator_rect = QRectF()
        self.attachment_items: list[AttachmentRowItem] = []
        self.attachment_divider = QGraphicsRectItem(self)
        self.attachment_divider.setPen(QPen(QColor("#cfd8e3"), 1.0, Qt.DashLine))
        self.attachment_divider.setBrush(Qt.NoBrush)
        self.attachment_divider.setAcceptedMouseButtons(Qt.NoButton)
        self.refresh_content_markers()
        self.refresh_attachments()
        self.resize_to_document()

        self.refresh_badge()

    STATUS_SYMBOLS = {
        "working": "◐",
        "waiting": "⏳",
        "done": "✓",
    }
    STATUS_ORDER = ("", "working", "waiting", "done")

    def underline_y(self) -> float:
        """Y-Position der Unterstreichung im lokalen Knotensystem.

        Die Methode muss auch während des schrittweisen Aufbaus eines NodeItem
        sicher funktionieren, wenn Text- oder Statusobjekte noch nicht existieren.
        """
        rect = self.rect()
        fallback = max(rect.top() + 1.0, rect.bottom() - 4.0)

        label = getattr(self, "label", None)
        if not _qt_valid(label):
            return fallback

        try:
            text_bottom = float(label.pos().y() + label.boundingRect().bottom())
        except (RuntimeError, TypeError, AttributeError):
            return fallback

        # Die Linie liegt knapp unter dem Text, bleibt aber sicher im Knoten.
        return min(fallback, max(rect.top() + 12.0, text_bottom + 2.0))

    def _content_x(self) -> float:
        """Linke Textposition einschließlich sichtbarer Typ-/Statussymbole."""
        x = 10.0
        gap = 8.0
        symbol_item = getattr(self, "symbol_item", None)
        if _qt_valid(symbol_item) and symbol_item.text():
            x += max(18.0, symbol_item.boundingRect().width()) + gap
        status_item = getattr(self, "status_item", None)
        if _qt_valid(status_item) and status_item.text():
            x += max(18.0, status_item.boundingRect().width()) + gap
        return x

    def resize_to_document(self, force_layout: bool = False) -> None:
        """Leitet die Knotenhöhe ausschließlich aus dem QTextDocument ab."""
        if not hasattr(self, "label"):
            return

        state = self.model.active_map["object_states"][self.object_id]
        current_width = max(120.0, float(state.get("width", 180.0)))
        text_x = self._content_x()
        text_width = max(70.0, current_width - text_x - 12.0)

        # Zuerst die Breite festlegen, erst danach die resultierende Höhe lesen.
        # Andernfalls wird die Höhe noch für die vorherige Zeilenbreite ermittelt.
        if abs(self.label.textWidth() - text_width) > 0.25:
            self.label.setTextWidth(text_width)

        layout_size = self.label.document().documentLayout().documentSize()
        document_height = max(22.0, float(layout_size.height()))
        # Anhänge liegen optisch unterhalb des Knotenrahmens und vergrößern
        # deshalb nicht mehr die eigentliche Knotenhöhe.
        content_height = document_height + 20.0
        if state.get("size_mode", "auto") == "manual":
            new_height = max(54.0, float(state.get("height", 54.0)), content_height)
        else:
            new_height = max(54.0, content_height)

        rect_changed = (
            abs(self.rect().width() - current_width) > 0.5
            or abs(self.rect().height() - new_height) > 0.5
        )
        state_changed = (
            abs(float(state.get("width", 180.0)) - current_width) > 0.5
            or abs(float(state.get("height", 54.0)) - new_height) > 0.5
        )

        if rect_changed:
            self.prepareGeometryChange()
            self.setRect(0.0, 0.0, current_width, new_height)
        if state_changed:
            self.model.set_node_size(self.object_id, current_width, new_height)

        self.update_content_positions()
        self.update_badge_position()
        self.update()
        for relation in getattr(self, "relations", []):
            relation.update_path()

    def update_content_positions(self) -> None:
        """Positioniert mehrzeiligen Text stabil am oberen Innenrand."""
        x = 10.0
        gap = 8.0
        top = 10.0

        symbol_item = getattr(self, "symbol_item", None)
        if _qt_valid(symbol_item):
            symbol_rect = symbol_item.boundingRect()
            symbol_item.setPos(x, top + 1.0)
            if symbol_item.text():
                x += max(18.0, symbol_rect.width()) + gap

        status_item = getattr(self, "status_item", None)
        if _qt_valid(status_item):
            status_rect = status_item.boundingRect()
            status_item.setPos(x, top + 1.0)
            self._status_slot_x = x
            if status_item.text():
                x += max(18.0, status_rect.width()) + gap

        label = getattr(self, "label", None)
        if _qt_valid(label):
            text_width = max(70.0, self.rect().width() - x - 12.0)
            if abs(label.textWidth() - text_width) > 0.25:
                label.setTextWidth(text_width)
            label.setPos(x, top)

        attachment_items = getattr(self, "attachment_items", [])
        divider = getattr(self, "attachment_divider", None)
        if attachment_items:
            # Die Linkliste steht bewusst außerhalb und unterhalb des Rahmens.
            # So bleibt der Knoten kompakt und entspricht der vorgesehenen Skizze.
            section_top = self.rect().bottom() + 7.0
            if _qt_valid(divider):
                divider.setVisible(True)
                divider.setRect(0.0, 0.0, max(20.0, self.rect().width() - 20.0), 0.0)
                divider.setPos(10.0, section_top)
            for index, item in enumerate(attachment_items):
                if _qt_valid(item):
                    item.setRect(0.0, 0.0, max(80.0, self.rect().width() - 20.0), 20.0)
                    item.setPos(10.0, section_top + 7.0 + index * 22.0)
        elif _qt_valid(divider):
            divider.setVisible(False)

        note_icon = getattr(self, "note_indicator", None)
        attach_icon = getattr(self, "attachment_indicator", None)
        count_item = getattr(self, "attachment_count_indicator", None)
        note_visible = bool(self.model.note(self.object_id).strip())
        attachment_count = len(self.model.attachments(self.object_id))
        y = max(4.0, self.rect().bottom() - 20.0)
        right = self.rect().right() - 9.0
        if _qt_valid(count_item):
            count_item.setText(str(attachment_count) if attachment_count else "")
            count_item.setVisible(attachment_count > 0)
        count_width = count_item.boundingRect().width() if _qt_valid(count_item) and attachment_count else 0.0
        if _qt_valid(attach_icon):
            attach_icon.setVisible(attachment_count > 0)
            ax = right - count_width - (18.0 if attachment_count else 0.0)
            attach_icon.setPos(ax, y)
            if _qt_valid(count_item):
                count_item.setPos(ax + 16.0, y - 1.0)
            self._attachment_indicator_rect = QRectF(ax - 3.0, y - 3.0, 22.0 + count_width, 22.0) if attachment_count else QRectF()
            right = ax - 7.0
        if _qt_valid(note_icon):
            note_icon.setVisible(note_visible)
            nx = right - 15.0
            note_icon.setPos(nx, y)
            self._note_indicator_rect = QRectF(nx - 3.0, y - 3.0, 21.0, 22.0) if note_visible else QRectF()

        resize_handle = getattr(self, "resize_handle", None)
        if _qt_valid(resize_handle):
            resize_handle.setPos(self.rect().bottomRight())

    def minimum_content_height(self, width: float) -> float:
        """Minimale Höhe, bei der der vollständige Inhalt im Rahmen bleibt."""
        text_x = self._content_x()
        text_width = max(70.0, float(width) - text_x - 12.0)
        if abs(self.label.textWidth() - text_width) > 0.25:
            self.label.setTextWidth(text_width)
        layout_size = self.label.document().documentLayout().documentSize()
        # Anhänge befinden sich außerhalb des Rahmens und zählen nicht zur
        # manuellen Mindesthöhe des Knotens.
        return max(54.0, float(layout_size.height()) + 20.0)

    def resize_manually(self, width: float, height: float) -> None:
        """Ändert die Knotengröße live über den Ziehpunkt unten rechts."""
        width = max(120.0, float(width))
        height = max(self.minimum_content_height(width), float(height))
        self.prepareGeometryChange()
        self.setRect(0.0, 0.0, width, height)
        self.model.set_node_size(self.object_id, width, height)
        self.model.active_map["object_states"][self.object_id]["size_mode"] = "manual"
        self.update_content_positions()
        self.update_badge_position()
        self.update()
        for relation in getattr(self, "relations", []):
            relation.update_path()

    def _status_hit_rect(self) -> QRectF:
        return QRectF(getattr(self, "_status_slot_x", 34.0) - 4.0, 5.0, 28.0, self.rect().height() - 10.0)

    def cycle_status(self) -> None:
        current = self.model.status(self.object_id)
        try:
            index = self.STATUS_ORDER.index(current)
        except ValueError:
            index = 0
        new_status = self.STATUS_ORDER[(index + 1) % len(self.STATUS_ORDER)]
        self.scene_owner.push_undo()
        self.model.set_status(self.object_id, new_status)
        self.refresh_content_markers()
        self.scene_owner.set_active_node(self.object_id)

    def refresh_content_markers(self) -> None:
        if hasattr(self, "symbol_item"):
            self.symbol_item.setText(self.model.symbol(self.object_id))
        if hasattr(self, "status_item"):
            self.status_item.setText(
                self.STATUS_SYMBOLS.get(self.model.status(self.object_id), "")
            )
        self.update_content_positions()

    @staticmethod
    def attachment_kind(entry: dict) -> str:
        kind = str(entry.get("type", "file"))
        target = str(entry.get("target", ""))
        if kind == "url" or target.lower().startswith(("http://", "https://")):
            return "url"
        path = Path(target)
        if kind == "folder" or path.is_dir():
            return "folder"
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"}:
            return "image"
        if suffix in {".py", ".pyw"}:
            return "python"
        return "link"

    def refresh_details_indicator(self) -> None:
        """Aktualisiert die klickbaren Notiz- und Anhangssymbole."""
        self.update_content_positions()
        self.update()

    def refresh_attachments(self) -> None:
        # V1.5: Die ausfuehrliche Liste steht im Knotendetails-Dock.
        # Auf der Map bleibt nur ein kleiner, unaufdringlicher Hinweis.
        for item in getattr(self, "attachment_items", []):
            if _qt_valid(item):
                item.setParentItem(None)
                if item.scene() is not None:
                    item.scene().removeItem(item)
        self.attachment_items = []
        divider = getattr(self, "attachment_divider", None)
        if _qt_valid(divider):
            divider.setVisible(False)
        self.refresh_details_indicator()

    def add_file_attachments(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(None, "Dateien anhängen")
        if not paths:
            return
        self.scene_owner.push_undo()
        for path in paths:
            p = Path(path)
            kind = "folder" if p.is_dir() else "file"
            self.model.add_attachment(self.object_id, kind, str(p), p.name or str(p))
        self.refresh_attachments()
        self.resize_to_document()
        self.scene_owner.set_active_node(self.object_id)

    def add_folder_attachment(self) -> None:
        path = QFileDialog.getExistingDirectory(None, "Ordner anhängen")
        if not path:
            return
        self.scene_owner.push_undo()
        p = Path(path)
        self.model.add_attachment(self.object_id, "folder", str(p), p.name or str(p))
        self.refresh_attachments()
        self.resize_to_document()

    def add_url_attachment(self) -> None:
        url, ok = QInputDialog.getText(None, "Webadresse hinzufügen", "URL:")
        url = url.strip()
        if not ok or not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        label, ok_label = QInputDialog.getText(None, "Bezeichnung", "Anzeigename:", text=url)
        if not ok_label:
            return
        self.scene_owner.push_undo()
        self.model.add_attachment(self.object_id, "url", url, label.strip() or url)
        self.refresh_attachments()
        self.resize_to_document()

    def open_attachment(self, entry: dict) -> None:
        target = str(entry.get("target", ""))
        if not target:
            return
        if self.attachment_kind(entry) == "url":
            QDesktopServices.openUrl(QUrl(target))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target).expanduser())))

    def remove_attachment(self, attachment_id: str) -> None:
        if not attachment_id:
            return
        self.scene_owner.push_undo()
        if self.model.remove_attachment(self.object_id, attachment_id):
            self.refresh_attachments()
            state = self.model.active_map["object_states"][self.object_id]
            state["height"] = self.minimum_content_height(float(state.get("width", 180.0)))
            self.resize_to_document()

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        added = False
        self.scene_owner.push_undo()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    p = Path(url.toLocalFile())
                    self.model.add_attachment(self.object_id, "folder" if p.is_dir() else "file", str(p), p.name or str(p))
                    added = True
                elif url.isValid():
                    target = url.toString()
                    self.model.add_attachment(self.object_id, "url", target, target)
                    added = True
        elif mime.hasText():
            text = mime.text().strip()
            if text.lower().startswith(("http://", "https://")):
                self.model.add_attachment(self.object_id, "url", text, text)
                added = True
        if added:
            self.refresh_attachments()
            self.resize_to_document()
            event.acceptProposedAction()
        else:
            event.ignore()

    def apply_style(self) -> None:
        state = self.model.active_map["object_states"][self.object_id]
        shape = state.get("shape", "rounded")
        fill = QColor(state.get("fill_color", "#f7f7f7"))
        border = QColor(state.get("border_color", "#4f5d75"))
        border_width = float(state.get("border_width", 1.5))

        if shape in {"none", "underline"}:
            self.setBrush(Qt.NoBrush)
            self.setPen(Qt.NoPen if shape == "none" else QPen(border, border_width))
        else:
            self.setBrush(fill)
            self.setPen(QPen(border, border_width))

        if hasattr(self, "label"):
            text_color = QColor(state.get("text_color", "#202020"))
            self.label.setDefaultTextColor(text_color)
            if hasattr(self, "symbol_item"):
                self.symbol_item.setBrush(text_color)
            if hasattr(self, "status_item"):
                self.status_item.setBrush(text_color)
        self.refresh_content_markers()
        self.update_badge_position()
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        state = self.model.active_map["object_states"][self.object_id]
        shape = state.get("shape", "rounded")

        if shape == "none":
            if self.isSelected():
                painter.setPen(QPen(QColor("#2563eb"), 2.0, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(self.rect(), 6.0, 6.0)
            return

        if shape == "underline":
            pen = QPen(QColor(state.get("border_color", "#4f5d75")), float(state.get("border_width", 1.5)))
            if self.isSelected():
                pen = QPen(QColor("#2563eb"), max(3.0, pen.widthF()))
            painter.setPen(pen)
            y = self.underline_y()
            painter.drawLine(QPointF(self.rect().left(), y), QPointF(self.rect().right(), y))
            if self.isSelected():
                painter.setPen(QPen(QColor("#2563eb"), 1.5, Qt.DashLine))
                painter.drawRoundedRect(self.rect(), 4.0, 4.0)
            return

        pen = self.pen()
        brush = self.brush()
        if self.isSelected():
            pen = QPen(QColor("#2563eb"), max(3.0, pen.widthF()))
            selected_fill = QColor(brush.color())
            if selected_fill.isValid():
                selected_fill = selected_fill.lighter(118)
                brush = selected_fill

        painter.setPen(pen)
        painter.setBrush(brush)

        if shape == "rounded":
            radius = float(state.get("corner_radius", 12.0))
            painter.drawRoundedRect(self.rect(), radius, radius)
        else:
            painter.drawRect(self.rect())


    def _outgoing_tree_relations(self, branch_type: int | None = None) -> list[tuple[str, dict]]:
        result = []
        for relation_id, relation in self.model.data["relations"].items():
            if relation.get("type") != "tree":
                continue
            if relation.get("source_id") != self.object_id:
                continue
            if branch_type is not None and int(relation.get("branch_type", 1)) != int(branch_type):
                continue
            result.append((relation_id, relation))
        return result

    def relation_exit_point(self, relation_id: str) -> QPointF:
        """Austrittspunkt eines konkreten Astes im lokalen Knotensystem."""
        relation = self.model.data["relations"].get(relation_id, {})
        branch_type = int(relation.get("branch_type", 1))
        state = self.model.active_map["object_states"][self.object_id]
        rect = self.rect()

        if state.get("shape", "rounded") == "underline" and branch_type in {1, 2}:
            return QPointF(rect.right() if branch_type == 1 else rect.left(), self.underline_y())

        siblings = self._outgoing_tree_relations(branch_type)
        states = self.model.active_map["object_states"]
        if branch_type in {3, 4}:
            siblings.sort(key=lambda item: float(states.get(item[1].get("target_id"), {}).get("x", 0.0)))
        else:
            siblings.sort(key=lambda item: float(states.get(item[1].get("target_id"), {}).get("y", 0.0)))
        ids = [item[0] for item in siblings]
        try:
            index = ids.index(relation_id)
        except ValueError:
            index = 0
        count = len(siblings)
        margin = min(12.0, max(6.0, min(rect.width(), rect.height()) * 0.18))
        target_state = states.get(relation.get("target_id"), {})

        if branch_type in {3, 4}:
            minimum, maximum = rect.left() + margin, rect.right() - margin
            if count <= 1:
                target_center = float(target_state.get("x", 0.0)) + float(target_state.get("width", 180.0)) / 2.0
                coordinate = max(minimum, min(maximum, target_center - self.scenePos().x()))
            else:
                coordinate = minimum + (maximum - minimum) * index / (count - 1)
            return QPointF(coordinate, rect.bottom() if branch_type == 3 else rect.top())

        minimum, maximum = rect.top() + margin, rect.bottom() - margin
        if count <= 1:
            target_center = float(target_state.get("y", 0.0)) + float(target_state.get("height", 54.0)) / 2.0
            coordinate = max(minimum, min(maximum, target_center - self.scenePos().y()))
        else:
            coordinate = minimum + (maximum - minimum) * index / (count - 1)
        return QPointF(rect.left() if branch_type == 2 else rect.right(), coordinate)

    def update_badge_position(self) -> None:
        for relation_id, handle in self.branch_exit_handles.items():
            handle.setPos(self.relation_exit_point(relation_id))

    def refresh_badge(self) -> None:
        current_ids = {relation_id for relation_id, _ in self._outgoing_tree_relations()}
        for relation_id in list(self.branch_exit_handles):
            if relation_id not in current_ids:
                handle = self.branch_exit_handles.pop(relation_id)
                handle.setParentItem(None)
                if handle.scene() is not None:
                    handle.scene().removeItem(handle)
        for relation_id in current_ids:
            if relation_id not in self.branch_exit_handles:
                self.branch_exit_handles[relation_id] = BranchExitHandle(self, relation_id)
        self.update_badge_position()
        for handle in self.branch_exit_handles.values():
            handle.update()
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            pos = value
            self.model.set_position(self.object_id, pos.x(), pos.y())

            refreshed_groups: set[tuple[int, int]] = set()
            for relation in getattr(self, "relations", []):
                if relation.source is self:
                    key = (id(relation.source), int(relation.branch_type))
                    if key not in refreshed_groups:
                        relation.update_source_group()
                        refreshed_groups.add(key)
                else:
                    # Wird ein Kind verschoben, kann sich auch die Reihenfolge
                    # und damit die Austrittsverteilung seiner Geschwister ändern.
                    key = (id(relation.source), int(relation.branch_type))
                    if key not in refreshed_groups:
                        relation.update_source_group()
                        refreshed_groups.add(key)
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            if hasattr(self, "resize_handle"):
                self.resize_handle.setVisible(bool(value))
                self.resize_handle.setPos(self.rect().bottomRight())
            self.update()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        self.label.begin_edit()
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if getattr(self, "_note_indicator_rect", QRectF()).contains(event.pos()):
                self.scene_owner.request_details(self.object_id, "note")
                event.accept()
                return
            if getattr(self, "_attachment_indicator_rect", QRectF()).contains(event.pos()):
                self.scene_owner.request_details(self.object_id, "attachments")
                event.accept()
                return
        if (
            event.button() == Qt.LeftButton
            and self._status_hit_rect().contains(event.pos())
        ):
            self.cycle_status()
            event.accept()
            return

        self.scene_owner.prepare_selection_click(
            self,
            event.modifiers(),
            node_id=self.object_id,
        )
        self._drag_start_pos = self.pos()
        self._drag_undo_snapshot = self.scene_owner.make_snapshot()
        self.scene_owner.begin_group_drag(self)
        super().mousePressEvent(event)
        self.scene_owner.begin_reparent_preview(self)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        self.scene_owner.update_reparent_preview(self)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self.scene_owner.finish_reparent_preview(
            self,
            getattr(self, "_drag_undo_snapshot", None),
        )

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()

        add_child_action = QAction("Kindknoten anlegen", menu)
        add_child_action.triggered.connect(self.add_child)
        menu.addAction(add_child_action)

        rename_action = QAction("Umbenennen", menu)
        rename_action.triggered.connect(self.label.begin_edit)
        menu.addAction(rename_action)

        attachment_menu = menu.addMenu("Hyperlink / Anhang hinzufügen")
        add_file_action = attachment_menu.addAction("Datei(en) auswählen …")
        add_file_action.triggered.connect(self.add_file_attachments)
        add_folder_action = attachment_menu.addAction("Ordner auswählen …")
        add_folder_action.triggered.connect(self.add_folder_attachment)
        add_url_action = attachment_menu.addAction("Webadresse eingeben …")
        add_url_action.triggered.connect(self.add_url_attachment)

        layout_menu = menu.addMenu("Ast wächst nach")
        state = self.model.active_map["object_states"][self.object_id]
        current_branch_type = int(state.get("default_branch_type", 1))
        branch_options = [("Rechts", 1), ("Links", 2)]
        if state.get("shape", "rounded") != "underline":
            branch_options.extend([("Unten", 3), ("Oben", 4)])
        for text, branch_type in branch_options:
            action = QAction(text, layout_menu)
            action.setCheckable(True)
            action.setChecked(current_branch_type == branch_type)
            action.triggered.connect(
                lambda checked=False, value=branch_type: self.set_default_branch_type(value)
            )
            layout_menu.addAction(action)

        role_menu = menu.addMenu("Inhaltsart")
        current_role = self.model.content_role(self.object_id)
        for text, role in (
            ("Thema", "topic"),
            ("Abschnitt", "section"),
            ("Aufzählung", "list_item"),
            ("Notiz", "note"),
            ("Verweis", "reference"),
        ):
            action = QAction(text, role_menu)
            action.setCheckable(True)
            action.setChecked(current_role == role)
            action.triggered.connect(
                lambda checked=False, value=role: self.set_content_role(value)
            )
            role_menu.addAction(action)

        shape_menu = menu.addMenu("Knotenform")
        for text, shape in (
            ("Ohne Rahmen", "none"),
            ("Unterstrichen", "underline"),
            ("Rechteck", "rect"),
            ("Runde Ecken", "rounded"),
        ):
            action = QAction(text, shape_menu)
            action.setCheckable(True)
            action.setChecked(
                self.model.active_map["object_states"][self.object_id].get(
                    "shape", "rounded"
                ) == shape
            )
            action.triggered.connect(
                lambda checked=False, value=shape: self.set_shape(value)
            )
            shape_menu.addAction(action)

        color_menu = menu.addMenu("Farben")
        fill_action = QAction("Füllfarbe …", color_menu)
        fill_action.triggered.connect(lambda: self.choose_color("fill_color"))
        color_menu.addAction(fill_action)

        border_action = QAction("Rahmenfarbe …", color_menu)
        border_action.triggered.connect(lambda: self.choose_color("border_color"))
        color_menu.addAction(border_action)

        text_action = QAction("Textfarbe …", color_menu)
        text_action.triggered.connect(lambda: self.choose_color("text_color"))
        color_menu.addAction(text_action)

        size_menu = menu.addMenu("Größe")
        state = self.model.active_map["object_states"][self.object_id]
        current_w = float(state.get("width", 180.0))
        current_h = float(state.get("height", 54.0))

        for text, width in (
            ("Schmal (140)", 140.0),
            ("Standard (180)", 180.0),
            ("Breit (240)", 240.0),
            ("Sehr breit (320)", 320.0),
        ):
            action = QAction(text, size_menu)
            action.triggered.connect(
                lambda checked=False, value=width: self.set_size(value, current_h)
            )
            size_menu.addAction(action)

        size_menu.addSeparator()
        for text, height in (
            ("Niedrig (42)", 42.0),
            ("Standardhöhe (54)", 54.0),
            ("Hoch (72)", 72.0),
            ("Sehr hoch (100)", 100.0),
        ):
            action = QAction(text, size_menu)
            action.triggered.connect(
                lambda checked=False, value=height: self.set_size(current_w, value)
            )
            size_menu.addAction(action)

        detach_action = QAction("Freier Knoten (Asttyp 0)", menu)
        detach_action.setEnabled(self.model.tree_parent(self.object_id) is not None)
        detach_action.triggered.connect(self.detach_from_parent)
        menu.addAction(detach_action)

        collapse_action = QAction("Ein-/Ausklappen", menu)
        collapse_action.triggered.connect(self.toggle_collapsed)
        menu.addAction(collapse_action)

        menu.addSeparator()

        delete_action = QAction("Knoten löschen", menu)
        delete_action.triggered.connect(self.delete_node)
        menu.addAction(delete_action)

        menu.exec(event.screenPos())

    def set_content_role(self, role: str) -> None:
        self.scene_owner.push_undo()
        self.model.set_content_role(self.object_id, role)

        # Die Inhaltsart gehört zum Objekt. Diese Darstellung ist nur
        # eine Vorgabe für die aktuelle Ansicht und kann danach geändert werden.
        if role == "list_item":
            self.model.set_node_shape(self.object_id, "underline")
        elif role == "reference":
            self.model.set_node_shape(self.object_id, "rounded")
            self.model.set_node_color(self.object_id, "fill_color", "#ffe8d6")
            self.model.set_node_color(self.object_id, "border_color", "#e05a00")
        elif role == "note":
            self.model.set_node_shape(self.object_id, "rect")
            self.model.set_node_color(self.object_id, "fill_color", "#fff8c5")
        elif self.model.active_map["object_states"][self.object_id].get("shape") in {"none", "underline"}:
            self.model.set_node_shape(self.object_id, "rounded")

        self.scene_owner.rebuild()
        item = self.scene_owner.node_items.get(self.object_id)
        if item is not None:
            item.setSelected(True)
            self.scene_owner.set_active_node(self.object_id)

    def set_default_branch_type(self, branch_type: int) -> None:
        self.scene_owner.push_undo()
        self.model.set_default_branch_type(self.object_id, branch_type)
        self.scene_owner.rebuild()
        item = self.scene_owner.node_items.get(self.object_id)
        if item is not None:
            item.setSelected(True)
            self.scene_owner.set_active_node(self.object_id)

    def set_shape(self, shape: str) -> None:
        self.scene_owner.push_undo()
        self.model.set_node_shape(self.object_id, shape)
        self.apply_style()

    def choose_color(self, key: str) -> None:
        state = self.model.active_map["object_states"][self.object_id]
        current = QColor(state.get(key, "#ffffff"))
        color = QColorDialog.getColor(current, None, "Farbe auswählen")
        if not color.isValid():
            return
        self.scene_owner.push_undo()
        self.model.set_node_color(self.object_id, key, color.name())
        self.apply_style()

    def set_size(self, width: float, height: float) -> None:
        self.scene_owner.push_undo()
        self.model.set_node_size(self.object_id, width, height)
        self.scene_owner.rebuild()
        item = self.scene_owner.node_items.get(self.object_id)
        if item is not None:
            item.setSelected(True)

    def detach_from_parent(self) -> None:
        snapshot = self.scene_owner.make_snapshot()
        if self.model.detach_from_parent(self.object_id):
            self.scene_owner.commit_snapshot(snapshot)
            self.scene_owner.set_active_node(self.object_id)
            self.scene_owner.rebuild()
            item = self.scene_owner.node_items.get(self.object_id)
            if item is not None:
                item.setSelected(True)

    def add_child(self) -> None:
        self.scene_owner.push_undo()
        parent_id = self.object_id
        branch_type = self.model.effective_child_branch_type(parent_id)
        template_child_id = self.model.last_child_id(
            parent_id,
            branch_type=branch_type,
        )

        x, y = self.model.next_child_position(parent_id, branch_type=branch_type)
        child_id = self.model.add_object("Neuer Knoten", x, y)
        self.model.add_relation(
            parent_id,
            child_id,
            "tree",
            branch_type=branch_type,
        )
        if template_child_id is not None:
            # Weitere Kinder übernehmen die Darstellung des letzten
            # Geschwisterknotens auf derselben Astseite.
            self.model.apply_child_template(
                template_child_id,
                child_id,
            )
        else:
            # Das erste Kind übernimmt die Darstellung seines Elternknotens.
            # Dadurch bleibt z. B. ein unterstrichener Ast unterstrichen.
            self.model.apply_visual_template(parent_id, child_id)
        self.model.arrange_hierarchy_from(parent_id)
        self.scene_owner.rebuild()

        parent_item = self.scene_owner.node_items.get(parent_id)
        child_item = self.scene_owner.node_items.get(child_id)
        if parent_item is not None:
            self.scene_owner.clearSelection()
            parent_item.setSelected(True)
            self.scene_owner.set_active_node(parent_id)

        # Der neue Knoten ist sofort beschreibbar. Der Elternknoten bleibt
        # trotzdem aktiv, damit das nächste Enter wieder ein Geschwister erzeugt.
        if child_item is not None:
            child_item.label.begin_edit()

    def toggle_collapsed(self) -> None:
        self.scene_owner.push_undo()
        self.model.toggle_collapsed(self.object_id)
        self.scene_owner.rebuild()

    def toggle_relation_collapsed(self, relation_id: str) -> None:
        self.scene_owner.toggle_relation_collapsed(relation_id)

    def delete_node(self) -> None:
        self.scene_owner.delete_object_ids({self.object_id})


class MapScene(QGraphicsScene):
    def __init__(self, model: ProjectModel) -> None:
        super().__init__()
        self.model = model
        self.node_items: dict[str, NodeItem] = {}
        self.undo_stack: list[tuple[dict, str | None]] = []
        self.redo_stack: list[tuple[dict, str | None]] = []
        self.max_undo_steps = 100
        self.active_node_id: str | None = None
        self.preview_target: NodeItem | None = None
        self.reparent_enabled = False
        self.group_drag_ids: set[str] = set()
        self.group_drag_start_positions: dict[str, QPointF] = {}
        self.group_drag_anchor_id: str | None = None
        self.editing_item: EditableNodeText | None = None
        self.details_request_handler = None
        self.format_painter_handler = None
        self.format_painter_cancel_handler = None
        self.drawing_controller = DrawingController(self, model)
        self.selectionChanged.connect(self._sync_visible_selection_owner)
        self.preview_path = QGraphicsPathItem()
        self.preview_path.setZValue(-5)
        self.preview_path.setPen(QPen(QColor("#3b82f6"), 2.0))
        self.setSceneRect(QRectF(-5000, -5000, 10000, 10000))
        self.rebuild()

    def prepare_selection_click(
        self,
        owner: QGraphicsItem,
        modifiers,
        node_id: str | None = None,
    ) -> None:
        """Einheitliches Auswahlverhalten nach dem Inkscape-Prinzip.

        Ein normaler Klick macht genau das angeklickte logische Objekt aktiv.
        Strg/Shift dürfen weiterhin eine Mehrfachauswahl aufbauen.
        """
        multi = bool(modifiers & (Qt.ControlModifier | Qt.ShiftModifier))
        if not multi:
            self.clearSelection()
            owner.setSelected(True)

        # Ein alter aktiver Knoten darf nicht "hängen bleiben", wenn sichtbar
        # ein anderes Objekt (Box, Pfeil, Linie, Text, Verbindung) gewählt wird.
        self.active_node_id = node_id

    def logical_owner_for_item(self, item: QGraphicsItem | None):
        """Liefert das sichtbare logische Objekt hinter Griffen/Text-Kindern."""
        current = item
        while current is not None:
            if isinstance(current, NodeItem):
                return current
            if isinstance(current, RelationItem):
                return current
            if getattr(current, "drawing_id", None) is not None:
                return current
            current = current.parentItem()
        return None

    def _sync_visible_selection_owner(self) -> None:
        selected = self.selectedItems()
        if not selected:
            return

        node_ids = []
        has_non_node = False
        for item in selected:
            owner = self.logical_owner_for_item(item)
            if isinstance(owner, NodeItem):
                if owner.object_id not in node_ids:
                    node_ids.append(owner.object_id)
            elif owner is not None:
                has_non_node = True

        if has_non_node and not node_ids:
            self.active_node_id = None
        elif len(node_ids) == 1 and not has_non_node:
            self.active_node_id = node_ids[0]

    def request_details(self, object_id: str, section: str) -> None:
        """Leitet einen Klick auf ein Knotendetail an das Hauptfenster weiter."""
        handler = getattr(self, "details_request_handler", None)
        if callable(handler):
            handler(object_id, section)

    def make_snapshot(self) -> tuple[dict, str | None]:
        return deepcopy(self.model.data), self.active_node_id

    def commit_snapshot(
        self,
        snapshot: tuple[dict, str | None] | None,
    ) -> None:
        if snapshot is None:
            return
        if snapshot[0] == self.model.data:
            return
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > self.max_undo_steps:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def push_undo(self) -> None:
        self.undo_stack.append(self.make_snapshot())
        if len(self.undo_stack) > self.max_undo_steps:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.make_snapshot())
        data, active_node_id = self.undo_stack.pop()
        self.model.data = deepcopy(data)
        self.active_node_id = active_node_id
        self.editing_item = None
        self.rebuild()
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.make_snapshot())
        data, active_node_id = self.redo_stack.pop()
        self.model.data = deepcopy(data)
        self.active_node_id = active_node_id
        self.editing_item = None
        self.rebuild()
        return True

    def clear_history(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()

    def set_active_node(self, object_id: str | None) -> None:
        if object_id is not None and object_id not in self.model.active_map["object_states"]:
            object_id = None
        self.active_node_id = object_id

    def selected_node(self):
        # A currently selected node has priority.
        for item in self.selectedItems():
            if isinstance(item, NodeItem):
                self.active_node_id = item.object_id
                return item

        # Qt can temporarily clear the selection during rebuild/focus changes.
        # Enter must nevertheless continue to act on the last active node,
        # regardless of whether that node already has children.
        if self.active_node_id is not None:
            return self.node_items.get(self.active_node_id)
        return None

    def _select_and_edit_node(self, object_id: str) -> None:
        """Aktiviert einen Knoten und startet unmittelbar die Texteingabe."""
        item = self.node_items.get(object_id)
        if item is None:
            return
        self.clearSelection()
        item.setSelected(True)
#        item.setFocus(Qt.OtherFocusReason)
        self.active_node_id = object_id

        views = self.views()
        if views:
            views[0].setFocus(Qt.OtherFocusReason)

        item.label.begin_edit()

    def create_child_for_selected(self) -> str | None:
        parent = self.selected_node()
        if parent is None:
            return None

        self.push_undo()
        parent_id = parent.object_id
        branch_type = self.model.effective_child_branch_type(parent_id)
        template_child_id = self.model.last_child_id(
            parent_id,
            branch_type=branch_type,
        )

        x, y = self.model.next_child_position(parent_id, branch_type=branch_type)
        child_id = self.model.add_object("Neuer Unterknoten", x, y)
        self.model.add_relation(
            parent_id,
            child_id,
            "tree",
            branch_type=branch_type,
        )
        if template_child_id is not None:
            self.model.apply_child_template(template_child_id, child_id)
        else:
            self.model.apply_visual_template(parent_id, child_id)

        self.model.arrange_hierarchy_from(parent_id)
        self.rebuild()
        self._select_and_edit_node(child_id)
        return child_id

    def create_sibling_for_selected(self) -> str | None:
        current = self.selected_node()
        if current is None:
            return None

        current_id = current.object_id
        parent_id = self.model.tree_parent(current_id)
        if parent_id is None:
            # Die Wurzel besitzt keinen Geschwisterknoten.
            return None

        relation_info = self.model.tree_relation_for_child(current_id)
        branch_type = (
            int(relation_info[1].get("branch_type", 1))
            if relation_info is not None
            else self.model.effective_child_branch_type(parent_id)
        )

        self.push_undo()
        x, y = self.model.next_child_position(parent_id, branch_type=branch_type)
        sibling_id = self.model.add_object("Neuer Knoten", x, y)
        self.model.add_relation(
            parent_id,
            sibling_id,
            "tree",
            branch_type=branch_type,
        )
        # Ein Geschwister übernimmt die Darstellung des aktuellen Knotens.
        self.model.apply_child_template(current_id, sibling_id)
        self.model.arrange_hierarchy_from(parent_id)
        self.rebuild()
        self._select_and_edit_node(sibling_id)
        return sibling_id

    def _activate_node(self, object_id: str | None) -> bool:
        if object_id is None:
            return False
        item = self.node_items.get(object_id)
        if item is None:
            return False
        self.clearSelection()
        item.setSelected(True)
        item.setFocus(Qt.OtherFocusReason)
        self.active_node_id = object_id
        views = self.views()
        if views:
            views[0].ensureVisible(item, 80.0, 60.0)
        return True

    def navigate_parent(self) -> bool:
        node = self.selected_node()
        return bool(node and self._activate_node(self.model.tree_parent(node.object_id)))

    def navigate_first_child(self) -> bool:
        node = self.selected_node()
        if node is None:
            return False
        children = self.model.child_ids(node.object_id)
        return self._activate_node(children[0] if children else None)

    def navigate_sibling(self, offset: int) -> bool:
        node = self.selected_node()
        if node is None:
            return False
        parent_id = self.model.tree_parent(node.object_id)
        if parent_id is None:
            return False
        siblings = self.model.child_ids(parent_id)
        try:
            index = siblings.index(node.object_id)
        except ValueError:
            return False
        target_index = index + offset
        if not 0 <= target_index < len(siblings):
            return False
        return self._activate_node(siblings[target_index])

    def delete_object_ids(self, object_ids: set[str]) -> bool:
        object_ids = {
            object_id for object_id in object_ids
            if object_id in self.model.active_map["object_states"]
        }
        if not object_ids:
            return False

        # Nach dem Löschen möglichst einen nicht mitgelöschten Elternknoten
        # aktivieren. Bei einer vollständigen Map-Auswahl bleibt die Szene leer.
        fallback_id = None
        for object_id in object_ids:
            parent_id = self.model.tree_parent(object_id)
            if parent_id is not None and parent_id not in object_ids:
                fallback_id = parent_id
                break

        self.push_undo()
        removed = self.model.remove_subtrees(object_ids)
        if not removed:
            return False
        self.active_node_id = fallback_id
        self.rebuild()
        if fallback_id is not None:
            self._activate_node(fallback_id)
        return True

    def delete_selected_node(self) -> bool:
        """Löscht ausschließlich tatsächlich markierte Knoten.

        active_node_id dient weiterhin der Tastaturnavigation, darf aber niemals
        als unsichtbare Lösch-Auswahl verwendet werden.
        """
        selected_ids = {
            item.object_id
            for item in self.selectedItems()
            if isinstance(item, NodeItem)
        }
        if not selected_ids:
            return False
        return self.delete_object_ids(selected_ids)

    def toggle_selected_collapsed(self) -> bool:
        node = self.selected_node()
        if node is None or not self.model.child_ids(node.object_id):
            return False
        self.push_undo()
        self.model.toggle_collapsed(node.object_id)
        object_id = node.object_id
        self.rebuild()
        self._activate_node(object_id)
        return True

    def toggle_relation_collapsed(self, relation_id: str) -> bool:
        relation = self.model.data["relations"].get(relation_id)
        if relation is None or relation.get("type") != "tree":
            return False

        was_collapsed = bool(relation.get("collapsed", False))
        target_id = relation.get("target_id")
        self.push_undo()
        self.model.toggle_relation_collapsed(relation_id)

        # Beim Öffnen bleibt die innere Geometrie des Teilbaums unverändert.
        # Nur andere sichtbare Teilbäume werden aus seinem Platz geschoben.
        if was_collapsed and target_id in self.model.active_map["object_states"]:
            protected = set(self.model.subtree_ids(target_id))
            self.make_space_around_subtree(target_id, protected)

        self.rebuild()
        source_id = relation.get("source_id")
        if source_id in self.node_items:
            self._activate_node(source_id)
        return True

    def create_node_at(self, pos: QPointF, title: str = "Neuer Knoten") -> str:
        self.push_undo()
        object_id = self.model.add_object(title, pos.x(), pos.y())
        self.rebuild()
        self._select_and_edit_node(object_id)
        return object_id

    NODE_CLIPBOARD_MIME = "application/x-mindmapper-nodes+json"

    def copy_selected_to_clipboard(self) -> bool:
        """Kopiert markierte Knoten samt Teilbäumen in die Systemzwischenablage."""
        selected = {
            item.object_id for item in self.selectedItems()
            if isinstance(item, NodeItem)
        }
        if not selected and self.active_node_id is not None:
            selected = {self.active_node_id}
        if not selected:
            return False

        # Bei Auswahl von Eltern und Kind wird der Teilbaum nur einmal kopiert.
        roots = [oid for oid in selected if self.model.tree_parent(oid) not in selected]
        copied_ids: set[str] = set()
        for root_id in roots:
            copied_ids.update(self.model.subtree_ids(root_id))

        states = self.model.active_map.get("object_states", {})
        copied_ids.intersection_update(states.keys())
        if not copied_ids:
            return False

        objects = {oid: deepcopy(self.model.data["objects"][oid]) for oid in copied_ids}
        object_states = {oid: deepcopy(states[oid]) for oid in copied_ids}
        relations = [
            deepcopy(rel) for rel in self.model.data.get("relations", {}).values()
            if rel.get("source_id") in copied_ids and rel.get("target_id") in copied_ids
        ]
        payload = {
            "version": 1,
            "roots": roots,
            "objects": objects,
            "object_states": object_states,
            "relations": relations,
        }
        mime = QMimeData()
        mime.setData(self.NODE_CLIPBOARD_MIME, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        mime.setText("MindMapper-Knoten")
        QApplication.clipboard().setMimeData(mime)
        return True

    def _paste_nodes_from_clipboard(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        if not mime.hasFormat(self.NODE_CLIPBOARD_MIME):
            return False
        try:
            payload = json.loads(bytes(mime.data(self.NODE_CLIPBOARD_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, TypeError):
            return False

        old_objects = payload.get("objects", {})
        old_states = payload.get("object_states", {})
        old_roots = payload.get("roots", [])
        if not isinstance(old_objects, dict) or not old_objects:
            return False

        self.push_undo()
        id_map = {old_id: new_id("object") for old_id in old_objects}

        xs = [float(st.get("x", 0.0)) for st in old_states.values()] or [0.0]
        ys = [float(st.get("y", 0.0)) for st in old_states.values()] or [0.0]
        source_cx = (min(xs) + max(xs)) / 2.0
        source_cy = (min(ys) + max(ys)) / 2.0
        if self.views():
            view = self.views()[0]
            target = view.mapToScene(view.viewport().rect().center())
        else:
            target = QPointF(source_cx + 40.0, source_cy + 40.0)
        dx, dy = target.x() - source_cx, target.y() - source_cy

        for old_id, obj in old_objects.items():
            new_oid = id_map[old_id]
            new_obj = deepcopy(obj)
            new_obj["id"] = new_oid
            self.model.data["objects"][new_oid] = new_obj
            state = deepcopy(old_states.get(old_id, {}))
            state["x"] = float(state.get("x", 0.0)) + dx
            state["y"] = float(state.get("y", 0.0)) + dy
            state["visible"] = True
            self.model.active_map["object_states"][new_oid] = state

        for rel in payload.get("relations", []):
            source = id_map.get(rel.get("source_id"))
            target_id = id_map.get(rel.get("target_id"))
            if source is None or target_id is None:
                continue
            new_rel = deepcopy(rel)
            rid = new_id("relation")
            new_rel.update({"id": rid, "source_id": source, "target_id": target_id})
            self.model.data["relations"][rid] = new_rel

        pasted_roots = [id_map[r] for r in old_roots if r in id_map]
        selected_parent = self.selected_node()
        if selected_parent is not None and selected_parent.object_id not in id_map.values():
            for root_id in pasted_roots:
                self.model.add_relation(selected_parent.object_id, root_id, "tree")
        elif self.model.active_map.get("root_object_id") is None and pasted_roots:
            self.model.active_map["root_object_id"] = pasted_roots[0]

        self.model.touch()
        self.rebuild()
        self.clearSelection()
        for oid in id_map.values():
            item = self.node_items.get(oid)
            if item is not None:
                item.setSelected(True)
        self.active_node_id = pasted_roots[0] if pasted_roots else next(iter(id_map.values()))
        return True

    def paste_from_clipboard(self) -> bool:
        """Fügt Rich Text, Knotenstrukturen oder Bilder passend zum Kontext ein."""
        if self.editing_item is not None:
            return self.editing_item.paste_from_clipboard()

        if self._paste_nodes_from_clipboard():
            return True

        image_html = _clipboard_image_html()
        if image_html is None:
            return False

        self.push_undo()
        if self.views():
            view = self.views()[0]
            pos = view.mapToScene(view.viewport().rect().center())
        else:
            pos = QPointF(0.0, 0.0)

        object_id = self.model.add_object("Bild", pos.x(), pos.y())
        self.model.set_rich_text(object_id, image_html, "Bild")

        # Die Breite des Bildknotens folgt zunächst dem eingebetteten Bild.
        # NodeItem.resize_to_document() begrenzt und vervollständigt die Höhe.
        state = self.model.active_map["object_states"][object_id]
        state["width"] = 720.0
        state["height"] = 80.0
        state["layout_mode"] = "manual"

        self.rebuild()
        item = self.node_items.get(object_id)
        if item is not None:
            self.clearSelection()
            item.setSelected(True)
            self.active_node_id = object_id
            item.resize_to_document()
        return True

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        transform = self.views()[0].transform() if self.views() else None
        clicked = self.itemAt(event.scenePos(), transform)

        # Zuerst immer eine laufende Texteingabe sauber beenden, wenn der Klick
        # außerhalb des aktuellen Editors liegt. Das muss VOR Zeichenwerkzeugen
        # passieren, sonst können alter Knotentext und neuer freier Text zugleich
        # im Editier-/Markierungszustand bleiben.
        if self.editing_item is not None:
            item = clicked
            inside_editor = False
            while item is not None:
                if item is self.editing_item:
                    inside_editor = True
                    break
                item = item.parentItem()

            if not inside_editor:
                previous_editor = self.editing_item
                previous_editor.finish_edit()

                # finish_edit() kann die Szene neu aufbauen; Treffer deshalb
                # anschließend mit der aktuellen Szene erneut bestimmen.
                transform = self.views()[0].transform() if self.views() else None
                clicked = self.itemAt(event.scenePos(), transform)

        # Formatpinsel hat im Einmal-Modus Vorrang vor normalen Werkzeugen.
        painter_handler = getattr(self, "format_painter_handler", None)
        if event.button() == Qt.LeftButton and callable(painter_handler):
            if clicked is not None and painter_handler(clicked):
                event.accept()
                return

        if self.drawing_controller.mouse_press(event):
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.drawing_controller.mouse_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.drawing_controller.mouse_release(event):
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.editing_item is not None:
            super().keyPressEvent(event)
            return

        # Wie in einem Zeichenprogramm: Escape beendet jedes aktive
        # Zeichenwerkzeug und kehrt zuverlässig zur Auswahl zurück.
        if event.key() == Qt.Key_Escape:
            cancel_painter = getattr(self, "format_painter_cancel_handler", None)
            if callable(cancel_painter):
                cancel_painter()
            self.drawing_controller.select_mode()
            event.accept()
            return

        if event.matches(QKeySequence.Paste):
            if self.paste_from_clipboard():
                event.accept()
                return

        if event.matches(QKeySequence.Undo):
            if self.undo():
                event.accept()
                return

        if event.matches(QKeySequence.Redo):
            if self.redo():
                event.accept()
                return

        modifiers = event.modifiers()
        ctrl_only = bool(modifiers & Qt.ControlModifier) and not bool(
            modifiers & (Qt.AltModifier | Qt.ShiftModifier | Qt.MetaModifier)
        )

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            created = (
                self.create_sibling_for_selected()
                if ctrl_only
                else self.create_child_for_selected()
            )
            if created is not None:
                event.accept()
                return

        if event.key() == Qt.Key_Tab and not modifiers:
            if self.create_child_for_selected() is not None:
                event.accept()
                return

        if event.key() == Qt.Key_F2:
            node = self.selected_node()
            if node is not None:
                node.label.begin_edit()
                event.accept()
                return

        if event.key() == Qt.Key_Delete:
            # Reihenfolge ist absichtlich strikt nach sichtbarer Auswahl:
            # Zeichnung -> Knotenverbindung -> Knoten. Keine versteckte Fallback-Auswahl.
            drawing_ids = self.drawing_controller.selected_drawing_ids()
            if drawing_ids:
                self.push_undo()
                drawings = self.model.active_map.setdefault("drawings", {})
                for drawing_id in drawing_ids:
                    drawings.pop(drawing_id, None)
                self.model.touch()
                self.rebuild()
                event.accept()
                return

            selected_relations = [
                item for item in self.selectedItems()
                if isinstance(item, RelationItem)
            ]
            if selected_relations:
                self.push_undo()
                for relation_item in selected_relations:
                    if relation_item.relation_id is not None:
                        self.model.remove_relation(relation_item.relation_id)
                self.rebuild()
                event.accept()
                return

            if self.delete_selected_node():
                event.accept()
                return

            # Nichts sichtbar ausgewählt: Entf tut nichts.
            event.accept()
            return

        navigation = {
            Qt.Key_Left: self.navigate_parent,
            Qt.Key_Right: self.navigate_first_child,
            Qt.Key_Up: lambda: self.navigate_sibling(-1),
            Qt.Key_Down: lambda: self.navigate_sibling(1),
        }
        handler = navigation.get(event.key())
        if handler is not None and handler():
            event.accept()
            return

        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # Robust: bei Doppelklick auf wirklich freie Fläche immer Knoten erzeugen.
        if self.itemAt(event.scenePos(), self.views()[0].transform()) is None:
            self.create_node_at(event.scenePos())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        if self.itemAt(event.scenePos(), self.views()[0].transform()) is None:
            menu = QMenu()
            action = QAction("Neuen Knoten hier anlegen", menu)
            action.triggered.connect(lambda: self.create_node_at(event.scenePos()))
            menu.addAction(action)
            menu.exec(event.screenPos())
            event.accept()
            return
        super().contextMenuEvent(event)

    def find_reparent_target(self, dragged: NodeItem) -> NodeItem | None:
        dragged_center = dragged.sceneBoundingRect().center()
        best_item: NodeItem | None = None
        best_distance = float("inf")

        for candidate in self.node_items.values():
            if candidate is dragged:
                continue
            if self.model.is_descendant(candidate.object_id, dragged.object_id):
                continue

            expanded = candidate.sceneBoundingRect().adjusted(-18, -18, 18, 18)
            if not expanded.contains(dragged_center):
                continue

            candidate_center = candidate.sceneBoundingRect().center()
            dx = dragged_center.x() - candidate_center.x()
            dy = dragged_center.y() - candidate_center.y()
            distance = dx * dx + dy * dy
            if distance < best_distance:
                best_distance = distance
                best_item = candidate

        return best_item

    def begin_group_drag(self, dragged: NodeItem) -> None:
        """Merkt eine Mehrfachauswahl als starren Verschiebeblock."""
        selected = [
            item for item in self.selectedItems() if isinstance(item, NodeItem)
        ]
        if dragged not in selected:
            selected = [dragged]

        self.group_drag_ids = {item.object_id for item in selected}
        self.group_drag_start_positions = {
            item.object_id: QPointF(item.pos()) for item in selected
        }
        self.group_drag_anchor_id = dragged.object_id

    def _apply_rigid_group_delta(self, dragged: NodeItem) -> set[str]:
        """Stellt sicher, dass alle markierten Knoten exakt denselben Weg zurücklegen."""
        if len(self.group_drag_ids) <= 1:
            return set()

        anchor_start = self.group_drag_start_positions.get(dragged.object_id)
        if anchor_start is None:
            return set()

        delta = dragged.pos() - anchor_start
        states = self.model.active_map["object_states"]
        moved_ids: set[str] = set()

        for object_id in self.group_drag_ids:
            start = self.group_drag_start_positions.get(object_id)
            item = self.node_items.get(object_id)
            state = states.get(object_id)
            if start is None or item is None or state is None:
                continue
            target = start + delta
            item.setPos(target)
            state["x"] = float(target.x())
            state["y"] = float(target.y())
            moved_ids.add(object_id)

        if moved_ids:
            self.model.touch()
        return moved_ids

    def begin_reparent_preview(self, dragged: NodeItem) -> None:
        self.preview_target = None
        if self.preview_path.scene() is None:
            self.addItem(self.preview_path)
        self.preview_path.setVisible(False)

        selected_nodes = [
            item for item in self.selectedItems() if isinstance(item, NodeItem)
        ]
        self.reparent_enabled = len(selected_nodes) == 1

    def _set_preview_target(self, target: NodeItem | None) -> None:
        if self.preview_target is target:
            return

        if self.preview_target is not None:
            self.preview_target.apply_style()

        self.preview_target = target

        if self.preview_target is not None:
            self.preview_target.setPen(QPen(QColor("#3b82f6"), 3.0))
            self.preview_target.update()

    def update_reparent_preview(self, dragged: NodeItem) -> None:
        if not getattr(self, "reparent_enabled", False):
            self._set_preview_target(None)
            self.preview_path.setVisible(False)
            return
        target = self.find_reparent_target(dragged)
        self._set_preview_target(target)

        if target is None:
            self.preview_path.setVisible(False)
            return

        inferred_branch_type = self.model.infer_branch_type(
            target.object_id,
            dragged.object_id,
        )
        self.preview_path.setPath(
            RelationItem.make_path(
                target.sceneBoundingRect(),
                dragged.sceneBoundingRect(),
                inferred_branch_type,
            )
        )
        self.preview_path.setVisible(True)

    def _shift_descendants_after_drag(
        self,
        dragged: NodeItem,
    ) -> set[str]:
        """
        Ein verschobener Elternknoten nimmt seinen kompletten Unterbaum mit.

        Qt verschiebt beim Ziehen zunächst nur das grafische Wurzelelement.
        Beim Loslassen wird die gleiche Verschiebung auf alle Nachfahren
        übertragen.
        """
        dx = dragged.pos().x() - dragged._drag_start_pos.x()
        dy = dragged.pos().y() - dragged._drag_start_pos.y()
        subtree_ids = set(self.model.subtree_ids(dragged.object_id))

        if abs(dx) < 0.001 and abs(dy) < 0.001:
            return subtree_ids

        states = self.model.active_map["object_states"]
        for object_id in subtree_ids:
            if object_id == dragged.object_id:
                continue
            state = states.get(object_id)
            if state is None:
                continue
            state["x"] = float(state.get("x", 0.0)) + dx
            state["y"] = float(state.get("y", 0.0)) + dy

        self.model.touch()
        return subtree_ids

    def _collision_root(
        self,
        object_id: str,
        moved_root_id: str,
    ) -> str:
        """
        Liefert den fremden Ast direkt unterhalb des gemeinsamen Vorfahren.

        Die alte Variante stieg bei fremden Knoten bis zur globalen Wurzel auf.
        Deren Teilbaum enthielt dann auch den verschobenen Knoten und durfte
        deshalb nicht bewegt werden. Effekt: Es wurde überhaupt kein Platz
        geschaffen.

        Jetzt wird nur der kollidierende Schwesterast verschoben.
        """
        moved_ancestors: set[str] = {moved_root_id}
        current = moved_root_id
        while True:
            parent_id = self.model.tree_parent(current)
            if parent_id is None:
                break
            moved_ancestors.add(parent_id)
            current = parent_id

        current = object_id
        while True:
            parent_id = self.model.tree_parent(current)
            if parent_id is None or parent_id in moved_ancestors:
                return current
            current = parent_id

    def _node_rect(self, object_id: str) -> QRectF:
        state = self.model.active_map["object_states"][object_id]
        return QRectF(
            float(state.get("x", 0.0)),
            float(state.get("y", 0.0)),
            float(state.get("width", 180.0)),
            float(state.get("height", 54.0)),
        )

    def make_space_around_subtree(
        self,
        moved_root_id: str,
        protected_ids: set[str],
        margin: float = 28.0,
    ) -> bool:
        """
        Push-and-Shove anhand echter Knotenkollisionen.

        Nicht der große Begrenzungsrahmen eines kompletten Teilbaums wird
        geprüft, sondern jeder sichtbare Knoten. Das verhindert Fehlalarme
        durch leere Flächen innerhalb eines ausgedehnten Teilbaums.
        """
        states = self.model.active_map["object_states"]
        if moved_root_id not in states:
            return False

        changed = False
        max_steps = max(40, len(states) * 12)

        for _ in range(max_steps):
            collision: tuple[str, str] | None = None

            # Geschobene Äste werden ebenfalls geschützt, sobald sie Platz
            # erhalten haben. So kann sich die Verdrängung kaskadierend
            # durch benachbarte Schwesteräste fortsetzen.
            active_ids = {
                object_id
                for object_id in protected_ids
                if object_id in states
            }

            for active_id in active_ids:
                active_rect = self._node_rect(active_id).adjusted(
                    -margin, -margin, margin, margin
                )
                for other_id in states:
                    if other_id in active_ids:
                        continue
                    if active_rect.intersects(self._node_rect(other_id)):
                        collision = (active_id, other_id)
                        break
                if collision is not None:
                    break

            if collision is None:
                break

            active_id, collision_id = collision
            shove_root = self._collision_root(
                collision_id,
                moved_root_id,
            )
            shove_ids = {
                object_id
                for object_id in self.model.subtree_ids(shove_root)
                if object_id in states
            }

            if shove_ids & protected_ids:
                # Sicherheitsnetz gegen Zyklen oder unerwartete Strukturen.
                break

            active_rect = self._node_rect(active_id)
            collision_rect = self._node_rect(collision_id)

            required_down = (
                active_rect.bottom() + margin - collision_rect.top()
            )
            required_up = (
                collision_rect.bottom() + margin - active_rect.top()
            )

            active_center_y = active_rect.center().y()
            collision_center_y = collision_rect.center().y()

            if collision_center_y >= active_center_y:
                dy = max(margin, required_down)
            else:
                dy = -max(margin, required_up)

            self.model.shift_subtree(shove_root, 0.0, dy)
            protected_ids.update(shove_ids)
            changed = True

        return changed

    def finish_reparent_preview(
        self,
        dragged: NodeItem,
        pre_drag_snapshot: tuple[dict, str | None] | None = None,
    ) -> bool:
        target = self.preview_target
        self.preview_path.setVisible(False)
        self._set_preview_target(None)

        changed = False
        if getattr(self, "reparent_enabled", False) and target is not None:
            changed = self.model.set_tree_parent(
                dragged.object_id,
                target.object_id,
            )
            if changed:
                self.model.arrange_hierarchy_from(target.object_id)

        moved = dragged.pos() != dragged._drag_start_pos
        collision_changed = False
        multi_drag = len(self.group_drag_ids) > 1

        if moved and not changed:
            if multi_drag:
                # Mehrfachauswahl ist ein starrer Block: relative Abstände und
                # die innere Struktur dürfen durch das Layout nicht verändert werden.
                protected_ids = self._apply_rigid_group_delta(dragged)
                for object_id in protected_ids:
                    self.model.set_layout_mode(object_id, "manual")

                # Nur Verbindungen aus der Auswahl nach außen dürfen ihre Seite
                # an die neue Lage anpassen. Interne Verbindungen bleiben unangetastet.
                for object_id in protected_ids:
                    parent_id = self.model.tree_parent(object_id)
                    if parent_id not in protected_ids:
                        self.model.refresh_branch_type_from_position(object_id)
            else:
                protected_ids = self._shift_descendants_after_drag(dragged)
                self.model.set_layout_mode(dragged.object_id, "manual")

                # Beim Überschreiten einer Knotenseite wechselt nur diese einzelne
                # Eltern-Kind-Verbindung automatisch ihre Anschlussseite.
                # Andere Kinder desselben Elternknotens behalten ihre Seiten.
                self.model.refresh_branch_type_from_position(dragged.object_id)

                # Der vom Benutzer verschobene Teilbaum bleibt exakt an seiner
                # neuen Position. Nur fremde, tatsächlich kollidierende Teilbäume
                # dürfen ausweichen.
                collision_changed = self.make_space_around_subtree(
                    dragged.object_id,
                    protected_ids,
                )

        if changed or moved or collision_changed:
            self.commit_snapshot(pre_drag_snapshot)

        if changed or moved or collision_changed:
            active_id = dragged.object_id
            selected_ids = set(self.group_drag_ids) if multi_drag else {active_id}
            self.rebuild()
            self.clearSelection()
            for object_id in selected_ids:
                item = self.node_items.get(object_id)
                if item is not None:
                    item.setSelected(True)
            if active_id in self.node_items:
                self.set_active_node(active_id)

        self.group_drag_ids.clear()
        self.group_drag_start_positions.clear()
        self.group_drag_anchor_id = None
        return changed

    def rebuild(self) -> None:
        # Drop Python references before QGraphicsScene.clear() destroys their
        # underlying C++ objects. This also prevents toolbar/timer callbacks
        # from using an editor belonging to the old scene contents.
        self.editing_item = None
        self.preview_target = None
        self.clear()
        self.node_items.clear()
        self.preview_target = None
        self.preview_path = QGraphicsPathItem()
        self.preview_path.setZValue(-5)
        self.preview_path.setPen(QPen(QColor("#3b82f6"), 2.0))
        self.preview_path.setVisible(False)
        self.addItem(self.preview_path)

        self.drawing_controller.set_model(self.model)
        self.drawing_controller.rebuild()

        visible_ids = self._visible_object_ids()

        for object_id, state in self.model.active_map["object_states"].items():
            if object_id in visible_ids and state.get("visible", True):
                item = NodeItem(object_id, self.model, self)
                self.addItem(item)
                self.node_items[object_id] = item

        for relation_id, relation in self.model.data["relations"].items():
            source_id = relation["source_id"]
            target_id = relation["target_id"]
            if source_id in self.node_items and target_id in self.node_items:
                self.addItem(
                    RelationItem(
                        self.node_items[source_id],
                        self.node_items[target_id],
                        int(relation.get("branch_type", 1)),
                        relation_id,
                    )
                )

        if self.active_node_id in self.node_items:
            self.node_items[self.active_node_id].setSelected(True)
        elif self.active_node_id not in self.model.active_map["object_states"]:
            self.active_node_id = None

    def _visible_object_ids(self) -> set[str]:
        states = self.model.active_map["object_states"]
        visible = set(states.keys())

        children: dict[str, list[str]] = {}
        for relation in self.model.data["relations"].values():
            if relation.get("type") == "tree":
                children.setdefault(relation["source_id"], []).append(relation["target_id"])

        def hide_descendants(parent_id: str) -> None:
            for child_id in children.get(parent_id, []):
                visible.discard(child_id)
                hide_descendants(child_id)

        # Alte Dateien mit dem früheren knotenweiten Zustand bleiben lesbar.
        for object_id, state in states.items():
            if state.get("collapsed", False):
                for relation in self.model.data["relations"].values():
                    if relation.get("type") == "tree" and relation.get("source_id") == object_id:
                        relation["collapsed"] = True
                state["collapsed"] = False

        # Jeder Austrittspunkt schaltet nur den dahinterliegenden Teilbaum.
        for relation in self.model.data["relations"].values():
            if relation.get("type") == "tree" and relation.get("collapsed", False):
                target_id = relation.get("target_id")
                if target_id in visible:
                    visible.discard(target_id)
                    hide_descendants(target_id)

        return visible
