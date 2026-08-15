from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import shiboken6


from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, QUrl
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
from .richtext_support import clipboard_image_html


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
        self.setAcceptedMouseButtons(Qt.LeftButton)
        state = owner.model.active_map["object_states"][owner.object_id]
        self.setDefaultTextColor(QColor(state.get("text_color", "#202020")))
        self._geometry_update_pending = False
        self._image_resize_in_progress = False
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

        # Beim Skalieren eines Inline-Bildes bleibt die Knotenbox während des
        # Drags bewusst stabil. Erst beim Loslassen wird sie einmal an das
        # neue Dokumentmaß angepasst. Das verhindert, dass der Resize-Griff
        # bei Bildern in einem eigenen Absatz unter der Maus "wegläuft".
        if getattr(self, "_image_resize_in_progress", False):
            return

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
        """Findet das erste eingebettete Bild mit EXAKTER Zeichenposition.

        Wichtig:
        QTextCursor.charFormat() auf einer Auswahl [pos, pos+1] kann unter
        PySide6/Qt6 bereits das Format des folgenden Zeichens melden. Genau das
        zeigte die Diagnose: gespeicherte Bildposition 22, das echte Bild lag
        aber bei 23..24.

        QTextFragment.position() liefert dagegen die tatsächliche Position des
        U+FFFC-Inline-Objekts im QTextDocument.
        """
        document = self.document()
        block = document.begin()

        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    if fmt.isImageFormat():
                        position = int(fragment.position())

                        cursor = QTextCursor(document)
                        cursor.setPosition(position)
                        cursor.setPosition(
                            position + max(1, int(fragment.length())),
                            QTextCursor.KeepAnchor,
                        )

                        # Ein Bildfragment ist normalerweise genau ein Zeichen.
                        # Falls Qt mehrere Zeichen im Fragment meldet, wird nur
                        # das erste Inline-Objekt selektiert.
                        if cursor.selectionEnd() - cursor.selectionStart() > 1:
                            cursor.setPosition(position)
                            cursor.setPosition(
                                position + 1,
                                QTextCursor.KeepAnchor,
                            )

                        return cursor, fmt.toImageFormat()

                iterator += 1
            block = block.next()

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

        # QTextLayout liefert die Cursorposition am Inline-Objekt sehr exakt,
        # die sichtbare Rasterkante des Bildes kann unter Qt jedoch noch um
        # wenige Pixel rechts/unten über diese logische Box hinausragen
        # (Antialiasing / Inline-Object-Metrik). Der Auswahlrahmen soll die
        # tatsächlich sichtbare Bildkante vollständig umfassen.
        visual_pad_right = 3.0
        visual_pad_bottom = 2.0
        return QRectF(
            x,
            y,
            width + visual_pad_right,
            height + visual_pad_bottom,
        )

    def _set_image_size_at(
        self,
        image_position: int,
        width: float,
        height: float,
    ) -> None:
        """Skaliert exakt das Bildzeichen, das beim Mouse-Press gewählt wurde.

        Während eines Drags wird nicht erneut nach dem "ersten Bild" gesucht.
        Dadurch bleibt die Referenz stabil, auch wenn Qt das Textlayout durch
        die Größenänderung neu berechnet.
        """
        document = self.document()
        count = max(0, document.characterCount() - 1)
        if image_position < 0 or image_position >= count:
            return

        cursor = QTextCursor(document)
        cursor.setPosition(int(image_position))
        cursor.setPosition(int(image_position) + 1, QTextCursor.KeepAnchor)

        fmt = cursor.charFormat()
        if not fmt.isImageFormat():
            return

        image_format = fmt.toImageFormat()
        image_format.setWidth(float(width))
        image_format.setHeight(float(height))

        # Exakt das vorhandene Bildzeichen ersetzen. Die Position stammt
        # aus QTextFragment.position() und zeigt deshalb auf das tatsächliche
        # Inline-Bildobjekt.
        cursor.beginEditBlock()
        cursor.clearSelection()
        cursor.setPosition(int(image_position))
        cursor.deleteChar()
        cursor.setPosition(int(image_position))
        cursor.insertImage(image_format)
        cursor.endEditBlock()

        document.markContentsDirty(int(image_position), 1)

        try:
            document.documentLayout().documentSize()
        except (RuntimeError, ReferenceError):
            pass

        try:
            self.update()
            self.owner.update()
            scene = self.scene()
            if scene is not None:
                scene.update()
        except (RuntimeError, ReferenceError):
            pass

    def _set_image_size(self, width: float, height: float) -> None:
        image = self._first_image()
        if image is None:
            return

        cursor, image_format = image

        # Die Zeichenposition explizit sichern und das einzelne Bildzeichen
        # anschließend neu selektieren. So funktioniert die Formatänderung
        # sowohl bei "Petrol [Bild]" als auch bei
        # "Text\\n[Bild]" zuverlässig.
        start = int(cursor.selectionStart())
        document = self.document()
        image_cursor = QTextCursor(document)
        image_cursor.setPosition(start)
        image_cursor.setPosition(start + 1, QTextCursor.KeepAnchor)

        image_format.setWidth(float(width))
        image_format.setHeight(float(height))
        image_cursor.setCharFormat(image_format)

        # Nur Bildrahmen live nachführen. Die Knotenbox wird beim Release
        # einmalig angepasst.
        self._update_image_hover()
        self.viewport_update_safe()

    def viewport_update_safe(self) -> None:
        """Fordert nur ein Neuzeichnen an, ohne die Knotengeometrie umzubauen."""
        try:
            self.update()
            owner = getattr(self, "owner", None)
            if _qt_valid(owner):
                owner.update()
        except (RuntimeError, ReferenceError):
            pass

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

            # Falls kein Bild aktiv ist, darf der normale Knotengriff wieder
            # erscheinen.
            node_handle = getattr(self.owner, "resize_handle", None)
            if _qt_valid(node_handle):
                node_handle.setVisible(bool(self.owner.isSelected()))
            return

        # WICHTIG:
        # Bei Bildern, die fast die gesamte Knotenbreite/-höhe belegen,
        # liegen Bild-Resize-Griff und Knoten-Resize-Griff praktisch exakt
        # übereinander. Der Knotengriff sitzt als direkter NodeItem-Kindknoten
        # in einer höheren Stacking-Ebene und fängt dann den Maus-Drag ab.
        #
        # Das erklärt, warum der Benzinkanister skalierbar war:
        # Sein Inline-Bildgriff liegt NICHT am unteren rechten Knoteneck.
        # Bei Motorrad/Haus/etc. überlagern sich die beiden Griffe dagegen.
        #
        # Solange das Bild aktiv ist, wird deshalb ausschließlich der
        # Bild-Resize-Griff angeboten.
        node_handle = getattr(self.owner, "resize_handle", None)
        if _qt_valid(node_handle):
            node_handle.setVisible(False)

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

        # Nach Verlassen des Bildes wieder den normalen Knotengriff zeigen,
        # sofern der Knoten ausgewählt ist.
        node_handle = getattr(self.owner, "resize_handle", None)
        if _qt_valid(node_handle):
            node_handle.setVisible(bool(self.owner.isSelected()))
            if self.owner.isSelected():
                node_handle.setPos(self.owner.rect().bottomRight())

    def mousePressEvent(self, event) -> None:
        """Inline-Bild zuerst behandeln, bevor der Klick den ganzen Knoten selektiert.

        Klick innerhalb des Bildes:
        - Bildrahmen + Resize-Griff anzeigen
        - Knoten darf selektiert bleiben, aber der Klick wird hier verbraucht
        - keine Texteingabe starten

        Klick außerhalb des Bildes:
        - normales bisheriges Verhalten
        """
        rect = self._image_rect()
        if rect is not None and rect.contains(event.pos()):
            self.owner.scene_owner.clearSelection()
            self.owner.setSelected(True)
            self.owner.scene_owner.set_active_node(self.owner.object_id)
            self._update_image_hover()
            event.accept()
            return

        super().mousePressEvent(event)

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
        image_html = clipboard_image_html()
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

        # Vertikale Verbindungen (oben/unten) liegen bewusst links vom Text.
        # Dadurch läuft der Stamm nicht mehr durch "Neuer Knoten" bzw.
        # "Neuer Unterknoten". Der Abstand ist unabhängig von der Textlänge.
        # Vertikaler Anschluss exakt am linken Beginn der Unterstreichung.
        # Dadurch gibt es am Kind keinen sichtbaren Überstand links vom Stamm.
        vertical_x = rect.left()

        if outgoing:
            if branch_type == 2:
                return QPointF(rect.left(), y)
            elif branch_type == 1:
                return QPointF(rect.right(), y)
            return QPointF(vertical_x, y)
        else:
            # Der Zielanschluss liegt immer direkt auf der sichtbaren
            # Unterstreichung.
            if branch_type == 2:
                return QPointF(rect.right(), y)
            if branch_type == 1:
                return QPointF(rect.left(), y)
            return QPointF(vertical_x, y)

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

    def _is_underline_target(self) -> bool:
        state = self.target.model.active_map["object_states"].get(
            self.target.object_id, {}
        )
        return state.get("shape", "rounded") == "underline"

    def _underline_group(self) -> list["RelationItem"]:
        """Unterstrichene Geschwister derselben Anschlussseite.

        Andere Knotentypen werden absichtlich nicht aufgenommen.
        """
        group = [
            relation
            for relation in self.source.relations
            if relation.source is self.source
            and int(relation.branch_type) == int(self.branch_type)
            and relation.isVisible()
            and relation._is_underline_target()
        ]
        if int(self.branch_type) in {1, 2}:
            group.sort(
                key=lambda relation:
                relation.target.sceneBoundingRect().center().y()
            )
        else:
            group.sort(
                key=lambda relation:
                relation.target.sceneBoundingRect().center().x()
            )
        return group

    def _underline_group_path(
        self,
        source_rect: QRectF,
        end: QPointF,
    ) -> QPainterPath | None:
        """Gemeinsamer Stamm für unterstrichene Geschwister.

        Regeln:
        - rechts: Stamm links vor der Gruppe
        - links:  Stamm rechts vor der Gruppe
        - unten:  Austritt aus der UNTERKANTE des Elternknotens, links versetzt
        - oben:   Austritt aus der OBERKANTE des Elternknotens, links versetzt
        - von dort läuft der gemeinsame Stamm senkrecht
        - keine Knotenposition wird verändert
        """
        group = self._underline_group()
        if not group:
            return None

        branch_type = int(self.branch_type)

        # Sichtbare Zielpunkte auf der Unterstreichung.
        target_points: list[QPointF] = []
        for relation in group:
            target_rect = relation.target.sceneBoundingRect()
            underline_y = relation.target.mapToScene(
                QPointF(0.0, relation.target.underline_y())
            ).y()

            if branch_type == 1:      # Gruppe rechts
                point = QPointF(target_rect.left(), underline_y)
            elif branch_type == 2:    # Gruppe links
                point = QPointF(target_rect.right(), underline_y)
            else:                     # oben / unten
                # Linke Linienkante ist stabil bei unterschiedlich langem Text.
                point = QPointF(target_rect.left(), underline_y)

            target_points.append(point)

        # --------------------------------------------------------------
        # Rechte / linke Gruppe:
        # vertikaler Stamm zwischen Eltern und Zielgruppe.
        # --------------------------------------------------------------
        if branch_type in {1, 2}:
            representative = group[(len(group) - 1) // 2]
            if representative.relation_id is not None:
                start_point = representative.source.mapToScene(
                    representative.source.relation_exit_point(
                        representative.relation_id
                    )
                )
            else:
                start_point = representative._dynamic_source_anchor(source_rect)

            if branch_type == 1:
                trunk_x = min(point.x() for point in target_points) - 28.0
                dx = max(45.0, abs(trunk_x - start_point.x()) * 0.45)
                c1 = QPointF(start_point.x() + dx, start_point.y())
                c2 = QPointF(trunk_x - dx * 0.30, start_point.y())
            else:
                trunk_x = max(point.x() for point in target_points) + 28.0
                dx = max(45.0, abs(start_point.x() - trunk_x) * 0.45)
                c1 = QPointF(start_point.x() - dx, start_point.y())
                c2 = QPointF(trunk_x + dx * 0.30, start_point.y())

            path = QPainterPath(start_point)
            path.cubicTo(c1, c2, QPointF(trunk_x, start_point.y()))
            path.lineTo(QPointF(trunk_x, end.y()))
            path.lineTo(end)
            return path

        # --------------------------------------------------------------
        # Obere / untere Gruppe:
        # Der Stamm tritt DIREKT aus Ober-/Unterkante aus.
        # Der Austrittspunkt liegt bewusst links im Elternknoten.
        # Danach läuft der Stamm senkrecht. Kein Haken, kein seitlicher Umweg.
        # --------------------------------------------------------------
        # Fester linker Anschlussbereich: deutlich vor dem Text und
        # unabhängig von unterschiedlich langen Beschriftungen.
        # Vertikaler Gruppenstamm exakt am linken Beginn der Unterstreichung.
        exit_x = source_rect.left()

        if branch_type == 3:  # unten
            start_point = QPointF(exit_x, source_rect.bottom())
        else:                 # oben
            start_point = QPointF(exit_x, source_rect.top())

        path = QPainterPath(start_point)

        # Gemeinsamer senkrechter Stamm direkt bis zur Höhe des Zieles.
        path.lineTo(QPointF(exit_x, end.y()))

        # Horizontaler Abzweig zur linken Kante der Unterstreichung.
        path.lineTo(end)
        return path

    def _kept_group_trunk_path(
        self,
        source_rect: QRectF,
        target_rect: QRectF,
    ) -> QPainterPath | None:
        """Verbindet einen ehemals unterstrichenen Knoten weiter am Gruppenstamm.

        Der kurze Abzweig allein reicht nicht: liegt das neue Rechteck unterhalb
        bzw. außerhalb der bisherigen Unterstrichen-Liste, muss der gemeinsame
        Stamm bis auf die Höhe des Rechtecks verlängert werden.
        """
        data = self._relation_data()
        if not data or not data.get("keep_group_trunk", False):
            return None

        branch_type = int(self.branch_type)

        underline_relations = [
            relation
            for relation in self.source.relations
            if relation is not self
            and relation.source is self.source
            and int(relation.branch_type) == branch_type
            and relation.isVisible()
            and relation._is_underline_target()
        ]
        if not underline_relations:
            return None

        # Sichtbare Y-Lagen der verbliebenen Unterstrichen-Einträge.
        underline_ys = [
            rel.target.mapToScene(
                QPointF(0.0, rel.target.underline_y())
            ).y()
            for rel in underline_relations
        ]
        min_y = min(underline_ys)
        max_y = max(underline_ys)

        # --------------------------------------------------------------
        # Rechte / linke Gruppe:
        # vorhandenen vertikalen Gruppenstamm bis zur neuen Rechteckhöhe
        # verlängern und dann waagerecht zum Rechteck abzweigen.
        # --------------------------------------------------------------
        if branch_type == 1:
            trunk_x = min(
                rel.target.sceneBoundingRect().left()
                for rel in underline_relations
            ) - 28.0
            end = QPointF(target_rect.left(), target_rect.center().y())

            if end.y() > max_y:
                stem_start_y = max_y
            elif end.y() < min_y:
                stem_start_y = min_y
            else:
                stem_start_y = end.y()

            path = QPainterPath(QPointF(trunk_x, stem_start_y))
            path.lineTo(QPointF(trunk_x, end.y()))
            path.lineTo(end)
            return path

        if branch_type == 2:
            trunk_x = max(
                rel.target.sceneBoundingRect().right()
                for rel in underline_relations
            ) + 28.0
            end = QPointF(target_rect.right(), target_rect.center().y())

            if end.y() > max_y:
                stem_start_y = max_y
            elif end.y() < min_y:
                stem_start_y = min_y
            else:
                stem_start_y = end.y()

            path = QPainterPath(QPointF(trunk_x, stem_start_y))
            path.lineTo(QPointF(trunk_x, end.y()))
            path.lineTo(end)
            return path

        # --------------------------------------------------------------
        # Oben / unten:
        # Der gemeinsame Stamm liegt am linken Beginn der Unterstrichen-Liste.
        # Auch hier bis zur neuen Rechteckhöhe verlängern.
        # --------------------------------------------------------------
        trunk_x = source_rect.left()

        if target_rect.center().x() >= trunk_x:
            end = QPointF(target_rect.left(), target_rect.center().y())
        else:
            end = QPointF(target_rect.right(), target_rect.center().y())

        if branch_type == 3:  # unten
            stem_start_y = max_y
        else:                 # oben
            stem_start_y = min_y

        path = QPainterPath(QPointF(trunk_x, stem_start_y))
        path.lineTo(QPointF(trunk_x, end.y()))
        path.lineTo(end)
        return path

    def update_path(self) -> None:
        source_rect = self.source.sceneBoundingRect()
        target_rect = self.target.sceneBoundingRect()

        source_underline = self._underline_anchor(
            self.source,
            self.branch_type,
            outgoing=True,
        )

        # Bei einem unterstrichenen Mutterknoten muss ein vertikaler Ast
        # direkt an dessen sichtbarer letzter Linie beginnen. Sonst entsteht
        # zwischen Unterstreichung und Kind eine optische Lücke.
        if source_underline is not None and int(self.branch_type) in {3, 4}:
            start = source_underline
        elif self.relation_id is not None:
            start = self.source.mapToScene(
                self.source.relation_exit_point(self.relation_id)
            )
        else:
            start = source_underline
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

        # Ein aus einer Unterstrichen-Gruppe in Rechteck/Rund umgewandelter
        # Knoten behält seinen Anschluss am gemeinsamen Stamm.
        kept_group_path = None
        if route is None:
            kept_group_path = self._kept_group_trunk_path(
                source_rect,
                target_rect,
            )

        # --------------------------------------------------------------
        # Unterstrichen -> Unterstrichen, vertikal:
        #
        # Der Stamm läuft vom Anschluss der Mutterlinie SENKRECHT bis auf
        # die Unterstreichung des Kindes. Die X-Position wird von der Mutter
        # geerbt und NICHT am Kind neu berechnet. Dadurch läuft die Verbindung
        # links am Text vorbei – exakt wie in der roten Referenzmarkierung.
        #
        # Falls Mutter und Kind horizontal versetzt sind, endet der Stamm
        # auf derselben X-Position innerhalb der Kinder-Unterstreichung;
        # nur wenn diese X-Position außerhalb der sichtbaren Linie läge,
        # wird auf deren Linienbereich begrenzt.
        vertical_underline_path = None
        source_state = self.source.model.active_map["object_states"].get(
            self.source.object_id, {}
        )
        target_state = self.target.model.active_map["object_states"].get(
            self.target.object_id, {}
        )
        if (
            route is None
            and branch_type in {3, 4}
            and source_state.get("shape", "rounded") == "underline"
            and target_state.get("shape", "rounded") == "underline"
        ):
            target_line_y = self.target.mapToScene(
                QPointF(0.0, self.target.underline_y())
            ).y()

            # Saubere Stammgeometrie:
            # Der Stamm liegt am linken Beginn der Kinder-Unterstreichung.
            # So entsteht unten kein linker Überstand.
            target_x = target_rect.left()

            # Startpunkt der Mutter ebenfalls auf die linke Unterstreichungskante
            # ziehen. Damit liegt die Abzweigung weiter links.
            source_line_y = self.source.mapToScene(
                QPointF(0.0, self.source.underline_y())
            ).y()
            source_x = source_rect.left()
            start = QPointF(source_x, source_line_y)

            vertical_underline_path = QPainterPath(start)
            vertical_underline_path.lineTo(
                QPointF(target_x, target_line_y)
            )

        # Unterstrichene Geschwister derselben Seite werden als Gruppe
        # geroutet. Manuell geführte Relationen behalten dagegen bewusst
        # ihre individuelle Geometrie.
        grouped_path = None
        if (
            kept_group_path is None
            and vertical_underline_path is None
            and route is None
            and self._is_underline_target()
        ):
            grouped_path = self._underline_group_path(source_rect, end)

        if kept_group_path is not None:
            path = kept_group_path
        elif vertical_underline_path is not None:
            path = vertical_underline_path
        else:
            path = grouped_path if grouped_path is not None else QPainterPath(start)

        if kept_group_path is not None:
            pass
        elif vertical_underline_path is not None:
            pass
        elif grouped_path is not None:
            pass
        elif route is None:
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
    """Klickfläche für Ein-/Ausklappen eines einzelnen Astes.

    Ausgeklappt:
        unsichtbare kleine Klickfläche direkt am Austrittspunkt.

    Eingeklappt:
        kurzer Zweigstumpf + leicht abgesetzter Plus-Kreis.

    Wichtig:
        Ein Linksklick toggelt IMMER den Astzustand. Der Ast selbst wird dabei
        nicht selektiert und seine manuelle Linienführung nicht aktiviert.
    """

    STUB_LENGTH = 22.0
    SYMBOL_GAP = 6.0
    SYMBOL_RADIUS = 6.0

    def __init__(self, owner: "NodeItem", relation_id: str) -> None:
        # Genug Platz für den sichtbaren Stumpf und das Plus.
        # Die Klickfläche bleibt ein eigenes Item oberhalb der Relation.
        super().__init__(-10.0, -10.0, 58.0, 58.0, owner)
        self.owner = owner
        self.relation_id = relation_id
        self.setPen(Qt.NoPen)
        self.setBrush(Qt.NoBrush)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setZValue(80)
        self.setCursor(Qt.PointingHandCursor)

    def _direction(self) -> QPointF:
        relation = self.owner.model.data["relations"].get(self.relation_id, {})
        branch_type = int(relation.get("branch_type", 1))
        if branch_type == 2:
            return QPointF(-1.0, 0.0)
        if branch_type == 4:
            return QPointF(0.0, -1.0)
        if branch_type == 3:
            return QPointF(0.0, 1.0)
        return QPointF(1.0, 0.0)

    def boundingRect(self) -> QRectF:
        # Symmetrisch groß genug für alle vier Richtungen.
        reach = self.STUB_LENGTH + self.SYMBOL_GAP + 2.0 * self.SYMBOL_RADIUS + 6.0
        return QRectF(-reach, -reach, 2.0 * reach, 2.0 * reach)

    def shape(self) -> QPainterPath:
        # Im ausgeklappten Zustand nur eine kompakte Klickfläche am Austritt.
        # Eingeklappt zusätzlich Stumpf und Plus gut anklickbar machen.
        relation = self.owner.model.data["relations"].get(self.relation_id, {})
        collapsed = bool(relation.get("collapsed", False))

        path = QPainterPath()
        path.addEllipse(QPointF(0.0, 0.0), 10.0, 10.0)

        if collapsed:
            d = self._direction()
            symbol_center = QPointF(
                d.x() * (self.STUB_LENGTH + self.SYMBOL_GAP + self.SYMBOL_RADIUS),
                d.y() * (self.STUB_LENGTH + self.SYMBOL_GAP + self.SYMBOL_RADIUS),
            )

            # Breite Klickzone entlang des Stumpfs.
            stroker = QPainterPathStroker()
            stroker.setWidth(14.0)
            stub = QPainterPath(QPointF(0.0, 0.0))
            stub.lineTo(
                QPointF(
                    d.x() * self.STUB_LENGTH,
                    d.y() * self.STUB_LENGTH,
                )
            )
            path.addPath(stroker.createStroke(stub))
            path.addEllipse(symbol_center, 12.0, 12.0)

        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        relation = self.owner.model.data["relations"].get(self.relation_id, {})
        if not relation.get("collapsed", False):
            return

        state = self.owner.model.active_map["object_states"][self.owner.object_id]
        marker_color = QColor(state.get("border_color", "#4f5d75"))
        width = max(1.5, float(state.get("border_width", 1.5)))

        pen = QPen(marker_color, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(QColor("#ffffff"))

        d = self._direction()
        stub_end = QPointF(
            d.x() * self.STUB_LENGTH,
            d.y() * self.STUB_LENGTH,
        )
        painter.drawLine(QPointF(0.0, 0.0), stub_end)

        symbol_center = QPointF(
            d.x() * (self.STUB_LENGTH + self.SYMBOL_GAP + self.SYMBOL_RADIUS),
            d.y() * (self.STUB_LENGTH + self.SYMBOL_GAP + self.SYMBOL_RADIUS),
        )

        painter.drawEllipse(
            symbol_center,
            self.SYMBOL_RADIUS,
            self.SYMBOL_RADIUS,
        )

        arm = 3.2
        painter.drawLine(
            QPointF(symbol_center.x() - arm, symbol_center.y()),
            QPointF(symbol_center.x() + arm, symbol_center.y()),
        )
        painter.drawLine(
            QPointF(symbol_center.x(), symbol_center.y() - arm),
            QPointF(symbol_center.x(), symbol_center.y() + arm),
        )

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            # Entscheidend: IMMER toggeln. Nicht an RelationItem weiterreichen.
            self.owner.toggle_relation_collapsed(self.relation_id)
            event.accept()
            return
        event.accept()


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
        self._image_position = -1
        self._press_scene_pos = QPointF()


    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        image = self.editor._first_image()
        if image is None:
            return

        image_cursor, image_format = image
        self._start_width = max(1.0, float(image_format.width()))
        self._start_height = max(1.0, float(image_format.height()))
        self._image_position = int(image_cursor.selectionStart())
        self._press_scene_pos = QPointF(event.scenePos())

        # Der Bildrahmen ist bereits aktiv, wenn dieser Griff erreichbar ist.
        # Keine erneute Knotenselektion hier: das verhindert Selection-Callbacks
        # während des Beginns des Bild-Drags.
        self.editor.owner.scene_owner.set_active_node(
            self.editor.owner.object_id
        )

        self._snapshot = self.editor.owner.scene_owner.make_snapshot()
        self._dragging = True
        self.editor._image_resize_in_progress = True
        self.grabMouse()
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._dragging:
            return

        # Größe ausschließlich aus dem Mausweg seit Mouse-Press ableiten.
        # Damit hängt die Berechnung nicht von einem während des Drags
        # wandernden QTextDocument-Layout oder Bildrechteck ab.
        delta = event.scenePos() - self._press_scene_pos
        new_width = max(30.0, self._start_width + float(delta.x()))

        aspect = (
            self._start_height / self._start_width
            if self._start_width > 0.0
            else 1.0
        )
        new_height = max(20.0, new_width * aspect)

        self.editor._set_image_size_at(
            self._image_position,
            new_width,
            new_height,
        )
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._dragging:
            return

        self._dragging = False
        self.editor._image_resize_in_progress = False
        try:
            self.ungrabMouse()
        except (RuntimeError, ReferenceError):
            pass

        self.editor._commit_image_resize()
        self.editor.owner.scene_owner.commit_snapshot(self._snapshot)
        self._snapshot = None
        self._image_position = -1
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
        state = self.model.active_map["object_states"].get(self.object_id, {})
        # Unterstrichen ist der kompakte Listen-/Aufzählungstyp.
        # Ohne Symbol darf der Text deutlich näher am Linienanfang beginnen.
        x = 4.0 if state.get("shape", "rounded") == "underline" else 10.0
        gap = 6.0 if state.get("shape", "rounded") == "underline" else 8.0
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
        state = self.model.active_map["object_states"].get(self.object_id, {})
        underline_compact = state.get("shape", "rounded") == "underline"
        x = 4.0 if underline_compact else 10.0
        gap = 6.0 if underline_compact else 8.0
        # Die sichtbare Unterstreichung liegt knapp unter dem Text.
        # Weniger Innenrand spart bei Listenpunkten zusätzlich Höhe.
        top = 5.0 if underline_compact else 10.0

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
        """Geordnetes Austrittslayout ohne Knotenbewegung.

        links/rechts: Ziele von oben nach unten
        oben/unten:   Ziele von links nach rechts
        Danach werden die Austritte symmetrisch über die Knotenkante verteilt.
        """
        relation = self.model.data["relations"].get(relation_id, {})
        branch_type = int(relation.get("branch_type", 1))
        state = self.model.active_map["object_states"][self.object_id]
        rect = self.rect()

        if state.get("shape", "rounded") == "underline" and branch_type in {1, 2}:
            return QPointF(
                rect.right() if branch_type == 1 else rect.left(),
                self.underline_y(),
            )

        siblings = self._outgoing_tree_relations(branch_type)
        states = self.model.active_map["object_states"]

        def target_center(rel_data: dict) -> tuple[float, float]:
            child = states.get(rel_data.get("target_id"), {})
            return (
                float(child.get("x", 0.0)) + float(child.get("width", 180.0)) / 2.0,
                float(child.get("y", 0.0)) + float(child.get("height", 54.0)) / 2.0,
            )

        if branch_type in {3, 4}:
            siblings.sort(
                key=lambda item: (
                    target_center(item[1])[0],
                    target_center(item[1])[1],
                    item[0],
                )
            )
        else:
            siblings.sort(
                key=lambda item: (
                    target_center(item[1])[1],
                    target_center(item[1])[0],
                    item[0],
                )
            )

        ids = [item[0] for item in siblings]
        try:
            index = ids.index(relation_id)
        except ValueError:
            index = 0
        count = len(siblings)

        if branch_type in {3, 4}:
            center = rect.center().x()
            edge_length = max(1.0, rect.width())
        else:
            center = rect.center().y()
            edge_length = max(1.0, rect.height())

        # Harmonischer Fächer: kompakt, symmetrisch, Ecken bleiben frei.
        usable_span = edge_length * 0.64
        preferred_step = 18.0
        span = 0.0 if count <= 1 else min(
            usable_span,
            preferred_step * (count - 1),
        )

        if count <= 1:
            coordinate = center
        else:
            step = span / (count - 1)
            coordinate = center - span / 2.0 + step * index

        if branch_type == 2:
            return QPointF(rect.left(), coordinate)
        if branch_type == 1:
            return QPointF(rect.right(), coordinate)
        if branch_type == 4:
            return QPointF(coordinate, rect.top())
        return QPointF(coordinate, rect.bottom())

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

        note_action = QAction("Notiz bearbeiten …", menu)
        note_action.setToolTip("Notiz zu diesem Knoten anlegen oder bearbeiten")
        note_action.triggered.connect(
            lambda checked=False: self.scene_owner.request_details(
                self.object_id,
                "note",
            )
        )
        menu.addAction(note_action)

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

        Normaler Klick auf ein NICHT markiertes Objekt:
            -> bisherige Auswahl aufheben und dieses Objekt auswählen.

        Normaler Klick auf ein BEREITS markiertes Objekt:
            -> bestehende Mehrfachauswahl erhalten.
               Dadurch kann eine markierte Gruppe direkt gezogen werden,
               ohne beim Anfassen wieder auseinanderzufallen.

        Strg/Shift:
            -> Mehrfachauswahl darf erweitert/reduziert werden.
        """
        multi_modifier = bool(
            modifiers & (Qt.ControlModifier | Qt.ShiftModifier)
        )

        if not multi_modifier:
            if owner.isSelected():
                # Wichtig für Gruppenverschiebung:
                # Eine vorhandene Mehrfachauswahl bleibt beim Anfassen eines
                # bereits ausgewählten Elements vollständig bestehen.
                pass
            else:
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

    def comb_branches_for_node(self, parent_id: str) -> bool:
        """Sortieren -> Seiten bestimmen -> harmonisch verteilen.

        Ändert ausschließlich Relationen/Liniengeometrie.
        """
        if parent_id not in self.node_items:
            return False

        changed = self.model.resort_outgoing_relations_from_geometry(parent_id)
        parent_item = self.node_items[parent_id]

        for relation_item in list(parent_item.relations):
            if relation_item.source is not parent_item:
                continue
            data = self.model.data["relations"].get(relation_item.relation_id, {})
            relation_item.branch_type = int(
                data.get("branch_type", relation_item.branch_type)
            )
            relation_item.update_path()

        parent_item.update_badge_position()
        return changed

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

    def create_child_for_selected(
        self,
        preserve_underline_layout: bool = False,
    ) -> str | None:
        """Erzeugt einen Unterknoten strikt in der geerbten Wachstumsrichtung.

        Grundregel:
        Die Verbindung zum Elternknoten bestimmt die Wachstumsrichtung
        dieses Schrittes. Bestehende Struktur wird nicht neu zentriert.

        - rechts  -> neuer Bereich rechts vom Elternknoten
        - links   -> neuer Bereich links vom Elternknoten
        - unten   -> neuer Bereich unterhalb des Elternknotens
        - oben    -> neuer Bereich oberhalb des Elternknotens

        Mehrere Kinder derselben Richtung werden nur innerhalb dieses
        Zielbereichs gestaffelt. Vorhandene Vorfahren und Geschwister
        bleiben unangetastet.
        """
        parent = self.selected_node()
        if parent is None:
            return None

        self.push_undo()
        parent_id = parent.object_id
        branch_type = self.model.effective_child_branch_type(parent_id)

        states = self.model.active_map["object_states"]
        parent_state = states.get(parent_id, {})
        px = float(parent_state.get("x", 0.0))
        py = float(parent_state.get("y", 0.0))
        pw = float(parent_state.get("width", 180.0))
        ph = float(parent_state.get("height", 54.0))

        template_child_id = self.model.last_child_id(
            parent_id,
            branch_type=branch_type,
        )

        existing_ids = [
            child_id
            for child_id in self.model.child_ids_by_branch(
                parent_id,
                branch_type,
            )
            if child_id in states
        ]

        horizontal_gap = 86.0
        vertical_gap = 34.0
        default_width = 180.0
        default_height = 54.0
        underline_pitch = 30.0
        underline_list = bool(existing_ids) and all(
            states[cid].get("shape", "rounded") == "underline"
            for cid in existing_ids
        )

        # --------------------------------------------------------------
        # Erstes Kind:
        # direkt in der geerbten Wachstumsrichtung.
        # --------------------------------------------------------------
        if not existing_ids:
            x, y = self.model.next_child_position(
                parent_id,
                branch_type=branch_type,
            )

        else:
            child_states = [states[cid] for cid in existing_ids]

            # Kompakte Unterstrichen-Liste:
            # bestehende Einträge bleiben exakt liegen; der neue Eintrag wird
            # nur eine sichtbare Listenzeile weiter angehängt.
            if underline_list:
                if branch_type == 4:
                    anchor = min(
                        child_states,
                        key=lambda st: float(st.get("y", 0.0)),
                    )
                    x = float(anchor.get("x", 0.0))
                    y = min(float(st.get("y", 0.0)) for st in child_states) - underline_pitch
                else:
                    anchor = max(
                        child_states,
                        key=lambda st: float(st.get("y", 0.0)),
                    )
                    x = float(anchor.get("x", 0.0))
                    y = max(float(st.get("y", 0.0)) for st in child_states) + underline_pitch

            # ----------------------------------------------------------
            # RECHTS:
            # Alle Kinder bleiben rechts des Elternknotens.
            # Neue Geschwister werden innerhalb dieser rechten Zone
            # ausschließlich nach unten ergänzt.
            # ----------------------------------------------------------
            if branch_type == 1:
                zone_x = max(
                    px + pw + horizontal_gap,
                    min(float(st.get("x", 0.0)) for st in child_states),
                )
                bottom = max(
                    float(st.get("y", 0.0))
                    + float(st.get("height", default_height))
                    for st in child_states
                )
                x = zone_x
                y = bottom + vertical_gap

            # ----------------------------------------------------------
            # LINKS:
            # Alle Kinder bleiben links des Elternknotens.
            # Neue Geschwister werden dort nach unten ergänzt.
            # ----------------------------------------------------------
            elif branch_type == 2:
                # bestehende linke Zone beibehalten
                zone_right = min(
                    px - horizontal_gap,
                    max(
                        float(st.get("x", 0.0))
                        + float(st.get("width", default_width))
                        for st in child_states
                    ),
                )
                # Breite des letzten/äußersten Knotens als Referenz
                reference = max(
                    child_states,
                    key=lambda st:
                    float(st.get("y", 0.0))
                    + float(st.get("height", default_height)),
                )
                width = float(reference.get("width", default_width))
                x = zone_right - width
                bottom = max(
                    float(st.get("y", 0.0))
                    + float(st.get("height", default_height))
                    for st in child_states
                )
                y = bottom + vertical_gap

            # ----------------------------------------------------------
            # UNTEN:
            # Der neue Bereich wächst nur nach unten.
            # Bestehende Knoten oberhalb bleiben exakt stehen.
            # ----------------------------------------------------------
            elif branch_type == 3:
                # X-Position der bestehenden unteren Gruppe beibehalten.
                anchor = max(
                    child_states,
                    key=lambda st:
                    float(st.get("y", 0.0))
                    + float(st.get("height", default_height)),
                )
                x = float(anchor.get("x", px))
                bottom = max(
                    float(st.get("y", 0.0))
                    + float(st.get("height", default_height))
                    for st in child_states
                )
                y = bottom + vertical_gap

            # ----------------------------------------------------------
            # OBEN:
            # Der neue Bereich wächst nur nach oben.
            # ----------------------------------------------------------
            else:  # branch_type == 4
                anchor = min(
                    child_states,
                    key=lambda st: float(st.get("y", 0.0)),
                )
                x = float(anchor.get("x", px))
                top = min(
                    float(st.get("y", 0.0))
                    for st in child_states
                )
                y = top - vertical_gap - default_height

        child_id = self.model.add_object(
            "Neuer Unterknoten",
            x,
            y,
        )
        self.model.add_relation(
            parent_id,
            child_id,
            "tree",
            branch_type=branch_type,
        )

        if template_child_id is not None:
            # Darstellung übernehmen, Position aber NICHT überschreiben.
            self.model.apply_child_template(
                template_child_id,
                child_id,
            )
        else:
            self.model.apply_visual_template(
                parent_id,
                child_id,
            )

        # Kein globaler Reflow.
        # Nur der neue Knoten und die lokale Liniengeometrie kommen hinzu.
        self.model.touch()

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

        states = self.model.active_map["object_states"]
        current_state = states.get(current_id, {})
        underline_append = current_state.get("shape", "rounded") == "underline"

        self.push_undo()

        if underline_append:
            # ----------------------------------------------------------
            # Sonderregel fuer Strg+Enter bei "Unterstrichen":
            #
            # Bestehende Geschwister bleiben EXAKT liegen.
            # Der neue Knoten wird nur am Ende seiner Seitengruppe
            # angehaengt. Kein arrange_children(), kein Zentrieren,
            # kein Verschieben der Vorfahren.
            # ----------------------------------------------------------
            group_ids = [
                child_id
                for child_id in self.model.child_ids_by_branch(
                    parent_id,
                    branch_type,
                )
                if child_id in states
                and states[child_id].get("shape", "rounded") == "underline"
            ]

            # Normalabstand des Listentyps "Unterstrichen".
            # Bewusst nach den sichtbaren Linien/Textzeilen und NICHT nach
            # den 54 px hohen unsichtbaren Auswahlrechtecken bemessen.
            underline_pitch = 30.0

            if group_ids:
                group_states = [states[child_id] for child_id in group_ids]

                if branch_type == 4:
                    # Obere Gruppe: eine kompakte Listenzeile weiter nach oben.
                    anchor = min(
                        group_states,
                        key=lambda st: float(st.get("y", 0.0)),
                    )
                    x = float(anchor.get("x", 0.0))
                    top = min(float(st.get("y", 0.0)) for st in group_states)
                    y = top - underline_pitch
                else:
                    # Rechts, links und unten: eine kompakte Listenzeile tiefer.
                    anchor = max(
                        group_states,
                        key=lambda st: float(st.get("y", 0.0)),
                    )
                    x = float(anchor.get("x", 0.0))
                    y = max(
                        float(st.get("y", 0.0))
                        for st in group_states
                    ) + underline_pitch
            else:
                x, y = self.model.next_child_position(
                    parent_id,
                    branch_type=branch_type,
                )
        else:
            # ----------------------------------------------------------
            # Normale Geschwister bei Strg+Enter:
            #
            # Nicht erneut die erste Standardposition des Elternknotens
            # verwenden – genau das führte zur Überlagerung.
            #
            # Wachstumsregel:
            #   rechts/links -> Geschwister nach unten anhängen
            #   unten/oben   -> Geschwister nach rechts anhängen
            #
            # Bestehende Knoten bleiben unverändert.
            # ----------------------------------------------------------
            group_ids = [
                child_id
                for child_id in self.model.child_ids_by_branch(
                    parent_id,
                    branch_type,
                )
                if child_id in states
            ]

            normal_vertical_gap = 34.0
            normal_horizontal_gap = 86.0

            if not group_ids:
                x, y = self.model.next_child_position(
                    parent_id,
                    branch_type=branch_type,
                )
            else:
                group_states = [states[child_id] for child_id in group_ids]

                if branch_type in {1, 2}:
                    # Rechte/linke Seitengruppe wächst nur nach unten.
                    anchor = max(
                        group_states,
                        key=lambda st:
                        float(st.get("y", 0.0))
                        + float(st.get("height", 54.0)),
                    )
                    x = float(anchor.get("x", current_state.get("x", 0.0)))
                    bottom = max(
                        float(st.get("y", 0.0))
                        + float(st.get("height", 54.0))
                        for st in group_states
                    )
                    y = bottom + normal_vertical_gap
                else:
                    # Obere/untere Gruppe wächst quer zur Ast-Richtung
                    # nach rechts weiter.
                    anchor = max(
                        group_states,
                        key=lambda st:
                        float(st.get("x", 0.0))
                        + float(st.get("width", 180.0)),
                    )
                    y = float(anchor.get("y", current_state.get("y", 0.0)))
                    right = max(
                        float(st.get("x", 0.0))
                        + float(st.get("width", 180.0))
                        for st in group_states
                    )
                    x = right + normal_horizontal_gap

        sibling_id = self.model.add_object("Neuer Knoten", x, y)
        self.model.add_relation(
            parent_id,
            sibling_id,
            "tree",
            branch_type=branch_type,
        )

        # Ein Geschwister übernimmt die Darstellung des aktuellen Knotens.
        self.model.apply_child_template(current_id, sibling_id)

        # Strg+Enter folgt jetzt ebenfalls der Wachstumsregel:
        # kein globaler Reflow, keine Neuzentrierung, keine Verschiebung
        # vorhandener Geschwister, Eltern oder Vorfahren.
        self.model.touch()

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

        image_html = clipboard_image_html()
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
        shift_only = bool(modifiers & Qt.ShiftModifier) and not bool(
            modifiers & (Qt.AltModifier | Qt.ControlModifier | Qt.MetaModifier)
        )

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if ctrl_only:
                # STRG+Enter: bewusst unverändert lassen.
                created = self.create_sibling_for_selected()
            elif shift_only:
                # Shift+Enter nutzt denselben Erzeugungspfad.
                # Ob lokal ohne Reflow gearbeitet wird, entscheidet jetzt
                # ausschließlich der Knotentyp des Elternknotens.
                created = self.create_child_for_selected(
                    preserve_underline_layout=True,
                )
            else:
                # Einfaches Enter: identische Grundregel für Unterstrichen.
                created = self.create_child_for_selected()

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
        # Unterstrichene Knoten sind kompakte Listeneinträge. Beim manuellen
        # Zusammenschieben darf Nähe zu einem anderen Knoten weder als
        # Kollision noch als Wunsch zum Umhängen der Hierarchie interpretiert
        # werden. Deshalb gibt es für diesen Typ kein automatisches
        # Reparent-Ziel und damit auch keinen blauen Vorschau-Pfeil.
        if not self._collision_enabled_for_node(dragged.object_id):
            return None

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
        """Merkt eine Mehrfachauswahl als starren Verschiebeblock.

        Wird ein bereits markierter Knoten angefasst, bewegen sich alle
        markierten Knoten mit exakt demselben Delta. Beziehungen, Hierarchie
        und relative Abstände innerhalb der Auswahl bleiben unverändert.
        """
        selected = [
            item for item in self.selectedItems()
            if isinstance(item, NodeItem)
        ]

        if dragged not in selected:
            selected = [dragged]

        self.group_drag_ids = {item.object_id for item in selected}
        self.group_drag_start_positions = {
            item.object_id: QPointF(item.pos())
            for item in selected
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
        self.reparent_enabled = (
            len(selected_nodes) == 1
            and self._collision_enabled_for_node(dragged.object_id)
        )

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

    def _collision_enabled_for_node(self, object_id: str) -> bool:
        """Unterstrichene Knoten sind bewusst kollisionsfrei.

        Sie dienen als kompakte Aufzählung und dürfen dicht untereinander
        stehen. Weder lösen sie eine automatische Verdrängung aus noch
        werden sie von einer anderen Karte automatisch weggeschoben.
        """
        state = self.model.active_map["object_states"].get(object_id, {})
        return state.get("shape", "rounded") != "underline"

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
                if not self._collision_enabled_for_node(active_id):
                    continue
                active_rect = self._node_rect(active_id).adjusted(
                    -margin, -margin, margin, margin
                )
                for other_id in states:
                    if other_id in active_ids:
                        continue
                    if not self._collision_enabled_for_node(other_id):
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

    # ------------------------------------------------------------------
    # Lokale Kollisionslogik nach dem "Spielkarten"-Regelwerk
    #
    # Leitsatz:
    #   Automatik hilft lokal, übernimmt aber nicht die Kontrolle.
    #
    # Regel 1:
    #   Der vom Benutzer abgelegte Knoten bleibt exakt an seiner Position.
    #
    # Regel 2a:
    #   Nur eine echte Überlappung löst die Automatik aus.
    #
    # Regel 2b:
    #   Die kollidierte Karte wird auf dem Mittelpunktvektor vom aktiven
    #   Knoten weg verschoben, bis zusätzlich ein kleiner Freiraum entsteht.
    #
    # Regel 3:
    #   Die verschobene Karte wird erneut geprüft. Maximal fünf Karten
    #   werden pro Benutzeraktion automatisch verschoben.
    #
    # Regel 4:
    #   Kinder werden NICHT als Teilbaum mitgenommen. Sie bewegen sich nur,
    #   wenn sie selbst durch die Kollisionskette betroffen sind.
    # ------------------------------------------------------------------

    COLLISION_CARD_GAP = 28.0
    COLLISION_MAX_SHOVED_CARDS = 5

    @staticmethod
    def _rects_really_overlap(first: QRectF, second: QRectF) -> bool:
        """Regel 2a: echte Flächenüberlappung, noch ohne Komfortabstand."""
        return (
            first.left() < second.right()
            and first.right() > second.left()
            and first.top() < second.bottom()
            and first.bottom() > second.top()
        )

    def _nearest_colliding_card(
        self,
        source_id: str,
        *,
        fixed_id: str,
        already_shoved: set[str],
    ) -> str | None:
        """Findet die nächstliegende tatsächlich kollidierende sichtbare Karte."""
        if source_id not in self.node_items:
            return None

        # Typ "Unterstrichen" ist vollständig von der automatischen
        # Kollisionsauflösung ausgenommen.
        if not self._collision_enabled_for_node(source_id):
            return None

        source_rect = self._node_rect(source_id)
        source_center = source_rect.center()
        candidates: list[tuple[float, str]] = []

        # node_items enthält nur die momentan sichtbaren Karten.
        for other_id in self.node_items:
            if other_id == source_id:
                continue

            # Unterstrichene Knoten sind auch als Kollisionsziel unsichtbar
            # für die Automatik. Ein normaler Knoten schiebt sie also nicht weg.
            if not self._collision_enabled_for_node(other_id):
                continue

            # Regel 1: Die vom Bediener abgelegte Karte ist für diesen
            # kompletten Vorgang absolut und darf niemals weggeschoben werden.
            if other_id == fixed_id:
                continue

            # Eine bereits automatisch geschobene Karte wird nicht ein zweites
            # Mal bewegt. Das verhindert Ping-Pong und geometrische Zyklen.
            if other_id in already_shoved:
                continue

            other_rect = self._node_rect(other_id)
            if not self._rects_really_overlap(source_rect, other_rect):
                continue

            dx = other_rect.center().x() - source_center.x()
            dy = other_rect.center().y() - source_center.y()
            candidates.append((dx * dx + dy * dy, other_id))

        if not candidates:
            return None

        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    def _shove_card_away(
        self,
        source_id: str,
        target_id: str,
        gap: float,
    ) -> tuple[float, float]:
        """
        Regel 2b: Verschiebt nur target_id entlang des Mittelpunktvektors.

        Die Strecke wird so gewählt, dass sich die Rechtecke nicht mehr
        überlappen und zwischen ihnen zusätzlich ``gap`` Pixel frei bleiben.
        """
        source_rect = self._node_rect(source_id)
        target_rect = self._node_rect(target_id)

        sx = source_rect.center().x()
        sy = source_rect.center().y()
        tx = target_rect.center().x()
        ty = target_rect.center().y()

        vx = tx - sx
        vy = ty - sy
        length = math.hypot(vx, vy)

        # Exakt gleiche Mittelpunkte besitzen keinen Richtungsvektor.
        # Der Sonderfall bekommt eine reproduzierbare Richtung nach rechts.
        if length < 1.0e-9:
            ux, uy = 1.0, 0.0
        else:
            ux, uy = vx / length, vy / length

        # Strecke bis zur ersten Trennung inklusive Komfortabstand.
        # Es reicht, wenn die Rechtecke entlang EINER Achse getrennt sind.
        distances: list[float] = []

        if ux > 1.0e-9:
            distances.append(
                (source_rect.right() + gap - target_rect.left()) / ux
            )
        elif ux < -1.0e-9:
            distances.append(
                (target_rect.right() - (source_rect.left() - gap)) / (-ux)
            )

        if uy > 1.0e-9:
            distances.append(
                (source_rect.bottom() + gap - target_rect.top()) / uy
            )
        elif uy < -1.0e-9:
            distances.append(
                (target_rect.bottom() - (source_rect.top() - gap)) / (-uy)
            )

        positive = [distance for distance in distances if distance >= 0.0]
        distance = min(positive) if positive else gap

        # Kleine Reserve gegen Rundungsfehler / Touching-Grenzfälle.
        distance += 0.5

        dx = ux * distance
        dy = uy * distance

        state = self.model.active_map["object_states"][target_id]
        new_x = float(state.get("x", 0.0)) + dx
        new_y = float(state.get("y", 0.0)) + dy

        # Regel 4: Nur diese EINE Karte bewegen, niemals ihren Teilbaum.
        self.model.set_position(target_id, new_x, new_y)
        self.model.set_layout_mode(target_id, "manual")

        # Nur die Anschlussseite darf auf die neue räumliche Lage reagieren.
        # Es wird dabei nichts weiter angeordnet.
        self.model.refresh_branch_type_from_position(target_id)

        return dx, dy

    def _resolve_card_collisions(
        self,
        fixed_id: str,
        gap: float | None = None,
        max_shoved_cards: int | None = None,
    ) -> list[str]:
        """
        Wendet Regeln 2a, 2b, 3 und 4 auf die abgelegte Karte an.

        Rückgabe ist die Reihenfolge der automatisch verschobenen Karten.
        """
        gap = (
            self.COLLISION_CARD_GAP
            if gap is None
            else max(0.0, float(gap))
        )
        limit = (
            self.COLLISION_MAX_SHOVED_CARDS
            if max_shoved_cards is None
            else max(0, int(max_shoved_cards))
        )

        if fixed_id not in self.node_items or limit == 0:
            return []

        if not self._collision_enabled_for_node(fixed_id):
            return []

        shoved: list[str] = []
        shoved_set: set[str] = set()

        def resolve_from(source_id: str) -> None:
            # Nach einer weitergereichten Kollision wird dieselbe Quellkarte
            # nochmals geprüft, weil sie gleichzeitig mehrere Karten
            # überdecken kann.
            while len(shoved) < limit:
                collision_id = self._nearest_colliding_card(
                    source_id,
                    fixed_id=fixed_id,
                    already_shoved=shoved_set,
                )
                if collision_id is None:
                    return

                self._shove_card_away(source_id, collision_id, gap)
                shoved.append(collision_id)
                shoved_set.add(collision_id)

                # Regel 3: Zuerst die gerade verschobene Karte prüfen.
                resolve_from(collision_id)

                if len(shoved) >= limit:
                    return

        resolve_from(fixed_id)
        return shoved

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

                # Verbindungen aus der Auswahl nach außen an die neue Lage anpassen.
                touched_parents: set[str] = set()
                for object_id in protected_ids:
                    parent_id = self.model.tree_parent(object_id)
                    if parent_id not in protected_ids:
                        self.model.refresh_branch_type_from_position(object_id)
                    if parent_id is not None:
                        touched_parents.add(parent_id)

                # Nur Linien kämmen; Karten bleiben unverändert.
                for object_id in protected_ids:
                    self.comb_branches_for_node(object_id)
                for parent_id in touched_parents:
                    self.comb_branches_for_node(parent_id)
            else:
                # Regel 1: Der Benutzer hat die Position bestimmt. Der gezogene
                # Knoten bleibt exakt dort; Kinder werden nicht mitgenommen.
                self.model.set_layout_mode(dragged.object_id, "manual")

                if not self._collision_enabled_for_node(dragged.object_id):
                    # Unterstrichen = freier kompakter Listeneintrag:
                    # Position übernehmen, aber KEIN Reparenting, KEINE
                    # Kollisionsauflösung, KEINE Seiten-Neubestimmung und
                    # KEIN automatisches Kämmen/Neuordnen.
                    shoved_cards = []
                else:
                    # Zuerst die eigene Elternverbindung an die neue Lage anpassen.
                    self.model.refresh_branch_type_from_position(dragged.object_id)

                    # Regeln 2a, 2b, 3, 4:
                    # Nur bei echter Kollision lokal weiterreichen, maximal 5 Karten.
                    shoved_cards = self._resolve_card_collisions(
                        dragged.object_id,
                    )
                    collision_changed = bool(shoved_cards)

                    # Danach Äste sortieren und harmonisch verteilen.
                    # Alte manuelle Linienführungen dürfen verworfen werden.
                    self.comb_branches_for_node(dragged.object_id)
                    parent_id = self.model.tree_parent(dragged.object_id)
                    if parent_id is not None:
                        self.comb_branches_for_node(parent_id)

                    # Auch automatisch weggeschobene Karten können Eltern sein.
                    # Nur deren Linien werden neu gekämmt.
                    for shoved_id in shoved_cards:
                        self.comb_branches_for_node(shoved_id)
                        shoved_parent = self.model.tree_parent(shoved_id)
                        if shoved_parent is not None:
                            self.comb_branches_for_node(shoved_parent)

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
