from __future__ import annotations

import shiboken6
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen, QTextCursor
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsTextItem, QGraphicsSceneMouseEvent


def _qt_valid(obj) -> bool:
    return obj is not None and shiboken6.isValid(obj)


class ImageScalableTextItem(QGraphicsTextItem):
    """Shared inline-image resizing for node and free-standing rich text.

    Subclasses persist resized HTML in ``_commit_image_resize``. The scene
    supplies make_snapshot/commit_snapshot for one undo step per drag.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image_resize_in_progress = False
        self.setAcceptHoverEvents(True)
        self.image_hover_frame = QGraphicsRectItem(self)
        self.image_hover_frame.setPen(QPen(QColor("#2563eb"), 1.5, Qt.DashLine))
        self.image_hover_frame.setBrush(Qt.NoBrush)
        self.image_hover_frame.setAcceptedMouseButtons(Qt.NoButton)
        self.image_hover_frame.setZValue(90)
        self.image_hover_frame.setVisible(False)
        self.image_resize_handle = ImageResizeHandle(self)

    def _commit_image_resize(self) -> None:
        raise NotImplementedError("Subclass must persist the resized image HTML")

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
            owner = getattr(self, "owner", None)
            if _qt_valid(owner):
                owner.update()
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


    def _update_image_hover(self) -> None:
        rect = self._image_rect()
        if rect is None:
            self.image_hover_frame.setVisible(False)
            self.image_resize_handle.setVisible(False)

            # Falls kein Bild aktiv ist, darf der normale Knotengriff wieder
            # erscheinen.
            node_handle = getattr(getattr(self, "owner", None), "resize_handle", None)
            if _qt_valid(node_handle):
                node_handle.setVisible(bool(self.owner.isSelected()))
            return

        # Hide an optional node resize handle so it cannot intercept the
        # inline-image handle. Free text has no enclosing node handle.
        node_handle = getattr(getattr(self, "owner", None), "resize_handle", None)
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
        node_handle = getattr(getattr(self, "owner", None), "resize_handle", None)
        if _qt_valid(node_handle):
            node_handle.setVisible(bool(self.owner.isSelected()))
            if self.owner.isSelected():
                node_handle.setPos(self.owner.rect().bottomRight())


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


class ImageResizeHandle(QGraphicsRectItem):
    """Ziehgriff unten rechts an einem eingebetteten Bild."""

    SIZE = 10.0

    def __init__(self, editor: "ImageScalableTextItem") -> None:
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

        scene = self.editor.scene()
        if scene is None:
            return
        self._snapshot = scene.make_snapshot()
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
        new_width = max(30.0, 20.0 * self._start_width / self._start_height,
                        self._start_width + float(delta.x()))

        aspect = (
            self._start_height / self._start_width
            if self._start_width > 0.0
            else 1.0
        )
        new_height = new_width * aspect

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
        self.editor.scene().commit_snapshot(self._snapshot)
        self._snapshot = None
        self._image_position = -1
        self.editor._update_image_hover()
        event.accept()

