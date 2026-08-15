from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QBrush,
    QIcon,
    QPixmap,
    QFont,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
    QKeyEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QDockWidget,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsTextItem,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
    QInputDialog,
    QMenu,
)

from .model import ProjectModel, new_id
from .richtext_support import clipboard_image_html


SELECTION_COLOR = QColor("#2563eb")
HANDLE_VISIBLE = 8.0
HANDLE_HIT = 32.0
SEGMENT_HIT_WIDTH = 16.0


def _tool_icon(kind: str, size: int = 22) -> QIcon:
    """Kleine, klare Vektor-Icons für das Zeichen-Dock."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor("#30343b"), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    if kind == "select":
        path = QPainterPath()
        path.moveTo(5.0, 3.0)
        path.lineTo(5.0, 16.0)
        path.lineTo(8.5, 12.5)
        path.lineTo(11.5, 19.0)
        path.lineTo(14.0, 17.8)
        path.lineTo(11.0, 11.5)
        path.lineTo(16.0, 11.2)
        path.closeSubpath()
        painter.setBrush(QColor("#30343b"))
        painter.drawPath(path)
    elif kind == "rectangle":
        painter.drawRect(QRectF(4.0, 5.0, 14.0, 11.0))
    elif kind == "line":
        painter.drawLine(QPointF(4.0, 17.0), QPointF(18.0, 5.0))
    elif kind == "arrow":
        painter.drawLine(QPointF(4.0, 17.0), QPointF(17.0, 6.0))
        painter.drawLine(QPointF(17.0, 6.0), QPointF(11.5, 6.5))
        painter.drawLine(QPointF(17.0, 6.0), QPointF(16.2, 11.5))
    elif kind == "text":
        font = QFont()
        font.setBold(True)
        font.setPointSize(13)
        painter.setFont(font)
        painter.drawText(QRectF(2.0, 1.0, 18.0, 20.0), Qt.AlignCenter, "T")

    painter.end()
    return QIcon(pixmap)


def _drawings(model: ProjectModel) -> dict[str, dict[str, Any]]:
    """Liefert den Zeichenbereich der aktiven Map und legt ihn bei Bedarf an."""
    return model.active_map.setdefault("drawings", {})


class DrawingHandle(QGraphicsRectItem):
    """Kleiner sichtbarer Griff mit deutlich größerer unsichtbarer Trefferfläche."""

    def __init__(self, parent: QGraphicsItem, cursor=Qt.SizeFDiagCursor) -> None:
        half = HANDLE_HIT / 2.0
        super().__init__(-half, -half, HANDLE_HIT, HANDLE_HIT, parent)
        self.setZValue(200.0)
        self.setPen(Qt.NoPen)
        # Fast vollständig transparent, aber nicht NoBrush: so behandelt Qt die
        # gesamte 32x32-Fläche zuverlässig als Maus-Trefferzone.
        hit_brush = QColor(255, 255, 255, 1)
        self.setBrush(hit_brush)
        self.setCursor(cursor)
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setVisible(False)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Nur ein kleines Quadrat zeichnen; die tatsächliche Klickfläche bleibt groß.
        half = HANDLE_VISIBLE / 2.0
        painter.setPen(QPen(SELECTION_COLOR, 1.2))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(QRectF(-half, -half, HANDLE_VISIBLE, HANDLE_VISIBLE))


class DrawingRectHandle(DrawingHandle):
    """Einer von vier Eckgriffen einer Box."""

    CURSORS = {
        "tl": Qt.SizeFDiagCursor,
        "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor,
        "bl": Qt.SizeBDiagCursor,
    }

    def __init__(self, owner: "DrawingRectItem", corner: str) -> None:
        super().__init__(owner, self.CURSORS[corner])
        self.owner = owner
        self.corner = corner
        self._snapshot = None
        self._opposite_scene = QPointF()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._snapshot = self.owner.controller.scene.make_snapshot()
        self.owner.setSelected(True)
        self._opposite_scene = self.owner.opposite_corner_scene(self.corner)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        self.owner.resize_from_corner(self.corner, self._opposite_scene, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self.owner.controller.scene.commit_snapshot(self._snapshot)
        self._snapshot = None
        event.accept()


class DrawingRectItem(QGraphicsRectItem):
    MIN_SIZE = 24.0

    def __init__(self, drawing_id: str, data: dict[str, Any], controller: "DrawingController") -> None:
        super().__init__(0.0, 0.0, float(data.get("width", 180.0)), float(data.get("height", 100.0)))
        self.drawing_id = drawing_id
        self.controller = controller
        self.setPos(float(data.get("x", 0.0)), float(data.get("y", 0.0)))
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.handles = {
            corner: DrawingRectHandle(self, corner)
            for corner in ("tl", "tr", "bl", "br")
        }
        self.setZValue(float(data.get("z", -20.0)))
        self.refresh_style()
        self._update_handles()

    def refresh_style(self) -> None:
        data = self.controller.data(self.drawing_id)
        fill = QColor(str(data.get("fill_color", "#fff2a8")))
        fill.setAlphaF(max(0.0, min(1.0, float(data.get("opacity", 0.30)))))
        self.setBrush(QBrush(fill))
        self.setPen(QPen(QColor(str(data.get("border_color", "#d6b500"))), float(data.get("border_width", 1.5))))
        self.setZValue(float(data.get("z", -20.0)))
        self.update()

    def _update_handles(self) -> None:
        r = self.rect()
        positions = {
            "tl": r.topLeft(),
            "tr": r.topRight(),
            "bl": r.bottomLeft(),
            "br": r.bottomRight(),
        }
        for corner, handle in self.handles.items():
            handle.setPos(positions[corner])

    def opposite_corner_scene(self, corner: str) -> QPointF:
        r = self.rect()
        opposite = {
            "tl": r.bottomRight(),
            "tr": r.bottomLeft(),
            "bl": r.topRight(),
            "br": r.topLeft(),
        }[corner]
        return self.mapToScene(opposite)

    def resize_from_corner(self, corner: str, opposite_scene: QPointF, cursor_scene: QPointF) -> None:
        x1, y1 = opposite_scene.x(), opposite_scene.y()
        x2, y2 = cursor_scene.x(), cursor_scene.y()

        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        width = max(self.MIN_SIZE, right - left)
        height = max(self.MIN_SIZE, bottom - top)

        # Bei Unterschreitung der Mindestgröße den festen Gegenpunkt respektieren.
        if right - left < self.MIN_SIZE:
            if cursor_scene.x() < opposite_scene.x():
                left = opposite_scene.x() - self.MIN_SIZE
            else:
                left = opposite_scene.x()
            width = self.MIN_SIZE
        if bottom - top < self.MIN_SIZE:
            if cursor_scene.y() < opposite_scene.y():
                top = opposite_scene.y() - self.MIN_SIZE
            else:
                top = opposite_scene.y()
            height = self.MIN_SIZE

        self.prepareGeometryChange()
        self.setPos(left, top)
        self.setRect(0.0, 0.0, width, height)

        data = self.controller.data(self.drawing_id)
        data.update({"x": float(left), "y": float(top), "width": float(width), "height": float(height)})
        self.controller.model.touch()
        self._update_handles()
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            data = self.controller.data(self.drawing_id)
            data["x"] = float(value.x())
            data["y"] = float(value.y())
            self.controller.model.touch()
        elif change == QGraphicsItem.ItemSelectedHasChanged and hasattr(self, "handles"):
            for handle in self.handles.values():
                handle.setVisible(bool(value))
            self._update_handles()
            self.update()
        return super().itemChange(change, value)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Rechteck vollständig selbst zeichnen. So vermeiden wir den
        # Qt-Standard-Auswahlrahmen und sind unabhängig von versionsabhängigen
        # QStyleOptionGraphicsItem-State-Flags.
        painter.save()
        painter.setPen(self.pen())
        painter.setBrush(self.brush())
        painter.drawRect(self.rect())

        if self.isSelected():
            painter.setPen(QPen(SELECTION_COLOR, 1.0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.rect())
        painter.restore()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.controller.scene.prepare_selection_click(
                self,
                event.modifiers(),
                node_id=None,
            )
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.controller.context_menu(self.drawing_id, event.screenPos())
        event.accept()


class SegmentEndpointHandle(DrawingHandle):
    """Großzügig greifbarer Endpunkt einer Linie bzw. eines Pfeils."""

    def __init__(self, owner: "DrawingSegmentItem", endpoint: int) -> None:
        super().__init__(owner, Qt.CrossCursor)
        self.owner = owner
        self.endpoint = endpoint
        self._snapshot = None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self.owner.controller.scene.prepare_selection_click(
            self.owner,
            event.modifiers(),
            node_id=None,
        )
        self._snapshot = self.owner.controller.scene.make_snapshot()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        self.owner.move_endpoint(self.endpoint, event.scenePos())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self.owner.controller.scene.commit_snapshot(self._snapshot)
        self._snapshot = None
        event.accept()


class DrawingSegmentItem(QGraphicsPathItem):
    """Gemeinsame Darstellung für Linie und Pfeil mit Inkscape-artigen Endgriffen."""

    def __init__(self, drawing_id: str, data: dict[str, Any], controller: "DrawingController") -> None:
        super().__init__()
        self.drawing_id = drawing_id
        self.controller = controller
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(float(data.get("z", 5.0)))
        self.start_handle = SegmentEndpointHandle(self, 1)
        self.end_handle = SegmentEndpointHandle(self, 2)
        self._rebuild_path(data)
        self.refresh_style()

    def _rebuild_path(self, data: dict[str, Any]) -> None:
        x1 = float(data.get("x1", 0.0))
        y1 = float(data.get("y1", 0.0))
        x2 = float(data.get("x2", x1 + 100.0))
        y2 = float(data.get("y2", y1))
        self.setPos(x1, y1)
        path = QPainterPath(QPointF(0.0, 0.0))
        path.lineTo(QPointF(x2 - x1, y2 - y1))
        self.setPath(path)
        self._update_handles()

    def _update_handles(self) -> None:
        path = self.path()
        if path.elementCount() < 2:
            return
        self.start_handle.setPos(path.pointAtPercent(0.0))
        self.end_handle.setPos(path.pointAtPercent(1.0))

    def shape(self) -> QPainterPath:
        # Die Linie darf optisch dünn bleiben, soll aber leicht anwählbar sein.
        stroker = QPainterPathStroker()
        stroker.setWidth(max(SEGMENT_HIT_WIDTH, self.pen().widthF() + 10.0))
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        # Genug Luft für Pfeilkopf, Auswahl und Trefferzone.
        return self.shape().boundingRect().adjusted(-12.0, -12.0, 12.0, 12.0)

    def refresh_style(self) -> None:
        data = self.controller.data(self.drawing_id)
        self.setPen(QPen(QColor(str(data.get("color", "#333333"))), float(data.get("width", 2.0)), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self.setZValue(float(data.get("z", 5.0)))
        self.update()

    def move_endpoint(self, endpoint: int, scene_pos: QPointF) -> None:
        data = self.controller.data(self.drawing_id)
        if endpoint == 1:
            data["x1"] = float(scene_pos.x())
            data["y1"] = float(scene_pos.y())
        else:
            data["x2"] = float(scene_pos.x())
            data["y2"] = float(scene_pos.y())
        self.controller.model.touch()
        self._rebuild_path(data)
        self.update()

    def paint(self, painter: QPainter, option, widget=None) -> None:
        # Linie/Pfeil zeichnen; Auswahl nur durch Endgriffe und einen sehr dezenten
        # gestrichelten Stroke anzeigen.
        painter.setPen(self.pen())
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())

        data = self.controller.data(self.drawing_id)
        if str(data.get("type", "arrow")) == "arrow":
            path = self.path()
            if path.elementCount() >= 2:
                end = path.pointAtPercent(1.0)
                before = path.pointAtPercent(0.92)
                dx = end.x() - before.x()
                dy = end.y() - before.y()
                length = max(0.001, (dx * dx + dy * dy) ** 0.5)
                ux, uy = dx / length, dy / length
                size = 10.0
                left = QPointF(end.x() - ux * size - uy * size * 0.48, end.y() - uy * size + ux * size * 0.48)
                right = QPointF(end.x() - ux * size + uy * size * 0.48, end.y() - uy * size - ux * size * 0.48)
                painter.drawLine(end, left)
                painter.drawLine(end, right)

        if self.isSelected():
            painter.setPen(QPen(SELECTION_COLOR, 1.0, Qt.DashLine, Qt.RoundCap))
            painter.drawPath(self.path())

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            data = self.controller.data(self.drawing_id)
            old_x1 = float(data.get("x1", 0.0))
            old_y1 = float(data.get("y1", 0.0))
            dx = float(value.x()) - old_x1
            dy = float(value.y()) - old_y1
            data["x1"] = float(value.x())
            data["y1"] = float(value.y())
            data["x2"] = float(data.get("x2", old_x1)) + dx
            data["y2"] = float(data.get("y2", old_y1)) + dy
            self.controller.model.touch()
        elif change == QGraphicsItem.ItemSelectedHasChanged and hasattr(self, "start_handle"):
            visible = bool(value)
            self.start_handle.setVisible(visible)
            self.end_handle.setVisible(visible)
            self._update_handles()
            self.update()
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.controller.scene.prepare_selection_click(
                self,
                event.modifiers(),
                node_id=None,
            )
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.controller.context_menu(self.drawing_id, event.screenPos())
        event.accept()


# Abwärtskompatibler Name für bestehenden Code/ältere Pickups.
DrawingArrowItem = DrawingSegmentItem


class DrawingTextItem(QGraphicsTextItem):
    """Freies Rich-Text-Objekt mit derselben Formatleiste wie Knotentext."""

    def __init__(self, drawing_id: str, data: dict[str, Any], controller: "DrawingController") -> None:
        super().__init__()
        self.drawing_id = drawing_id
        self.controller = controller
        html = str(data.get("html", "") or "")
        if html:
            self.setHtml(html)
        else:
            self.setPlainText(str(data.get("text", "Text")))
        self.setDefaultTextColor(QColor(str(data.get("color", "#333333"))))
        self.setPos(float(data.get("x", 0.0)), float(data.get("y", 0.0)))
        self.setZValue(float(data.get("z", 5.0)))
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self._original_html = self.toHtml()
        self._snapshot = None

    def begin_edit(self, select_all: bool = False) -> None:
        scene = self.controller.scene

        # Es darf zu jedem Zeitpunkt genau einen aktiven Texteditor geben.
        previous = scene.editing_item
        if previous is not None and previous is not self:
            previous.finish_edit()

        scene.clearSelection()
        self.setSelected(True)
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setFocus(Qt.OtherFocusReason)
        self._snapshot = scene.make_snapshot()
        self._original_html = self.toHtml()
        scene.editing_item = self
        if select_all:
            cursor = self.textCursor()
            cursor.select(QTextCursor.Document)
            self.setTextCursor(cursor)

    def finish_edit(self) -> None:
        data = self.controller.data(self.drawing_id)
        html = self.toHtml()
        plain = self.toPlainText()
        if html != self._original_html:
            data["html"] = html
            data["text"] = plain
            self.controller.model.touch()
            self.controller.scene.commit_snapshot(self._snapshot)
        self._snapshot = None
        # Wie bei Inkscape/Office: nach Verlassen der Texteingabe bleibt keine
        # alte blaue Textmarkierung auf der Map zurück.
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.clearFocus()
        if self.controller.scene.editing_item is self:
            self.controller.scene.editing_item = None

    def toggle_bold(self) -> None:
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontWeight(
            QFont.Weight.Normal
            if int(cursor.charFormat().fontWeight()) >= int(QFont.Weight.Bold)
            else QFont.Weight.Bold
        )
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def toggle_italic(self) -> None:
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontItalic(not cursor.charFormat().fontItalic())
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def toggle_underline(self) -> None:
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not cursor.charFormat().fontUnderline())
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def set_font_point_size(self, size: float) -> None:
        if size <= 0:
            return
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(size))
        cursor.mergeCharFormat(fmt)
        self.setTextCursor(cursor)

    def set_text_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        cursor = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setForeground(color)
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
        """Paste text, HTML or screenshots exactly like node rich text."""
        mime = QApplication.clipboard().mimeData()
        cursor = self.textCursor()

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
        return True

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.scene() is not None:
            data = self.controller.data(self.drawing_id)
            data["x"] = float(value.x())
            data["y"] = float(value.y())
            self.controller.model.touch()
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.LeftButton
            and not (self.textInteractionFlags() & Qt.TextEditorInteraction)
        ):
            self.controller.scene.prepare_selection_click(
                self,
                event.modifiers(),
                node_id=None,
            )
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.begin_edit(select_all=False)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.Paste):
            if self.paste_from_clipboard():
                event.accept()
                return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers()
            if modifiers == Qt.ControlModifier or modifiers == Qt.ShiftModifier:
                self.finish_edit()
                event.accept()
                return
        if event.key() == Qt.Key_Escape:
            self.setHtml(self._original_html)
            self.finish_edit()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self.textInteractionFlags() & Qt.TextEditorInteraction:
            super().contextMenuEvent(event)
            return
        self.controller.context_menu(self.drawing_id, event.screenPos())
        event.accept()


class DrawingController:
    """Verwaltet freie grafische Annotationen einer Map."""

    def __init__(self, scene, model: ProjectModel) -> None:
        self.scene = scene
        self.model = model
        self.mode = "select"
        self.fill_color = "#fff2a8"
        self.line_color = "#3b3b3b"
        self._start: QPointF | None = None
        self._draft_id: str | None = None
        self.items: dict[str, QGraphicsItem] = {}
        self._mode_listeners: list[Any] = []

    def set_model(self, model: ProjectModel) -> None:
        self.model = model

    def data(self, drawing_id: str) -> dict[str, Any]:
        return _drawings(self.model).get(drawing_id, {})

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        for callback in list(self._mode_listeners):
            try:
                callback(mode)
            except (RuntimeError, ReferenceError):
                pass

    def add_mode_listener(self, callback) -> None:
        if callback not in self._mode_listeners:
            self._mode_listeners.append(callback)

    def select_mode(self) -> None:
        """Zurück in den normalen Auswahl-/Bearbeitungsmodus."""
        self.set_mode("select")

    def rebuild(self) -> None:
        self.items.clear()
        for drawing_id, data in _drawings(self.model).items():
            dtype = str(data.get("type", ""))
            if dtype == "rectangle":
                item = DrawingRectItem(drawing_id, data, self)
            elif dtype in {"arrow", "line"}:
                item = DrawingSegmentItem(drawing_id, data, self)
            elif dtype == "text":
                item = DrawingTextItem(drawing_id, data, self)
            else:
                continue
            self.scene.addItem(item)
            self.items[drawing_id] = item

    def _new_rectangle(self, pos: QPointF) -> str:
        drawing_id = new_id("drawing")
        _drawings(self.model)[drawing_id] = {
            "id": drawing_id,
            "type": "rectangle",
            "x": float(pos.x()), "y": float(pos.y()),
            "width": 1.0, "height": 1.0,
            "fill_color": self.fill_color,
            "border_color": self.line_color,
            "border_width": 1.5,
            "opacity": 0.30,
            "z": -20.0,
            "anchor_object_id": None,
            "offset_x": 0.0, "offset_y": 0.0,
        }
        self.model.touch()
        return drawing_id

    def _new_segment(self, pos: QPointF, drawing_type: str) -> str:
        drawing_id = new_id("drawing")
        _drawings(self.model)[drawing_id] = {
            "id": drawing_id,
            "type": drawing_type,
            "x1": float(pos.x()), "y1": float(pos.y()),
            "x2": float(pos.x()), "y2": float(pos.y()),
            "color": self.line_color,
            "width": 2.0,
            "z": 5.0,
            "anchor_object_id": None,
            "offset_x": 0.0, "offset_y": 0.0,
        }
        self.model.touch()
        return drawing_id

    def _new_text(self, pos: QPointF) -> None:
        self.scene.push_undo()
        drawing_id = new_id("drawing")
        _drawings(self.model)[drawing_id] = {
            "id": drawing_id,
            "type": "text",
            "x": float(pos.x()), "y": float(pos.y()),
            "text": "Text",
            "html": "",
            "color": self.line_color,
            "z": 5.0,
            "anchor_object_id": None,
            "offset_x": 0.0, "offset_y": 0.0,
        }
        self.model.touch()
        item = DrawingTextItem(drawing_id, self.data(drawing_id), self)
        self.scene.addItem(item)
        self.items[drawing_id] = item
        item.begin_edit(select_all=True)

    def _clicked_handle(self, event) -> bool:
        """True, wenn der Klick auf einem vorhandenen Zeichen-Griff liegt.

        Auch wenn gerade Box/Linie/Pfeil als Werkzeug aktiv ist, haben die
        Bearbeitungsgriffe Vorrang. Sonst würde der Controller beim Versuch,
        einen Endpunkt zu ziehen, stattdessen ein neues Objekt beginnen.
        """
        views = self.scene.views()
        transform = views[0].transform() if views else None
        clicked = self.scene.itemAt(event.scenePos(), transform)
        return isinstance(clicked, DrawingHandle)

    def mouse_press(self, event) -> bool:
        if event.button() != Qt.LeftButton:
            return False
        if self._clicked_handle(event):
            return False
        if self.mode == "select":
            return False
        pos = event.scenePos()
        if self.mode == "text":
            self._new_text(pos)
            self.select_mode()
            event.accept()
            return True
        self.scene.push_undo()
        self._start = QPointF(pos)
        if self.mode == "rectangle":
            self._draft_id = self._new_rectangle(pos)
        elif self.mode in {"arrow", "line"}:
            self._draft_id = self._new_segment(pos, self.mode)
        else:
            return False
        data = self.data(self._draft_id)
        if self.mode == "rectangle":
            item = DrawingRectItem(self._draft_id, data, self)
        else:
            item = DrawingSegmentItem(self._draft_id, data, self)
        self.scene.addItem(item)
        self.items[self._draft_id] = item
        # Das gerade gezeichnete Objekt bleibt sichtbar ausgewählt. Dadurch
        # ist auch eine weiße/sehr transparente Box sofort erkennbar und ihre
        # Griffe stehen direkt zur Verfügung.
        self.scene.clearSelection()
        item.setSelected(True)
        event.accept()
        return True

    def mouse_move(self, event) -> bool:
        if self._draft_id is None or self._start is None:
            return False
        data = self.data(self._draft_id)
        pos = event.scenePos()
        if data.get("type") == "rectangle":
            left = min(self._start.x(), pos.x())
            top = min(self._start.y(), pos.y())
            data["x"] = float(left)
            data["y"] = float(top)
            data["width"] = max(1.0, abs(float(pos.x() - self._start.x())))
            data["height"] = max(1.0, abs(float(pos.y() - self._start.y())))
            item = self.items.get(self._draft_id)
            if isinstance(item, DrawingRectItem):
                item.setPos(data["x"], data["y"])
                item.setRect(0.0, 0.0, data["width"], data["height"])
                item._update_handles()
        elif data.get("type") in {"arrow", "line"}:
            data["x2"] = float(pos.x())
            data["y2"] = float(pos.y())
            item = self.items.get(self._draft_id)
            if isinstance(item, DrawingSegmentItem):
                item._rebuild_path(data)
        self.model.touch()
        event.accept()
        return True

    def mouse_release(self, event) -> bool:
        if self._draft_id is None:
            return False
        finished_id = self._draft_id
        self._draft_id = None
        self._start = None
        item = self.items.get(finished_id)
        if item is not None:
            self.scene.clearSelection()
            item.setSelected(True)
        # Zeichenwerkzeuge sind Einmal-Werkzeuge: Nach dem Erzeugen sofort
        # zurück zur Auswahl. So kann das neue Objekt direkt bearbeitet werden.
        self.select_mode()
        event.accept()
        return True

    def delete(self, drawing_id: str) -> None:
        if drawing_id not in _drawings(self.model):
            return
        self.scene.push_undo()
        del _drawings(self.model)[drawing_id]
        self.model.touch()
        self.scene.rebuild()

    def selected_drawing_ids(self) -> list[str]:
        ids: list[str] = []
        for item in self.scene.selectedItems():
            drawing_id = getattr(item, "drawing_id", None)
            if drawing_id and drawing_id in _drawings(self.model) and drawing_id not in ids:
                ids.append(drawing_id)
        return ids

    def apply_fill_color(self, color: str) -> bool:
        changed = False
        selected = self.selected_drawing_ids()
        if not selected:
            self.fill_color = color
            return False
        self.scene.push_undo()
        for drawing_id in selected:
            data = self.data(drawing_id)
            if data.get("type") == "rectangle":
                data["fill_color"] = color
                item = self.items.get(drawing_id)
                if isinstance(item, DrawingRectItem):
                    item.refresh_style()
                changed = True
        if changed:
            self.model.touch()
        return changed

    def apply_line_color(self, color: str) -> bool:
        changed = False
        selected = self.selected_drawing_ids()
        if not selected:
            self.line_color = color
            return False
        self.scene.push_undo()
        for drawing_id in selected:
            data = self.data(drawing_id)
            dtype = data.get("type")
            if dtype == "rectangle":
                data["border_color"] = color
                item = self.items.get(drawing_id)
                if isinstance(item, DrawingRectItem):
                    item.refresh_style()
                changed = True
            elif dtype in {"arrow", "line"}:
                data["color"] = color
                item = self.items.get(drawing_id)
                if isinstance(item, DrawingSegmentItem):
                    item.refresh_style()
                changed = True
            elif dtype == "text":
                data["color"] = color
                item = self.items.get(drawing_id)
                if isinstance(item, DrawingTextItem):
                    item.setDefaultTextColor(QColor(color))
                changed = True
        if changed:
            self.model.touch()
        return changed

    def context_menu(self, drawing_id: str, screen_pos) -> None:
        data = self.data(drawing_id)
        menu = QMenu()
        delete_action = QAction("Löschen", menu)
        front_action = QAction("In den Vordergrund", menu)
        back_action = QAction("In den Hintergrund", menu)
        menu.addAction(front_action)
        menu.addAction(back_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        chosen = menu.exec(screen_pos)
        if chosen is delete_action:
            self.delete(drawing_id)
        elif chosen is front_action:
            self.scene.push_undo(); data["z"] = 20.0; self.model.touch(); self.scene.rebuild()
        elif chosen is back_action:
            self.scene.push_undo(); data["z"] = -20.0; self.model.touch(); self.scene.rebuild()


class DrawingDock(QDockWidget):
    """Kompakte Werkzeugpalette für freie Zeichenelemente."""

    def __init__(self, controller: DrawingController, parent=None) -> None:
        super().__init__("Zeichnen", parent)
        self.setObjectName("drawing_dock")
        self.controller = controller

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(6)

        layout.addWidget(QLabel("Werkzeug"))
        row = QHBoxLayout()
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons: dict[str, QToolButton] = {}
        for mode in ("select", "rectangle", "line", "arrow", "text"):
            button = QToolButton()
            button.setIcon(_tool_icon(mode))
            button.setIconSize(QPixmap(22, 22).size())
            button.setCheckable(True)
            button.setFixedSize(38, 34)
            button.setToolTip({
                "select": "Auswählen / verschieben",
                "rectangle": "Farbige Box aufziehen",
                "line": "Einfache Linie aufziehen",
                "arrow": "Pfeil aufziehen",
                "text": "Freien Text setzen",
            }[mode])
            button.clicked.connect(lambda _checked=False, m=mode: self.controller.set_mode(m))
            self.tool_group.addButton(button)
            self.tool_buttons[mode] = button
            row.addWidget(button)
            if mode == "select":
                button.setChecked(True)
        layout.addLayout(row)
        self.controller.add_mode_listener(self._sync_mode_button)

        self.mode_label = QLabel("Auswahl")
        self.mode_label.setStyleSheet("color: #6b7280; padding: 2px 1px;")
        layout.addWidget(self.mode_label)
        layout.addStretch(1)

        self.setWidget(panel)

    def _sync_mode_button(self, mode: str) -> None:
        button = self.tool_buttons.get(mode)
        if button is not None:
            button.setChecked(True)
        names = {
            "select": "Auswahl",
            "rectangle": "Box zeichnen",
            "line": "Linie zeichnen",
            "arrow": "Pfeil zeichnen",
            "text": "Text setzen",
        }
        self.mode_label.setText(names.get(mode, "Auswahl"))

    def _choose_fill(self) -> None:
        color = QColorDialog.getColor(QColor(self.controller.fill_color), self, "Flächenfarbe")
        if color.isValid():
            self.controller.apply_fill_color(color.name())
            self.controller.fill_color = color.name()

    def _choose_line(self) -> None:
        color = QColorDialog.getColor(QColor(self.controller.line_color), self, "Linienfarbe")
        if color.isValid():
            self.controller.apply_line_color(color.name())
            self.controller.line_color = color.name()
