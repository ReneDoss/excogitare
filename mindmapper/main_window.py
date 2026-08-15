from __future__ import annotations

from pathlib import Path
import math

from dataclasses import dataclass

from PySide6.QtCore import QFileInfo, QSettings, QSize, QSizeF, QRectF, QMarginsF, QTimer, Qt, QUrl
import shiboken6
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QDesktopServices, QIcon, QPainter, QPen, QPixmap, QTextCursor, QImage, QPdfWriter, QPageSize, QPageLayout, QTextCharFormat, QTextListFormat
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QToolBar,
    QDockWidget,
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QComboBox,
    QPushButton,
    QColorDialog,
    QHBoxLayout,
    QSpinBox,
    QAbstractSpinBox,
    QInputDialog,
    QSizePolicy,
    QGridLayout,
    QGroupBox,
    QToolButton,
    QScrollArea,
    QFrame,
    QTabBar,
    QPlainTextEdit,
    QTextEdit,
    QListWidget,
    QListWidgetItem,
    QFileIconProvider,
    QStyle,
)

from . import APP_NAME, APP_VERSION, FORMAT_VERSION
from .graphics import MapScene
from .migrations import UnsupportedFormatError
from .model import ProjectModel
from .storage import load_project, save_project
from .view import MapView, MiniMapView
from .drawing import DrawingDock
from .richtext_support import clipboard_image_html

def _make_format_painter_icon(size: int = 18) -> QIcon:
    """Kleiner neutraler Formatpinsel ohne Abhängigkeit von Emoji-Fonts."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    pen = QPen(QColor("#4b5563"), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)

    # Pinselstiel
    painter.drawLine(4, 14, 12, 6)
    painter.drawLine(6, 16, 14, 8)

    # Pinselkopf
    painter.setBrush(QColor("#d9a441"))
    painter.drawRect(11, 3, 5, 6)

    # kleine Farbkante
    painter.setPen(QPen(QColor("#2563eb"), 2.0))
    painter.drawLine(3, 15, 6, 15)
    painter.end()
    return QIcon(pixmap)


class RichNoteEdit(QTextEdit):
    """Dock-based rich-text editor with the same formatting commands as map text."""

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
        """Paste via toolbar/menu using the common rich-text clipboard path."""
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

    def insertFromMimeData(self, source) -> None:
        """Handle normal Ctrl+V directly inside the note editor.

        QTextEdit's default paste path does not reliably embed clipboard images
        as self-contained HTML. Node and drawing text use Excogitare's own image
        conversion already; notes must do the same when Ctrl+V is pressed.
        """
        if source is not None and source.hasImage():
            image_html = clipboard_image_html()
            if image_html is not None:
                cursor = self.textCursor()
                cursor.insertHtml(image_html)
                self.setTextCursor(cursor)
                return

        # HTML and plain text continue through Qt's normal rich-text handling.
        super().insertFromMimeData(source)


@dataclass
class DocumentState:
    model: ProjectModel
    path: Path | None = None

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("Dossmatik", "MindMapper")
        self.model = ProjectModel.create()
        self.current_path: Path | None = None

        self.documents: list[DocumentState] = [
            DocumentState(
            model=self.model,
            path=self.current_path,
            )
        ]
        self.active_document_index = 0

        self.scene = MapScene(self.model)
        self._format_painter_payload = None
        self.scene.format_painter_handler = self._apply_format_painter_to_item
        self.scene.format_painter_cancel_handler = self._cancel_format_painter
 #       self.model = ProjectModel.create()
 #       self.current_path: Path | None = None
 #       self.scene = MapScene(self.model)
        self.view = MapView(self.scene)
        self._closing = False
        self._create_map_tabs()
        self._create_sidebars()
        self.scene.details_request_handler = self._show_node_details
        self.scene.selectionChanged.connect(self._update_properties)

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1200, 760)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._restore_window_layout()
        self._format_timer = QTimer(self)
        self._format_timer.timeout.connect(self._update_format_controls)
        self._format_timer.timeout.connect(self._refresh_details)
        self._format_timer.start(150)
        self._update_status()

        # Beim ersten Start sofort einen Wurzelknoten anbieten.
        # QTimer stellt sicher, dass die Ansicht bereits ihre endgültige
        # Größe besitzt und der Knoten wirklich in der sichtbaren Mitte landet.
        QTimer.singleShot(0, self._create_initial_root_node)

    def _create_map_tabs(self) -> None:
        """Erzeugt die Reiterleiste für mehrere Maps eines Projekts."""
        central = QWidget()
        central.setObjectName("map_central_widget")
        central.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.map_tabs = QTabBar()
        self.map_tabs.setObjectName("map_tabs")
        self.map_tabs.setMovable(True)
        self.map_tabs.setTabsClosable(True)
        self.map_tabs.setExpanding(False)
        self.map_tabs.setElideMode(Qt.ElideRight)
        self.map_tabs.setUsesScrollButtons(True)
        self.map_tabs.currentChanged.connect(self._on_map_tab_changed)
        self.map_tabs.tabCloseRequested.connect(self._close_map_tab)
    #    self.map_tabs.tabBarDoubleClicked.connect(self._rename_map_tab)
        self.map_tabs.tabMoved.connect(self._keep_plus_tab_last)
        self.map_tabs.setStyleSheet(
            "QTabBar#map_tabs { background: #eef1f5; border-bottom: 1px solid #c8ced7; }"
            "QTabBar#map_tabs::tab { min-width: 95px; max-width: 210px; min-height: 25px; max-height: 25px; "
            "padding: 0px 9px; margin: 1px 1px 0 1px; background: #dde2e9; "
            "border: 1px solid #c8ced7; border-bottom: 0; border-top-left-radius: 5px; "
            "border-top-right-radius: 5px; }"
            "QTabBar#map_tabs::tab:selected { background: white; }"
            "QTabBar#map_tabs::tab:hover { background: #f7f8fa; }"
        )
        self.map_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view.setMinimumSize(0, 0)
        layout.addWidget(self.map_tabs)
        layout.addWidget(self.view, 1)
        layout.setStretch(0, 0)
        layout.setStretch(1, 1)
        self.setCentralWidget(central)
        self._refresh_map_tabs()

    def _refresh_map_tabs(self) -> None:
        if not hasattr(self, "map_tabs"):
            return

        self.map_tabs.blockSignals(True)

        while self.map_tabs.count():
            self.map_tabs.removeTab(0)

        for index, document in enumerate(self.documents):
            if document.path is None:
                tab_name = "Unbenannt"
            else:
                tab_name = document.path.stem

            self.map_tabs.addTab(tab_name)
            self.map_tabs.setTabData(index, index)

        plus_index = self.map_tabs.addTab("+")
        self.map_tabs.setTabData(plus_index, None)
        self.map_tabs.setTabToolTip(plus_index, "Neue Map anlegen")
        self.map_tabs.setTabButton(
            plus_index,
            QTabBar.ButtonPosition.RightSide,
            None,
        )

        self.map_tabs.setCurrentIndex(self.active_document_index)
        self.map_tabs.blockSignals(False)

    def _on_map_tab_changed(self, index: int) -> None:
        if index < 0:
            return

        document_index = self.map_tabs.tabData(index)

        if document_index is None:
            self._add_new_document()
            return

        document_index = int(document_index)

        if document_index < 0 or document_index >= len(self.documents):
            return

        self.active_document_index = document_index

        document = self.documents[document_index]

        self.model = document.model
        self.current_path = document.path

        self.scene.model = self.model
        self.scene.active_node_id = None
        self.scene.clear_history()
        self.scene.rebuild()

        self._update_properties()
        self._update_status()

        if self.current_path is None:
            self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        else:
            self.setWindowTitle(
                f"{APP_NAME} {APP_VERSION} – {self.current_path.name}"
            )

        if not self.model.active_map.get("object_states"):
            QTimer.singleShot(0, self._create_initial_root_node)

    def _add_new_document(self) -> None:
        new_model = ProjectModel.create()

        document = DocumentState(
            model=new_model,
            path=None,
        )

        self.documents.append(document)
        self.active_document_index = len(self.documents) - 1

        self.model = new_model
        self.current_path = None

        self.scene.model = self.model
        self.scene.clear_history()
        self.scene.active_node_id = None
        self.scene.rebuild()

        self._refresh_map_tabs()
        self._update_properties()
        self._update_status()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")

        QTimer.singleShot(0, self._create_initial_root_node)




    def _add_new_map(self) -> None:
        map_id = self.model.add_map()
        self.scene.active_node_id = None
        self.scene.clear_history()
        self.scene.rebuild()
        self._refresh_map_tabs()
        for index in range(self.map_tabs.count()):
            if self.map_tabs.tabData(index) == map_id:
                self.map_tabs.setCurrentIndex(index)
                break
        QTimer.singleShot(0, self._create_initial_root_node)
        self._update_properties()
        self._update_status()

    def _close_map_tab(self, index: int) -> None:
        document_index = self.map_tabs.tabData(index)

        # Das Plus-Tab kann nicht geschlossen werden.
        if document_index is None:
            return

        document_index = int(document_index)

        if document_index < 0 or document_index >= len(self.documents):
            return

        document = self.documents[document_index]

        if document.path is None:
            document_name = "Unbenannt"
        else:
            document_name = document.path.stem

        answer = QMessageBox.question(
            self,
            "Map schließen",
            f'Soll "{document_name}" wirklich geschlossen werden?',
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )

        if answer != QMessageBox.Yes:
            return

        # Dokument nur aus Excogitare schließen.
        # Die .mmproj-Datei auf der Festplatte bleibt erhalten.
        self.documents.pop(document_index)

        # Es soll immer mindestens ein Dokument geöffnet bleiben.
        if not self.documents:
            new_model = ProjectModel.create()
            self.documents.append(
                DocumentState(
                    model=new_model,
                    path=None,
                )
            )

        self.active_document_index = min(
            document_index,
            len(self.documents) - 1,
        )

        document = self.documents[self.active_document_index]

        self.model = document.model
        self.current_path = document.path

        self.scene.model = self.model
        self.scene.active_node_id = None
        self.scene.clear_history()
        self.scene.rebuild()

        self._refresh_map_tabs()
        self._update_properties()
        self._update_status()

        if self.current_path is None:
            self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        else:
            self.setWindowTitle(
                f"{APP_NAME} {APP_VERSION} – {self.current_path.name}"
            )

        if not self.model.active_map.get("object_states"):
            QTimer.singleShot(0, self._create_initial_root_node)

    def _keep_plus_tab_last(self, _from: int, _to: int) -> None:
        plus_index = next((i for i in range(self.map_tabs.count())
                           if self.map_tabs.tabData(i) is None), -1)
        if plus_index >= 0 and plus_index != self.map_tabs.count() - 1:
            self.map_tabs.blockSignals(True)
            self.map_tabs.moveTab(plus_index, self.map_tabs.count() - 1)
            self.map_tabs.blockSignals(False)

        # Die visuelle Reihenfolge der Reiter auch in der Projektdatei bewahren.
        ordered_ids = [self.map_tabs.tabData(i) for i in range(self.map_tabs.count())
                       if self.map_tabs.tabData(i) is not None]
        maps = self.model.data["maps"]
        self.model.data["maps"] = {map_id: maps[map_id] for map_id in ordered_ids
                                   if map_id in maps}
        self.model.touch()

    def _create_sidebars(self) -> None:
        self.properties_dock = QDockWidget("Eigenschaften", self)
        self.properties_dock.setObjectName("properties_dock")
        self.properties_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()

        self.title_edit = QLineEdit()
        self.title_edit.editingFinished.connect(self._apply_title)
        form.addRow("Text", self.title_edit)

        self.type_combo = QComboBox()
        for key, preset in self.model.NODE_TYPE_PRESETS.items():
            self.type_combo.addItem(
                f"{preset['symbol']} {preset['label']}".strip(),
                key,
            )
        self.type_combo.currentIndexChanged.connect(self._apply_node_type)
        form.addRow("Typ", self.type_combo)

        self.status_combo = QComboBox()
        self.status_combo.addItem("Kein Status", "")
        self.status_combo.addItem("◐ In Arbeit", "working")
        self.status_combo.addItem("⏳ Warten", "waiting")
        self.status_combo.addItem("✓ Erledigt", "done")
        self.status_combo.currentIndexChanged.connect(self._apply_status)
        form.addRow("Status", self.status_combo)

        self.role_combo = QComboBox()
        self.role_combo.addItem("Thema", "topic")
        self.role_combo.addItem("Abschnitt", "section")
        self.role_combo.addItem("Aufzählung", "list_item")
        self.role_combo.addItem("Notiz", "note")
        self.role_combo.addItem("Verweis", "reference")
        self.role_combo.currentIndexChanged.connect(self._apply_role)
        form.addRow("Inhaltsart", self.role_combo)

        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Automatisch", "auto")
        self.layout_combo.addItem("Manuell", "manual")
        self.layout_combo.currentIndexChanged.connect(self._apply_layout_mode)
        form.addRow("Position", self.layout_combo)

        layout.addLayout(form)

        color_row = QHBoxLayout()
        self.fill_button = QPushButton("Hintergrund …")
        self.fill_button.clicked.connect(
            lambda: self._choose_property_color("fill_color")
        )
        self.text_button = QPushButton("Text …")
        self.text_button.clicked.connect(
            lambda: self._choose_property_color("text_color")
        )
        color_row.addWidget(self.fill_button)
        color_row.addWidget(self.text_button)
        layout.addLayout(color_row)

        self.border_button = QPushButton("Rahmenfarbe …")
        self.border_button.clicked.connect(
            lambda: self._choose_property_color("border_color")
        )
        layout.addWidget(self.border_button)

        self.auto_button = QPushButton("Knoten wieder automatisch anordnen")
        self.auto_button.clicked.connect(self._restore_auto_layout)
        layout.addWidget(self.auto_button)
        layout.addStretch(1)
        self.properties_dock.setWidget(panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.properties_dock)

        self._create_symbols_dock()
        self._create_details_dock()

        self.drawing_dock = DrawingDock(self.scene.drawing_controller, self)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.drawing_dock)

        self.overview_dock = QDockWidget("Übersicht", self)
        self.overview_dock.setObjectName("overview_dock")
        self.symbols_dock.setObjectName("symbolsDock")
        self.overview = MiniMapView(self.scene, self.view)
        self.overview_dock.setWidget(self.overview)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.overview_dock)
        # Die Übersicht ist nur noch ein optionales Werkzeug. Standardmäßig
        # erhält die Symbolbibliothek den frei werdenden Platz.
        self.overview_dock.hide()
        self.resizeDocks(
            [self.symbols_dock, self.drawing_dock, self.properties_dock],
            [420, 190, 260],
            Qt.Vertical,
        )
        self._update_properties()

    def _create_details_dock(self) -> None:
        """Detailansicht fuer Notizen und Anhaenge des ausgewaehlten Knotens."""
        self.details_dock = QDockWidget("Knotendetails", self)
        self.details_dock.setObjectName("details_dock")
        self.details_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(6)

        self.details_title = QLabel("Kein Knoten ausgewaehlt")
        title_font = self.details_title.font()
        if title_font.pointSize() <= 0:
            title_font.setPointSize(10)
        title_font.setBold(True)
        self.details_title.setFont(title_font)
        layout.addWidget(self.details_title)

        note_header = QHBoxLayout()
        note_icon = QLabel()
        note_icon.setPixmap(self._detail_icon("note").pixmap(16, 16))
        note_header.addWidget(note_icon)
        note_header.addWidget(QLabel("Notiz"))
        note_header.addStretch(1)
        layout.addLayout(note_header)
        self.note_edit = RichNoteEdit()
        self.note_edit.setAcceptRichText(True)
        self.note_edit.setPlaceholderText("Rich-Text-Notiz zum ausgewählten Knoten …")
        self.note_edit.setMinimumHeight(120)
        self.note_edit.textChanged.connect(self._note_changed)
        layout.addWidget(self.note_edit, 1)

        attachment_header = QHBoxLayout()
        attachment_icon = QLabel()
        attachment_icon.setPixmap(self._detail_icon("attachment").pixmap(16, 16))
        attachment_header.addWidget(attachment_icon)
        attachment_header.addWidget(QLabel("Anhänge"))
        attachment_header.addStretch(1)
        self.attachment_count_label = QLabel("0")
        attachment_header.addWidget(self.attachment_count_label)
        layout.addLayout(attachment_header)

        self.attachment_list = QListWidget()
        self.attachment_list.setAlternatingRowColors(True)
        self.attachment_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_list.itemDoubleClicked.connect(lambda _item: self._open_selected_attachment())
        self.attachment_list.setIconSize(QSize(20, 20))
        self.attachment_list.setSpacing(1)
        self.attachment_list.setMinimumHeight(0)
        self.attachment_list.setMaximumHeight(164)
        self.attachment_list.setVisible(False)
        layout.addWidget(self.attachment_list, 0)

        row = QHBoxLayout()
        self.add_attachment_button = QPushButton("Datei ...")
        self.add_attachment_button.clicked.connect(self._details_add_files)
        self.add_url_button = QPushButton("URL ...")
        self.add_url_button.clicked.connect(self._details_add_url)
        self.open_attachment_button = QPushButton("Oeffnen")
        self.open_attachment_button.clicked.connect(self._open_selected_attachment)
        self.remove_attachment_button = QPushButton("Entfernen")
        self.remove_attachment_button.clicked.connect(self._remove_selected_attachment)
        for button in (self.add_attachment_button, self.add_url_button, self.open_attachment_button, self.remove_attachment_button):
            row.addWidget(button)
        layout.addLayout(row)

        self.details_dock.setWidget(panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.details_dock)
        self.details_dock.resize(340, 500)
        self._details_object_id = None
        self._details_signature = None

    def _detail_icon(self, kind: str) -> QIcon:
        """Kleine gezeichnete Symbole, unabhängig von Emoji-Schriftarten."""
        pixmap = QPixmap(18, 18)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor("#667085"), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if kind == "note":
            painter.drawRoundedRect(3, 2, 12, 14, 2, 2)
            painter.drawLine(6, 6, 12, 6)
            painter.drawLine(6, 9, 12, 9)
            painter.drawLine(6, 12, 10, 12)
        elif kind == "url":
            painter.drawEllipse(2.5, 2.5, 13, 13)
            painter.drawLine(3.5, 9, 14.5, 9)
            painter.drawEllipse(6, 2.5, 6, 13)
        else:
            painter.save(); painter.translate(9, 9); painter.rotate(-28)
            painter.drawRoundedRect(-8, -3, 10, 6, 3, 3)
            painter.drawRoundedRect(-2, -3, 10, 6, 3, 3)
            painter.drawLine(-2, 0, 2, 0); painter.restore()
        painter.end()
        return QIcon(pixmap)

    def _attachment_icon(self, entry: dict) -> QIcon:
        target = str(entry.get("target", ""))
        kind = str(entry.get("type", "file"))
        if kind == "url" or target.lower().startswith(("http://", "https://")):
            return self._detail_icon("url")
        provider = QFileIconProvider()
        path = Path(target).expanduser()
        if kind == "folder" or path.is_dir():
            return provider.icon(QFileIconProvider.Folder)
        icon = provider.icon(QFileInfo(str(path)))
        if not icon.isNull():
            return icon
        return self._detail_icon("attachment")

    def _show_node_details(self, object_id: str, section: str = "note") -> None:
        """Öffnet ein geschlossenes Detail-Dock und springt zum angeklickten Inhalt."""
        item = self.scene.node_items.get(object_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self.scene.set_active_node(object_id)
        self.details_dock.show()
        self.details_dock.raise_()
        self._refresh_details(True)
        if section == "attachments":
            self.attachment_list.setFocus(Qt.MouseFocusReason)
            if self.attachment_list.count() and self.attachment_list.currentRow() < 0:
                self.attachment_list.setCurrentRow(0)
        else:
            self.note_edit.setFocus(Qt.MouseFocusReason)

    def _details_selected_id(self) -> str | None:
        return self._selected_object_id()

    def _note_changed(self) -> None:
        if getattr(self, "_updating_details", False):
            return
        object_id = self._details_selected_id()
        if object_id is None:
            return

        plain = self.note_edit.toPlainText()
        html = self.note_edit.toHtml()

        if self.model.note(object_id) == plain and self.model.note_html(object_id) == html:
            return

        self.model.set_note_rich(object_id, html, plain)

        # Keep the periodic details refresh from resetting the live cursor.
        attachments = self.model.attachments(object_id)
        obj = self.model.data["objects"].get(object_id, {})
        self._details_signature = (
            object_id,
            str(obj.get("title", "")),
            self.model.note_html(object_id),
            plain,
            tuple(
                (
                    str(a.get("id", "")),
                    str(a.get("label", "")),
                    str(a.get("target", "")),
                )
                for a in attachments
            ),
        )
        self._details_object_id = object_id

        item = self.scene.node_items.get(object_id)
        if item is not None:
            item.refresh_details_indicator()

    def _attachment_icon_text(self, entry: dict) -> str:
        target = str(entry.get("target", ""))
        kind = str(entry.get("type", "file"))
        suffix = Path(target).suffix.lower()
        if kind == "url" or target.lower().startswith(("http://", "https://")):
            return "URL"
        if kind == "folder":
            return "DIR"
        if suffix == ".pdf":
            return "PDF"
        if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"}:
            return "IMG"
        if suffix in {".py", ".pyw"}:
            return "PY"
        return "LINK"

    def _refresh_details(self, force: bool = False) -> None:
        object_id = self._details_selected_id()
        if object_id is None:
            signature = None
        else:
            obj = self.model.data["objects"].get(object_id, {})
            attachments = self.model.attachments(object_id)
            signature = (
                object_id,
                str(obj.get("title", "")),
                self.model.note_html(object_id),
                self.model.note(object_id),
                tuple(
                    (
                        str(a.get("id", "")),
                        str(a.get("label", "")),
                        str(a.get("target", "")),
                    )
                    for a in attachments
                ),
            )
        if not force and signature == self._details_signature:
            return
        self._details_signature = signature
        self._details_object_id = object_id
        self._updating_details = True
        try:
            enabled = object_id is not None
            for widget in (self.note_edit, self.attachment_list, self.add_attachment_button, self.add_url_button,
                           self.open_attachment_button, self.remove_attachment_button):
                widget.setEnabled(enabled)
            self.attachment_list.clear()
            if object_id is None:
                self.details_title.setText("Kein Knoten ausgewaehlt")
                self.note_edit.clear()
                self.attachment_count_label.setText("0")
                self.attachment_list.clear()
                self.attachment_list.setVisible(False)
                self.attachment_list.setMinimumHeight(0)
                self.attachment_list.setMaximumHeight(0)
                return
            obj = self.model.data["objects"][object_id]
            self.details_title.setText(str(obj.get("title", "Knoten")))
            note_html = self.model.note_html(object_id)
            if note_html:
                self.note_edit.setHtml(note_html)
            else:
                # Backward compatibility: old .mmproj notes were plain text.
                self.note_edit.setPlainText(self.model.note(object_id))
            attachments = list(self.model.attachments(object_id))
            count = len(attachments)
            self.attachment_count_label.setText(str(count))
            row_height = 30
            for entry in attachments:
                item = QListWidgetItem(self._attachment_icon(entry), str(entry.get("label", "Anhang")))
                item.setData(Qt.UserRole, str(entry.get("id", "")))
                item.setToolTip(str(entry.get("target", "")))
                # Unter Windows muss die Zeilenhoehe explizit sein. Eine Breite
                # von 1 statt 0 verhindert, dass Qt den Textbereich ausblendet.
                item.setSizeHint(QSize(max(120, self.attachment_list.viewport().width() - 4), row_height))
                self.attachment_list.addItem(item)
            if count:
                visible_rows = min(count, 5)
                frame = self.attachment_list.frameWidth() * 2
                spacing = max(0, self.attachment_list.spacing()) * max(0, visible_rows - 1)
                h = frame + visible_rows * row_height + spacing + 2
                self.attachment_list.setMinimumHeight(h)
                self.attachment_list.setMaximumHeight(h)
                self.attachment_list.setVisible(True)
                self.attachment_list.doItemsLayout()
                self.attachment_list.viewport().update()
            else:
                self.attachment_list.setVisible(False)
                self.attachment_list.setMinimumHeight(0)
                self.attachment_list.setMaximumHeight(0)
        finally:
            self._updating_details = False

    def _details_add_files(self) -> None:
        object_id = self._details_selected_id()
        if object_id is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Dateien anhaengen")
        if not paths:
            return
        self.scene.push_undo()
        for path in paths:
            p = Path(path)
            self.model.add_attachment(object_id, "file", str(p), p.name or str(p))
        item = self.scene.node_items.get(object_id)
        if item is not None:
            item.refresh_attachments()
        self._refresh_details(True)

    def _details_add_url(self) -> None:
        object_id = self._details_selected_id()
        if object_id is None:
            return
        url, ok = QInputDialog.getText(self, "Webadresse hinzufuegen", "URL:")
        url = url.strip()
        if not ok or not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        label, ok = QInputDialog.getText(self, "Bezeichnung", "Anzeigename:", text=url)
        if not ok:
            return
        self.scene.push_undo()
        self.model.add_attachment(object_id, "url", url, label.strip() or url)
        item = self.scene.node_items.get(object_id)
        if item is not None:
            item.refresh_attachments()
        self._refresh_details(True)

    def _selected_attachment(self) -> dict | None:
        object_id = self._details_selected_id()
        item = self.attachment_list.currentItem()
        if object_id is None or item is None:
            return None
        attachment_id = str(item.data(Qt.UserRole) or "")
        return next((a for a in self.model.attachments(object_id) if str(a.get("id", "")) == attachment_id), None)

    def _open_selected_attachment(self) -> None:
        entry = self._selected_attachment()
        if not entry:
            return
        target = str(entry.get("target", ""))
        if target.lower().startswith(("http://", "https://")) or str(entry.get("type", "")) == "url":
            QDesktopServices.openUrl(QUrl(target))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target).expanduser())))

    def _remove_selected_attachment(self) -> None:
        object_id = self._details_selected_id()
        entry = self._selected_attachment()
        if object_id is None or not entry:
            return
        self.scene.push_undo()
        self.model.remove_attachment(object_id, str(entry.get("id", "")))
        item = self.scene.node_items.get(object_id)
        if item is not None:
            item.refresh_attachments()
        self._refresh_details(True)

    def _create_symbols_dock(self) -> None:
        """Erzeugt eine kompakte, durchsuchbare und einklappbare Symbolpalette."""
        self.symbols_dock = QDockWidget("Symbole", self)
        self.symbols_dock.setObjectName("symbols_dock")
        self.symbols_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(5)

        self.symbol_search = QLineEdit()
        self.symbol_search.setPlaceholderText("Symbol suchen …")
        self.symbol_search.setClearButtonEnabled(True)
        self.symbol_search.textChanged.connect(self._filter_symbol_palette)
        root_layout.addWidget(self.symbol_search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        palette = QWidget()
        self.symbol_palette_layout = QVBoxLayout(palette)
        self.symbol_palette_layout.setContentsMargins(0, 0, 0, 0)
        self.symbol_palette_layout.setSpacing(3)

        # Ein Symbol kann sowohl in Favoriten als auch in einer Kategorie stehen.
        self.symbol_buttons: dict[str, list[QToolButton]] = {}
        self.symbol_entries: list[tuple[QToolButton, str, str, QWidget]] = []
        self.symbol_sections: list[dict[str, object]] = []

        categories = (
            ("Status", (("○", "Offen"), ("◐", "In Arbeit"), ("●", "Aktiv"),
                        ("✓", "Erledigt"), ("✕", "Fehler"), ("!", "Wichtig"),
                        ("?", "Frage"))),
            ("Priorität", (("1", "Priorität 1"), ("2", "Priorität 2"),
                           ("3", "Priorität 3"), ("↑", "Hoch"), ("↓", "Niedrig"))),
            ("Technik", (("⚙", "Mechanik"), ("🔧", "Werkzeug"), ("💡", "Idee"),
                         ("🧪", "Versuch"), ("📐", "Konstruktion"), ("🔌", "Elektrik"),
                         ("⚡", "Elektronik"), ("💻", "Software"), ("📡", "Kommunikation"),
                         ("📷", "Kamera"))),
            ("Dokument", (("📄", "Dokument"), ("📎", "Anhang"), ("🔗", "Link"),
                          ("📌", "Merker"), ("★", "Favorit"), ("📊", "Tabelle"),
                          ("📈", "Messung"), ("🖼", "Bild"))),
            ("Fortschritt", (("☐", "Nicht begonnen"), ("◔", "25 Prozent"),
                             ("◑", "50 Prozent"), ("◕", "75 Prozent"),
                             ("☑", "Abgeschlossen"))),
        )

        favorites = (("✓", "Erledigt"), ("!", "Wichtig"), ("⚙", "Mechanik"),
                     ("💡", "Idee"), ("📎", "Anhang"), ("📌", "Merker"),
                     ("★", "Favorit"), ("☑", "Abgeschlossen"))
        self._add_symbol_section("★ Favoriten", favorites, expanded=True, collapsible=False)

        for title, entries in categories:
            # Alle Kategorien sind beim Start sichtbar. Bei vielen Symbolen
            # kann der Benutzer einzelne Bereiche weiterhin einklappen.
            self._add_symbol_section(title, entries, expanded=True, collapsible=True)

        self.clear_symbols_button = QPushButton("Alle entfernen")
        self.clear_symbols_button.clicked.connect(self._clear_palette_symbols)
        self.symbol_palette_layout.addWidget(self.clear_symbols_button)
        self.symbol_palette_layout.addStretch(1)

        palette.setStyleSheet(
            "QToolButton#symbolHeader { text-align: left; font-weight: 600; "
            "border: 0; padding: 4px 3px; margin: 0; background: transparent; }"
            "QToolButton#symbolHeader:hover { background: #edf1f6; border-radius: 4px; }"
            "QToolButton#symbolButton { font-size: 11pt; border: 1px solid transparent; "
            "border-radius: 4px; background: transparent; padding: 0; }"
            "QToolButton#symbolButton:hover { background: #e8edf5; border-color: #c8d2df; }"
            "QToolButton#symbolButton:checked { background: #dce9ff; border-color: #6f9fdf; }"
            "QToolButton#symbolButton:disabled { color: #a8adb5; }"
        )
        scroll.setWidget(palette)
        root_layout.addWidget(scroll, 1)
        self.symbols_dock.setWidget(root)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.symbols_dock)

    def _add_symbol_section(
        self,
        title: str,
        entries: tuple[tuple[str, str], ...],
        *,
        expanded: bool,
        collapsible: bool,
    ) -> None:
        """Fügt eine kompakte Rastersektion zur Symbolpalette hinzu."""
        header = QToolButton()
        header.setObjectName("symbolHeader")
        header.setToolButtonStyle(Qt.ToolButtonTextOnly)
        header.setCheckable(collapsible)
        header.setChecked(expanded)
        header.setText(("▾ " if expanded and collapsible else "▸ " if collapsible else "") + title)
        header.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # Die Kategorienamen beginnen konsequent am linken Rand. Das ist
        # platzsparender und lässt sich schneller überfliegen als zentrierte
        # Überschriften.
        self.symbol_palette_layout.addWidget(header, 0, Qt.AlignLeft)

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(2, 0, 2, 2)
        grid.setHorizontalSpacing(2)
        grid.setVerticalSpacing(2)
        for index, (symbol, tooltip) in enumerate(entries):
            button = QToolButton()
            button.setObjectName("symbolButton")
            button.setText(symbol)
            button.setToolTip(tooltip)
            button.setCheckable(True)
            button.setFixedSize(27, 27)
            button.clicked.connect(
                lambda _checked=False, value=symbol: self._toggle_palette_symbol(value)
            )
            grid.addWidget(button, index // 8, index % 8)
            self.symbol_buttons.setdefault(symbol, []).append(button)
            self.symbol_entries.append((button, symbol, tooltip, content))
        content.setVisible(expanded or not collapsible)
        self.symbol_palette_layout.addWidget(content)

        section = {
            "title": title,
            "header": header,
            "content": content,
            "entries": entries,
            "collapsible": collapsible,
        }
        self.symbol_sections.append(section)
        if collapsible:
            header.toggled.connect(
                lambda checked, sec=section: self._set_symbol_section_expanded(sec, checked)
            )

    def _set_symbol_section_expanded(self, section: dict[str, object], expanded: bool) -> None:
        content = section["content"]
        header = section["header"]
        if isinstance(content, QWidget):
            content.setVisible(expanded)
        if isinstance(header, QToolButton):
            header.setText(("▾ " if expanded else "▸ ") + str(section["title"]))

    def _filter_symbol_palette(self, text: str) -> None:
        """Filtert nach Symbol oder Bezeichnung und öffnet passende Kategorien."""
        needle = text.strip().casefold()
        for section in self.symbol_sections:
            content = section["content"]
            header = section["header"]
            if not isinstance(content, QWidget) or not isinstance(header, QToolButton):
                continue
            matched = False
            for button, symbol, tooltip, owner in self.symbol_entries:
                if owner is not content:
                    continue
                visible = not needle or needle in symbol.casefold() or needle in tooltip.casefold()
                button.setVisible(visible)
                matched = matched or visible
            header.setVisible(matched)
            if not matched:
                content.setVisible(False)
            elif needle:
                content.setVisible(True)
                if bool(section["collapsible"]):
                    header.blockSignals(True)
                    header.setChecked(True)
                    header.setText("▾ " + str(section["title"]))
                    header.blockSignals(False)
            elif bool(section["collapsible"]):
                content.setVisible(header.isChecked())
            else:
                content.setVisible(True)

    def _toggle_palette_symbol(self, symbol: str) -> None:
        object_id = self._selected_object_id()
        if object_id is None:
            self._update_symbol_palette()
            if hasattr(self, "details_dock"):
                self._refresh_details(True)
            return
        self.scene.push_undo()
        self.model.toggle_symbol(object_id, symbol)
        self._rebuild_and_reselect(object_id)

    def _clear_palette_symbols(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None or not self.model.symbols(object_id):
            return
        self.scene.push_undo()
        self.model.clear_symbols(object_id)
        self._rebuild_and_reselect(object_id)

    def _update_symbol_palette(self) -> None:
        object_id = self._selected_object_id()
        active = set(self.model.symbols(object_id)) if object_id is not None else set()
        enabled = object_id is not None
        for symbol, buttons in self.symbol_buttons.items():
            for button in buttons:
                button.setEnabled(enabled)
                button.blockSignals(True)
                button.setChecked(symbol in active)
                button.blockSignals(False)
        self.clear_symbols_button.setEnabled(enabled and bool(active))

    def _selected_object_id(self) -> str | None:
        node = self.scene.selected_node()
        return node.object_id if node is not None else None

    def _update_properties(self):
        if self._closing:
            return
        object_id = self._selected_object_id()
        enabled = object_id is not None
        for widget in (
            self.title_edit,
            self.type_combo,
            self.status_combo,
            self.role_combo,
            self.layout_combo,
            self.fill_button,
            self.text_button,
            self.border_button,
            self.auto_button,
        ):
            widget.setEnabled(enabled)
        if object_id is None:
            self.title_edit.setText("")
            self._update_symbol_palette()
            return
        obj = self.model.data["objects"][object_id]
        self.title_edit.blockSignals(True)
        self.title_edit.setText(str(obj.get("title", "")))
        self.title_edit.blockSignals(False)
        node_type = self.model.node_type(object_id)
        idx = self.type_combo.findData(node_type)
        self.type_combo.blockSignals(True)
        self.type_combo.setCurrentIndex(max(0, idx))
        self.type_combo.blockSignals(False)

        self._update_symbol_palette()

        status = self.model.status(object_id)
        idx = self.status_combo.findData(status)
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(max(0, idx))
        self.status_combo.blockSignals(False)

        role = self.model.content_role(object_id)
        idx = self.role_combo.findData(role)
        self.role_combo.blockSignals(True); self.role_combo.setCurrentIndex(max(0, idx)); self.role_combo.blockSignals(False)
        mode = self.model.layout_mode(object_id)
        idx = self.layout_combo.findData(mode)
        self.layout_combo.blockSignals(True); self.layout_combo.setCurrentIndex(max(0, idx)); self.layout_combo.blockSignals(False)
        if hasattr(self, "details_dock"):
            self._refresh_details(True)

    def _apply_title(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None: return
        self.scene.push_undo()
        self.model.set_title(object_id, self.title_edit.text())
        self.scene.rebuild()

    def _rebuild_and_reselect(self, object_id: str) -> None:
        self.scene.rebuild()
        item = self.scene.node_items.get(object_id)
        if item is not None:
            item.setSelected(True)
            self.scene.set_active_node(object_id)
        self._update_properties()

    def _apply_node_type(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None:
            return
        self.scene.push_undo()
        self.model.set_node_type(
            object_id,
            str(self.type_combo.currentData()),
            apply_preset=True,
        )
        self._rebuild_and_reselect(object_id)

    def _apply_status(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None:
            return
        self.scene.push_undo()
        self.model.set_status(object_id, str(self.status_combo.currentData()))
        self._rebuild_and_reselect(object_id)

    def _choose_property_color(self, key: str) -> None:
        object_id = self._selected_object_id()
        if object_id is None:
            return
        state = self.model.active_map["object_states"][object_id]
        color = QColorDialog.getColor(
            QColor(state.get(key, "#ffffff")),
            self,
            "Farbe auswählen",
        )
        if not color.isValid():
            return
        self.scene.push_undo()
        self.model.set_node_color(object_id, key, color.name())
        self._rebuild_and_reselect(object_id)

    def _apply_role(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None:
            return
        item = self.scene.node_items.get(object_id)
        if item is None:
            return
        # Dieselbe vollständige Inhaltsart-Logik wie im Kontextmenü nutzen:
        # Modellwert, Darstellung, Undo und erneute Auswahl.
        item.set_content_role(str(self.role_combo.currentData()))
        self._update_properties()

    def _apply_layout_mode(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None: return
        self.model.set_layout_mode(object_id, str(self.layout_combo.currentData()))

    def _restore_auto_layout(self) -> None:
        object_id = self._selected_object_id()
        if object_id is None: return
        parent_id = self.model.tree_parent(object_id)
        self.scene.push_undo()
        self.model.set_layout_mode(object_id, "auto")
        if parent_id is not None:
            self.model.arrange_hierarchy_from(parent_id)
        self.scene.rebuild()
        self._update_properties()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    _EXPORT_HELPER_CLASS_NAMES = {
        "BranchExitHandle",
        "NodeResizeHandle",
        "ImageResizeHandle",
        "RelationGuideHandle",
    }

    def _export_file_stem(self) -> str:
        if self.current_path is not None:
            return self.current_path.stem
        title = str(self.model.data.get("project", {}).get("title", "")).strip()
        return title or "Excogitare_Map"

    def _prepare_scene_export(self):
        """Blendet ausschließlich Bedienhilfen für den Export aus.

        Die Map selbst, Rich-Text, Bilder, Zeichenflächen und eingeklappte
        Zustände bleiben exakt so erhalten wie in der aktuellen Ansicht.
        """
        selected_items = list(self.scene.selectedItems())
        helper_states: list[tuple[object, bool]] = []

        # Auswahlrahmen/Resize-Griffe verschwinden.
        self.scene.clearSelection()

        for item in self.scene.items():
            class_name = item.__class__.__name__
            if class_name in self._EXPORT_HELPER_CLASS_NAMES:
                helper_states.append((item, item.isVisible()))
                item.setVisible(False)

        return selected_items, helper_states

    def _restore_scene_export(self, state) -> None:
        selected_items, helper_states = state

        for item, was_visible in helper_states:
            try:
                item.setVisible(was_visible)
            except (RuntimeError, ReferenceError):
                pass

        for item in selected_items:
            try:
                item.setSelected(True)
            except (RuntimeError, ReferenceError):
                pass

    def _export_scene_bounds(self, padding: float = 28.0) -> QRectF:
        """Bounds aller sichtbaren Map-Inhalte ohne unsichtbare Bedienhilfen."""
        bounds: QRectF | None = None

        for item in self.scene.items():
            if not item.isVisible():
                continue
            if item.__class__.__name__ in self._EXPORT_HELPER_CLASS_NAMES:
                continue

            try:
                rect = item.sceneBoundingRect()
            except (RuntimeError, ReferenceError):
                continue

            if rect.isNull() or rect.isEmpty():
                continue

            # Vorschaupfad und leere Hilfsobjekte nicht in den Exportbereich ziehen.
            if item is getattr(self.scene, "preview_path", None):
                continue

            bounds = QRectF(rect) if bounds is None else bounds.united(rect)

        if bounds is None:
            return QRectF(-100.0, -100.0, 200.0, 200.0)

        return bounds.adjusted(-padding, -padding, padding, padding)

    def export_pdf(self) -> None:
        """Exportiert die komplette sichtbare Map als eine große PDF-Seite."""
        default_name = f"{self._export_file_stem()}.pdf"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Map als PDF exportieren",
            default_name,
            "PDF-Dateien (*.pdf)",
        )
        if not filename:
            return
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        export_state = self._prepare_scene_export()
        try:
            source = self._export_scene_bounds()
            if source.width() <= 0.0 or source.height() <= 0.0:
                raise RuntimeError("Die Map besitzt keinen exportierbaren Inhalt.")

            # Szene wird als Vektorgrafik auf EINE an die Map angepasste Seite
            # gerendert. 96 Szeneneinheiten werden als ca. 25,4 mm interpretiert.
            mm_per_scene_unit = 25.4 / 96.0
            page_w_mm = max(20.0, source.width() * mm_per_scene_unit)
            page_h_mm = max(20.0, source.height() * mm_per_scene_unit)

            writer = QPdfWriter(filename)
            writer.setTitle(self._export_file_stem())
            writer.setCreator(f"{APP_NAME} {APP_VERSION}")
            writer.setResolution(300)

            page_size = QPageSize(
                QSizeF(page_w_mm, page_h_mm),
                QPageSize.Millimeter,
                "Excogitare Map",
                QPageSize.ExactMatch,
            )
            writer.setPageSize(page_size)
            writer.setPageMargins(
                QMarginsF(0.0, 0.0, 0.0, 0.0),
                QPageLayout.Millimeter,
            )

            painter = QPainter(writer)
            if not painter.isActive():
                raise RuntimeError("PDF-Zeichner konnte nicht gestartet werden.")

            target = QRectF(
                0.0,
                0.0,
                float(writer.width()),
                float(writer.height()),
            )
            self.scene.render(
                painter,
                target,
                source,
                Qt.KeepAspectRatio,
            )
            painter.end()

            self.statusBar().showMessage(
                f"PDF exportiert: {filename}",
                5000,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "PDF-Export",
                f"PDF konnte nicht exportiert werden:\\n\\n{exc}",
            )
        finally:
            self._restore_scene_export(export_state)

    def export_jpg(self) -> None:
        """Exportiert die komplette sichtbare Map als hochauflösendes JPG."""
        default_name = f"{self._export_file_stem()}.jpg"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Map als JPG exportieren",
            default_name,
            "JPEG-Bilder (*.jpg *.jpeg)",
        )
        if not filename:
            return
        if not filename.lower().endswith((".jpg", ".jpeg")):
            filename += ".jpg"

        export_state = self._prepare_scene_export()
        try:
            source = self._export_scene_bounds()
            if source.width() <= 0.0 or source.height() <= 0.0:
                raise RuntimeError("Die Map besitzt keinen exportierbaren Inhalt.")

            # Hohe Auflösung, aber mit Sicherheitsgrenze gegen riesige Images.
            scale = 2.0
            max_dimension = 16000.0
            longest = max(source.width(), source.height())
            if longest * scale > max_dimension:
                scale = max_dimension / longest

            pixel_w = max(1, int(round(source.width() * scale)))
            pixel_h = max(1, int(round(source.height() * scale)))

            # Zusätzlich eine vernünftige Gesamtpixelgrenze (~120 MP) setzen.
            max_pixels = 120_000_000
            pixel_count = pixel_w * pixel_h
            if pixel_count > max_pixels:
                reduction = math.sqrt(max_pixels / float(pixel_count))
                pixel_w = max(1, int(pixel_w * reduction))
                pixel_h = max(1, int(pixel_h * reduction))

            image = QImage(
                pixel_w,
                pixel_h,
                QImage.Format_RGB32,
            )
            image.fill(QColor("#ffffff"))

            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

            self.scene.render(
                painter,
                QRectF(0.0, 0.0, float(pixel_w), float(pixel_h)),
                source,
                Qt.KeepAspectRatio,
            )
            painter.end()

            if not image.save(filename, "JPG", 94):
                raise RuntimeError("Qt konnte die JPG-Datei nicht schreiben.")

            self.statusBar().showMessage(
                f"JPG exportiert: {filename} ({pixel_w} × {pixel_h} px)",
                5000,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "JPG-Export",
                f"JPG konnte nicht exportiert werden:\\n\\n{exc}",
            )
        finally:
            self._restore_scene_export(export_state)

    def _create_actions(self) -> None:
        self.new_action = QAction("Neu", self)
        self.new_action.setShortcut(QKeySequence.New)
        self.new_action.triggered.connect(self.new_project)

        self.open_action = QAction("Öffnen", self)
        self.open_action.setShortcut(QKeySequence.Open)
        self.open_action.triggered.connect(self.open_project)

        self.save_action = QAction("Speichern", self)
        self.save_action.setShortcut(QKeySequence.Save)
        self.save_action.triggered.connect(self.save_project)

        self.create_node_action = QAction("Neuer Knoten (Ins)", self)
        self.create_node_action.setShortcut("Insert")
        self.create_node_action.triggered.connect(self.create_node)

        self.create_child_action = QAction("Unterknoten (Enter/Tab)", self)
        # Enter/Tab werden in der Szene behandelt, damit sie beim
        # Bearbeiten eines Knotentextes nicht dazwischenfunken.
        self.create_child_action.setToolTip(
            "Unterknoten zum ausgewählten Knoten erzeugen (Enter oder Tab)"
        )
        self.create_child_action.triggered.connect(self.create_child)

        self.create_sibling_action = QAction("Geschwisterknoten (Strg+Enter)", self)
        # Strg+Enter wird in der Szene ausgewertet, damit der Texteditor
        # die Tastenkombination während der Texteingabe nicht abfängt.
        self.create_sibling_action.setToolTip(
            "Geschwisterknoten zum ausgewählten Knoten erzeugen (Strg+Enter)"
        )
        self.create_sibling_action.triggered.connect(self.create_sibling)

        self.save_as_action = QAction("Speichern unter", self)
        self.save_as_action.setShortcut(QKeySequence.SaveAs)
        self.save_as_action.triggered.connect(self.save_project_as)

        self.export_pdf_action = QAction("PDF …", self)
        self.export_pdf_action.setToolTip("Gesamte sichtbare Map als PDF exportieren")
        self.export_pdf_action.triggered.connect(self.export_pdf)

        self.export_jpg_action = QAction("JPG …", self)
        self.export_jpg_action.setToolTip("Gesamte sichtbare Map als JPG exportieren")
        self.export_jpg_action.triggered.connect(self.export_jpg)

        self.undo_action = QAction("Rückgängig", self)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self.undo)

        self.redo_action = QAction("Wiederholen", self)
        self.redo_action.setShortcut(QKeySequence.Redo)
        self.redo_action.triggered.connect(self.redo)

        self.bold_action = QAction("Fett", self)
        self.bold_action.setShortcut(QKeySequence.Bold)
        self.bold_action.setCheckable(True)
        self.bold_action.triggered.connect(lambda: self._apply_text_format("bold"))

        self.italic_action = QAction("Kursiv", self)
        self.italic_action.setShortcut(QKeySequence.Italic)
        self.italic_action.setCheckable(True)
        self.italic_action.triggered.connect(lambda: self._apply_text_format("italic"))

        self.underline_action = QAction("Unterstrichen", self)
        self.underline_action.setShortcut(QKeySequence.Underline)
        self.underline_action.setCheckable(True)
        self.underline_action.triggered.connect(lambda: self._apply_text_format("underline"))

        self.bullet_action = QAction("Aufzählung", self)
        self.bullet_action.setCheckable(True)
        self.bullet_action.triggered.connect(lambda: self._apply_text_format("bullet"))

        self.numbered_action = QAction("Nummerierte Liste", self)
        self.numbered_action.setCheckable(True)
        self.numbered_action.triggered.connect(lambda: self._apply_text_format("numbered"))

        self.text_color_action = QAction("Farbe …", self)
        self.text_color_action.triggered.connect(self._choose_context_color)

        self.format_painter_action = QAction("Format übertragen", self)
        self.format_painter_action.setToolTip("Format des ausgewählten Elements übernehmen")
        self.format_painter_action.triggered.connect(self._start_format_painter)

        self.link_action = QAction("Link einfügen …", self)
        self.link_action.setShortcut("Ctrl+K")
        self.link_action.triggered.connect(self._insert_link)

        self.copy_nodes_action = QAction("Kopieren", self)
        self.copy_nodes_action.setShortcut(QKeySequence.Copy)
        self.copy_nodes_action.triggered.connect(self._copy_to_clipboard)

        self.paste_rich_action = QAction("Einfügen", self)
        self.paste_rich_action.setShortcut(QKeySequence.Paste)
        self.paste_rich_action.triggered.connect(self._paste_from_clipboard)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&Datei")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addSeparator()

        export_menu = file_menu.addMenu("Exportieren")
        export_menu.addAction(self.export_pdf_action)
        export_menu.addAction(self.export_jpg_action)

        edit_menu = self.menuBar().addMenu("&Bearbeiten")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.copy_nodes_action)
        edit_menu.addAction(self.paste_rich_action)

        view_menu = self.menuBar().addMenu("&Ansicht")
        view_menu.addAction(self.properties_dock.toggleViewAction())
        view_menu.addAction(self.symbols_dock.toggleViewAction())
        view_menu.addAction(self.overview_dock.toggleViewAction())
        view_menu.addAction(self.details_dock.toggleViewAction())
        view_menu.addAction(self.drawing_dock.toggleViewAction())
        view_menu.addSeparator()
        self.reset_layout_action = QAction("Fenster zurücksetzen", self)
        self.reset_layout_action.triggered.connect(self._reset_window_layout)
        view_menu.addAction(self.reset_layout_action)

        format_menu = self.menuBar().addMenu("&Format")
        format_menu.addAction(self.bold_action)
        format_menu.addAction(self.italic_action)
        format_menu.addAction(self.underline_action)
        format_menu.addSeparator()
        format_menu.addAction(self.bullet_action)
        format_menu.addAction(self.numbered_action)
        format_menu.addSeparator()
        format_menu.addAction(self.text_color_action)
        format_menu.addAction(self.format_painter_action)
        format_menu.addAction(self.link_action)

        node_menu = self.menuBar().addMenu("&Knoten")
        node_menu.addAction(self.create_node_action)
        node_menu.addAction(self.create_child_action)
        node_menu.addAction(self.create_sibling_action)

        help_menu = self.menuBar().addMenu(self.tr("&Hilfe"))
        about_action = QAction(self.tr("Über Excogitare"), self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            self.tr("Über Excogitare"),
            self.tr(
                "<h2>Excogitare</h2>"
                "<p><b>Engineering Knowledge Workspace</b></p>"
                "<p>Version {version}</p>"
                "<p><i>Collect your knowledge.<br>"
                "Connect your ideas.<br>"
                "Master complexity.</i></p>"
                "<p>Developed by René Doß<br>"
                "Dossmatik GmbH</p>"
            ).format(version=APP_VERSION),
        )


    def _create_toolbar(self) -> None:
        self.format_toolbar = QToolBar("Text", self)
        self.format_toolbar.setObjectName("text_toolbar")
        self.format_toolbar.setMovable(False)
        self.format_toolbar.setFloatable(False)
        self.format_toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.format_toolbar.setContentsMargins(2, 1, 2, 1)
        self.format_toolbar.setIconSize(QSize(16, 16))
        self.format_toolbar.setStyleSheet(
            "QToolBar#text_toolbar { spacing: 2px; padding: 1px 4px; "
            "min-height: 27px; max-height: 29px; border: 0; "
            "border-bottom: 1px solid #d7dce2; background: #f7f8fa; }"
            "QToolBar#text_toolbar QToolButton { min-width: 22px; max-height: 23px; "
            "padding: 0px 4px; margin: 0; border: 1px solid transparent; "
            "border-radius: 3px; background: transparent; color: #20242a; }"
            "QToolBar#text_toolbar QToolButton:hover { background: #e8edf5; "
            "border-color: #c8d2df; }"
            "QToolBar#text_toolbar QToolButton:checked { background: #dce9ff; "
            "border-color: #7aa7e8; }"
            "QToolBar#text_toolbar QToolButton:disabled { color: #9aa1aa; }"
            "QToolBar#text_toolbar QSpinBox { min-height: 22px; max-height: 22px; "
            "padding: 0 15px 0 4px; border: 1px solid #c8ced7; "
            "border-radius: 2px; background: white; selection-background-color: #dce9ff; }"
            "QToolBar#text_toolbar QSpinBox::up-button { subcontrol-origin: border; "
            "subcontrol-position: top right; width: 13px; height: 10px; "
            "border-left: 1px solid #c8ced7; border-bottom: 1px solid #d9dde3; "
            "background: #f5f6f8; }"
            "QToolBar#text_toolbar QSpinBox::down-button { subcontrol-origin: border; "
            "subcontrol-position: bottom right; width: 13px; height: 10px; "
            "border-left: 1px solid #c8ced7; background: #f5f6f8; }"
            "QToolBar#text_toolbar QSpinBox::up-button:hover, "
            "QToolBar#text_toolbar QSpinBox::down-button:hover { background: #e8edf5; }"
            "QToolBar#text_toolbar QToolBarSeparator { width: 5px; margin: 3px 2px; "
            "background: #cfd4db; }"
        )

        # Kurze, sofort erkennbare Beschriftungen; vollständige Namen stehen
        # weiterhin im Menü Format und in den Tooltips.
        self.bold_action.setText("B")
        self.bold_action.setToolTip("Fett (Strg+B)")
        bold_font = QFont(self.font())
        bold_font.setBold(True)
        self.bold_action.setFont(bold_font)
        self.italic_action.setText("I")
        self.italic_action.setToolTip("Kursiv (Strg+I)")
        italic_font = QFont(self.font())
        italic_font.setItalic(True)
        self.italic_action.setFont(italic_font)
        self.underline_action.setText("U")
        self.underline_action.setToolTip("Unterstrichen (Strg+U)")
        underline_font = QFont(self.font())
        underline_font.setUnderline(True)
        self.underline_action.setFont(underline_font)
        self.bullet_action.setText("•")
        self.bullet_action.setToolTip("Aufzählung")
        self.numbered_action.setText("1.")
        self.numbered_action.setToolTip("Nummerierte Liste")
        self.text_color_action.setText("Farbe ▾")
        self.text_color_action.setToolTip("Farbe des ausgewählten Elements ändern")
        self.link_action.setText("🔗")
        self.link_action.setToolTip("Link einfügen (Strg+K)")
        self.paste_rich_action.setText("📋")
        self.paste_rich_action.setToolTip("Text, HTML oder Screenshot einfügen (Strg+V)")

        self.format_toolbar.addAction(self.bold_action)
        self.format_toolbar.addAction(self.italic_action)
        self.format_toolbar.addAction(self.underline_action)
        self.format_toolbar.addSeparator()
        self.format_toolbar.addAction(self.bullet_action)
        self.format_toolbar.addAction(self.numbered_action)
        self.format_toolbar.addSeparator()

        self.font_size_down = QPushButton("−")
        self.font_size_down.setFixedSize(24, 24)
        self.font_size_down.setToolTip("Schrift verkleinern")
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 72)
        self.font_size_spin.setValue(10)
        self.font_size_spin.setKeyboardTracking(False)
        self.font_size_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)     
        self.font_size_spin.setAlignment(Qt.AlignCenter)
        self.font_size_spin.setToolTip("Schriftgröße")
        self.font_size_spin.setFixedWidth(54)
        self.font_size_spin.setMinimumWidth(54)
        self.font_size_spin.valueChanged.connect(self._set_font_size)
        self.font_size_up = QPushButton("+")
        self.font_size_up.setFixedSize(24, 24)
        self.font_size_up.setToolTip("Schrift vergrößern")

        self.font_size_down.clicked.connect(
            lambda: self.font_size_spin.setValue(self.font_size_spin.value() - 1)
        )
        self.font_size_up.clicked.connect(
            lambda: self.font_size_spin.setValue(self.font_size_spin.value() + 1)
        )

        self.format_toolbar.addWidget(self.font_size_down)
        self.format_toolbar.addWidget(self.font_size_spin)
        self.format_toolbar.addWidget(self.font_size_up)

        
        self.format_toolbar.addAction(self.text_color_action)

        self.format_painter_button = QToolButton()
        self.format_painter_button.setIcon(_make_format_painter_icon())
        self.format_painter_button.setIconSize(QSize(18, 18))
        self.format_painter_button.setCheckable(True)
        self.format_painter_button.setAutoRaise(True)
        self.format_painter_button.setFixedSize(28, 24)
        self.format_painter_button.setToolTip("Format übertragen")
        self.format_painter_button.clicked.connect(self._toggle_format_painter)
        self.format_toolbar.addWidget(self.format_painter_button)
        self.format_toolbar.addAction(self.link_action)
        self.format_toolbar.addSeparator()
        self.format_toolbar.addAction(self.paste_rich_action)



        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.format_toolbar.addWidget(spacer)
        self.addToolBar(Qt.TopToolBarArea, self.format_toolbar)
        self._update_format_controls()

    def resizeEvent(self, event) -> None:
        """Synchronisiert den zentralen Map-Viewport mit der Fenstergröße."""
        super().resizeEvent(event)
        self._sync_central_view_geometry()

    def showEvent(self, event) -> None:
        """Korrigiert auch Geometrie nach restoreGeometry/restoreState."""
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_central_view_geometry)

    def _sync_central_view_geometry(self) -> None:
        # Nur Widget-Geometrie aktualisieren. Zoom, Scrollposition,
        # Szene und Knotenkoordinaten bleiben unverändert.
        central = self.centralWidget()
        if central is None:
            return
        central.updateGeometry()
        layout = central.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        if hasattr(self, "view"):
            self.view.updateGeometry()
            self.view.viewport().update()

    def _restore_window_layout(self) -> None:
        """Stellt Dockpositionen und Fenstergeometrie der letzten Sitzung wieder her."""
        geometry = self.settings.value("main_window/geometry")
        state = self.settings.value("main_window/state_v1_3")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if state is not None:
            self.restoreState(state)

    def _reset_window_layout(self) -> None:
        """Stellt die übersichtliche V1.3.2-Standardanordnung wieder her."""
        for dock in (self.properties_dock, self.symbols_dock, self.drawing_dock, self.overview_dock):
            self.removeDockWidget(dock)
            self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.properties_dock.show()
        self.symbols_dock.show()
        self.drawing_dock.show()
        self.overview_dock.hide()
        self.resizeDocks(
            [self.symbols_dock, self.drawing_dock, self.properties_dock],
            [420, 190, 260],
            Qt.Vertical,
        )

    def _active_text_editor(self):
        # Notes use the same global formatting toolbar as node/drawing text.
        if hasattr(self, "note_edit") and self.note_edit.hasFocus():
            return self.note_edit

        editor = self.scene.editing_item
        if editor is None:
            return None
        try:
            if not shiboken6.isValid(editor):
                self.scene.editing_item = None
                return None
        except (RuntimeError, ReferenceError):
            self.scene.editing_item = None
            return None
        return editor

    def _apply_text_format(self, command: str) -> None:
        editor = self._active_text_editor()
        if editor is None:
            return
        if command == "bold": editor.toggle_bold()
        elif command == "italic": editor.toggle_italic()
        elif command == "underline": editor.toggle_underline()
        elif command == "bullet": editor.toggle_bullet_list()
        elif command == "numbered": editor.toggle_numbered_list()
        editor.setFocus(Qt.OtherFocusReason)
        self._update_format_controls()

    def _copy_to_clipboard(self) -> None:
        editor = self._active_text_editor()
        if editor is not None:
            editor.copy()
            return
        self.scene.copy_selected_to_clipboard()

    def _paste_from_clipboard(self) -> None:
        editor = self._active_text_editor()
        if editor is not None and hasattr(editor, "paste_from_clipboard"):
            if editor.paste_from_clipboard():
                editor.setFocus(Qt.OtherFocusReason)
                self._update_format_controls()
                return

        if self.scene.paste_from_clipboard():
            editor = self._active_text_editor()
            if editor is not None:
                editor.setFocus(Qt.OtherFocusReason)
            self._update_format_controls()

    def _set_font_size(self, value: int) -> None:
        editor = self._active_text_editor()
        if editor is None:
            return
        editor.set_font_point_size(value)
        editor.setFocus(Qt.OtherFocusReason)

    def _selected_format_source(self):
        """Ermittelt exakt ein sichtbares Quellobjekt für den Formatpinsel."""
        selected = self.scene.selectedItems()
        logical = []
        seen = set()

        for item in selected:
            current = item
            while current is not None:
                object_id = getattr(current, "object_id", None)
                drawing_id = getattr(current, "drawing_id", None)
                relation_id = getattr(current, "relation_id", None)
                key = None
                if object_id is not None:
                    key = ("node", object_id)
                elif drawing_id is not None:
                    key = ("drawing", drawing_id)
                elif relation_id is not None:
                    key = ("relation", relation_id)
                if key is not None:
                    if key not in seen:
                        logical.append((key, current))
                        seen.add(key)
                    break
                current = current.parentItem()

        if len(logical) != 1:
            return None
        return logical[0]

    def _capture_format_payload(self):
        """Nimmt das Format des exakt einen ausgewählten Quellobjekts auf.

        Inhalt und Geometrie werden bewusst NICHT übernommen:
        kein Text, keine Bilder, keine Größe und keine Position.
        """
        source = self._selected_format_source()
        if source is None:
            return None

        (kind, source_id), item = source

        if kind == "node":
            state = self.model.active_map["object_states"].get(source_id, {})
            keys = (
                "shape",
                "fill_color",
                "border_color",
                "text_color",
                "border_width",
                "corner_radius",
            )

            # Zusätzlich das sichtbare Zeichenformat des Knotentextes erfassen.
            text_format = {}
            label = getattr(item, "label", None)
            if label is not None:
                try:
                    document = label.document()
                    cursor = QTextCursor(document)
                    if document.characterCount() > 1:
                        cursor.setPosition(0)
                        cursor.movePosition(
                            QTextCursor.NextCharacter,
                            QTextCursor.KeepAnchor,
                        )
                    fmt = cursor.charFormat()
                    text_format = {
                        "font_family": fmt.fontFamily(),
                        "font_size": float(fmt.fontPointSize()),
                        "font_weight": int(fmt.fontWeight()),
                        "italic": bool(fmt.fontItalic()),
                        "underline": bool(fmt.fontUnderline()),
                        "text_color": fmt.foreground().color().name(),
                    }
                except (RuntimeError, ReferenceError):
                    text_format = {}

            return {
                "kind": "node",
                "format": {
                    key: state.get(key)
                    for key in keys
                    if key in state
                },
                "text_format": text_format,
            }

        if kind == "drawing":
            data = self.scene.drawing_controller.data(source_id)
            dtype = str(data.get("type", ""))

            if dtype == "rectangle":
                keys = (
                    "fill_color",
                    "border_color",
                    "border_width",
                    "opacity",
                )
                return {
                    "kind": "rectangle",
                    "format": {
                        key: data.get(key)
                        for key in keys
                        if key in data
                    },
                }

            if dtype in {"line", "arrow"}:
                keys = ("color", "width")
                return {
                    "kind": "segment",
                    "format": {
                        key: data.get(key)
                        for key in keys
                        if key in data
                    },
                }

            if dtype == "text":
                try:
                    cursor = item.textCursor()
                    cursor.setPosition(0)
                    cursor.movePosition(
                        QTextCursor.NextCharacter,
                        QTextCursor.KeepAnchor,
                    )
                    fmt = cursor.charFormat()
                    return {
                        "kind": "text",
                        "format": {
                            "font_family": fmt.fontFamily(),
                            "font_size": float(fmt.fontPointSize()),
                            "font_weight": int(fmt.fontWeight()),
                            "italic": bool(fmt.fontItalic()),
                            "underline": bool(fmt.fontUnderline()),
                            "color": fmt.foreground().color().name(),
                        },
                    }
                except (RuntimeError, ReferenceError):
                    return {
                        "kind": "text",
                        "format": {
                            "color": data.get("color", "#333333")
                        },
                    }

        return None

    def _start_format_painter(self) -> None:
        payload = self._capture_format_payload()
        if payload is None:
            self.statusBar().showMessage(
                "Format übertragen: zuerst genau ein Objekt auswählen.",
                3500,
            )
            if hasattr(self, "format_painter_button"):
                self.format_painter_button.blockSignals(True)
                self.format_painter_button.setChecked(False)
                self.format_painter_button.blockSignals(False)
            return

        self._format_painter_payload = payload
        if hasattr(self, "format_painter_button"):
            self.format_painter_button.blockSignals(True)
            self.format_painter_button.setChecked(True)
            self.format_painter_button.blockSignals(False)

        # Anders als bisher bleibt der Pinsel aktiv. So können mehrere Knoten
        # nacheinander formatiert werden. Esc oder erneuter Klick beendet ihn.
        self.statusBar().showMessage(
            "Formatpinsel aktiv – Ziel(e) anklicken. Esc oder Pinsel beendet.",
            0,
        )

    def _toggle_format_painter(self, checked: bool) -> None:
        if checked:
            self._start_format_painter()
        else:
            self._cancel_format_painter()

    def _cancel_format_painter(self) -> None:
        self._format_painter_payload = None
        if hasattr(self, "format_painter_button"):
            self.format_painter_button.blockSignals(True)
            self.format_painter_button.setChecked(False)
            self.format_painter_button.blockSignals(False)
        self.statusBar().clearMessage()

    @staticmethod
    def _logical_item(item):
        current = item
        while current is not None:
            if getattr(current, "object_id", None) is not None:
                return ("node", current.object_id, current)
            drawing_id = getattr(current, "drawing_id", None)
            if drawing_id is not None:
                return ("drawing", drawing_id, current)
            relation_id = getattr(current, "relation_id", None)
            if relation_id is not None:
                return ("relation", relation_id, current)
            current = current.parentItem()
        return None

    @staticmethod
    def _merge_character_format(text_item, values: dict) -> bool:
        """Formatiert den kompletten vorhandenen Text, ohne Inhalt zu ersetzen."""
        try:
            document = text_item.document()
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.Document)

            fmt = QTextCharFormat()
            family = str(values.get("font_family", "") or "")
            if family:
                fmt.setFontFamily(family)

            size = float(values.get("font_size", 0.0) or 0.0)
            if size > 0.0:
                fmt.setFontPointSize(size)

            if "font_weight" in values:
                fmt.setFontWeight(int(values["font_weight"]))
            if "italic" in values:
                fmt.setFontItalic(bool(values["italic"]))
            if "underline" in values:
                fmt.setFontUnderline(bool(values["underline"]))

            color = values.get("text_color", values.get("color"))
            if color:
                fmt.setForeground(QColor(str(color)))

            cursor.mergeCharFormat(fmt)
            return True
        except (RuntimeError, ReferenceError, TypeError, ValueError):
            return False

    def _apply_format_painter_to_item(self, clicked_item) -> bool:
        """Wendet ausschließlich Format an; Inhalt und Position bleiben erhalten."""
        payload = self._format_painter_payload
        if payload is None:
            return False

        logical = self._logical_item(clicked_item)
        if logical is None:
            # Klick ins Leere soll den aktiven Pinsel nicht versehentlich beenden.
            return False

        kind, target_id, item = logical
        applied = False

        # Erst prüfen, ob Quelle und Ziel kompatibel sind. Dadurch entsteht bei
        # einem Fehlklick kein nutzloser Undo-Eintrag.
        compatible = (
            (payload["kind"] == "node" and kind == "node")
            or (
                kind == "drawing"
                and (
                    (
                        payload["kind"] == "rectangle"
                        and str(
                            self.scene.drawing_controller.data(target_id).get(
                                "type", ""
                            )
                        ) == "rectangle"
                    )
                    or (
                        payload["kind"] == "segment"
                        and str(
                            self.scene.drawing_controller.data(target_id).get(
                                "type", ""
                            )
                        ) in {"line", "arrow"}
                    )
                    or (
                        payload["kind"] == "text"
                        and str(
                            self.scene.drawing_controller.data(target_id).get(
                                "type", ""
                            )
                        ) == "text"
                    )
                )
            )
        )

        if not compatible:
            self.statusBar().showMessage(
                "Formatpinsel aktiv – dieses Ziel passt nicht zur Quelle.",
                2500,
            )
            return True

        self.scene.push_undo()

        if payload["kind"] == "node" and kind == "node":
            state = self.model.active_map["object_states"].get(target_id)
            target_node = self.scene.node_items.get(target_id)

            if state is not None and target_node is not None:
                # Nur Darstellung übernehmen. Größe, Position, Layout,
                # Ein-/Ausklappzustand und Inhalt bleiben beim Ziel.
                state.update(payload["format"])

                text_values = payload.get("text_format", {})
                if text_values:
                    self._merge_character_format(
                        target_node.label,
                        text_values,
                    )
                    self.model.set_rich_text(
                        target_id,
                        target_node.label.toHtml(),
                        target_node.label.toPlainText(),
                    )

                self.model.touch()
                applied = True

        elif kind == "drawing":
            data = self.scene.drawing_controller.data(target_id)
            dtype = str(data.get("type", ""))

            if payload["kind"] == "rectangle" and dtype == "rectangle":
                data.update(payload["format"])
                self.model.touch()
                applied = True

            elif payload["kind"] == "segment" and dtype in {"line", "arrow"}:
                data.update(payload["format"])
                self.model.touch()
                applied = True

            elif payload["kind"] == "text" and dtype == "text":
                if self._merge_character_format(item, payload["format"]):
                    data["html"] = item.toHtml()
                    data["text"] = item.toPlainText()
                    self.model.touch()
                    applied = True

        if applied:
            # Pinsel bleibt aktiv. Nach rebuild bleibt der aufgenommene Payload
            # unabhängig von der aktuellen Auswahl erhalten.
            self.scene.rebuild()
            self.statusBar().showMessage(
                "Format übertragen – Pinsel bleibt aktiv. Esc beendet.",
                2200,
            )
            return True

        # Sicherheitsnetz: falls trotz Kompatibilitätsprüfung nichts geändert
        # werden konnte, den gerade angelegten Undo-Eintrag entfernen.
        if self.scene.undo_stack:
            self.scene.undo_stack.pop()
        return True

    def _context_color_target(self):
        """Ermittelt das Element, auf das der globale Farbknopf wirkt."""
        editor = self._active_text_editor()
        if editor is not None:
            current = editor.textCursor().charFormat().foreground().color()
            return ("text", editor, current if current.isValid() else QColor("#202020"))

        drawing_ids = self.scene.drawing_controller.selected_drawing_ids()
        if drawing_ids:
            drawing_id = drawing_ids[0]
            data = self.scene.drawing_controller.data(drawing_id)
            dtype = str(data.get("type", ""))
            if dtype == "rectangle":
                current = QColor(str(data.get("fill_color", "#fff2a8")))
            else:
                current = QColor(str(data.get("color", "#3b3b3b")))
            return ("drawing", drawing_id, current)

        for item in self.scene.selectedItems():
            object_id = getattr(item, "object_id", None)
            if object_id in self.model.active_map.get("object_states", {}):
                state = self.model.active_map["object_states"][object_id]
                return ("node", object_id, QColor(str(state.get("fill_color", "#ffffff"))))

        return None

    def _set_color_action_preview(self, color: QColor | None) -> None:
        if color is None or not color.isValid():
            self.text_color_action.setIcon(QIcon())
            return
        pixmap = QPixmap(16, 16)
        pixmap.fill(color)
        painter = QPainter(pixmap)
        painter.setPen(QPen(QColor("#6b7280"), 1))
        painter.drawRect(0, 0, 15, 15)
        painter.end()
        self.text_color_action.setIcon(QIcon(pixmap))

    def _choose_context_color(self) -> None:
        target = self._context_color_target()
        if target is None:
            return

        kind, target_id, current = target
        color = QColorDialog.getColor(current, self, "Farbe auswählen")
        if not color.isValid():
            return

        if kind == "text":
            editor = target_id
            editor.set_text_color(color)
            editor.setFocus(Qt.OtherFocusReason)
        elif kind == "drawing":
            drawing_id = target_id
            data = self.scene.drawing_controller.data(drawing_id)
            if data.get("type") == "rectangle":
                self.scene.drawing_controller.apply_fill_color(color.name())
            else:
                self.scene.drawing_controller.apply_line_color(color.name())
        elif kind == "node":
            object_id = target_id
            self.scene.push_undo()
            self.model.set_node_color(object_id, "fill_color", color.name())
            self._rebuild_and_reselect(object_id)

        self._set_color_action_preview(color)
        self._update_format_controls()

    # Kompatibilität für ältere interne Aufrufe.
    def _choose_text_color(self) -> None:
        self._choose_context_color()

    def _insert_link(self) -> None:
        editor = self._active_text_editor()
        if editor is None:
            return
        url, ok = QInputDialog.getText(self, "Link einfügen", "Adresse:")
        if ok and url.strip():
            editor.insert_link(url)
            editor.setFocus(Qt.OtherFocusReason)
            self._update_format_controls()

    def _selected_text_format(self):
        """Liest das sichtbare Textformat auch dann, wenn gerade nicht editiert wird.

        Das behebt den irreführenden Fall, dass die Schriftgrößenanzeige einfach
        den Wert des zuletzt bearbeiteten Textes stehen lässt.
        """
        source = self._selected_format_source()
        if source is None:
            return None

        (kind, source_id), item = source
        text_item = None

        if kind == "node":
            text_item = getattr(item, "label", None)
        elif kind == "drawing":
            data = self.scene.drawing_controller.data(source_id)
            if str(data.get("type", "")) == "text":
                text_item = item

        if text_item is None:
            return None

        try:
            document = text_item.document()
            if document is None or document.characterCount() <= 1:
                return text_item.textCursor().charFormat()

            cursor = QTextCursor(document)
            cursor.setPosition(0)
            cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
            return cursor.charFormat()
        except (RuntimeError, ReferenceError):
            return None

    def _update_format_controls(self) -> None:
        editor = self._active_text_editor()
        enabled = editor is not None
        text_actions = (self.bold_action, self.italic_action, self.underline_action,
                        self.bullet_action, self.numbered_action, self.link_action)
        for action in text_actions:
            action.setEnabled(enabled)

        color_target = self._context_color_target()
        self.text_color_action.setEnabled(color_target is not None)
        self._set_color_action_preview(color_target[2] if color_target is not None else None)

        self.paste_rich_action.setEnabled(True)
        if hasattr(self, "font_size_spin"):
            # Schriftgröße ist während der Texteingabe änderbar. Außerhalb der
            # Texteingabe zeigt das Feld trotzdem die echte Größe der Auswahl.
            self.font_size_spin.setEnabled(enabled)

        if editor is not None:
            try:
                cursor = editor.textCursor()
                char_fmt = cursor.charFormat()
            except (RuntimeError, ReferenceError):
                self.scene.editing_item = None
                self._update_format_controls()
                return
        else:
            char_fmt = self._selected_text_format()
            if char_fmt is None:
                for action in text_actions:
                    action.blockSignals(True)
                    action.setChecked(False)
                    action.blockSignals(False)
                return

            # Nicht im Editiermodus: nur anzeigen, nicht als Textbearbeitung
            # behandeln. Fett/Kursiv/Unterstrichen dürfen den sichtbaren
            # Zustand aber korrekt widerspiegeln.
            cursor = None
        states = {
            self.bold_action: int(char_fmt.fontWeight()) >= 700,
            self.italic_action: char_fmt.fontItalic(),
            self.underline_action: char_fmt.fontUnderline(),
        }
        current_list = cursor.currentList() if cursor is not None else None
        list_style = current_list.format().style() if current_list is not None else None
        from PySide6.QtGui import QTextListFormat
        states[self.bullet_action] = list_style == QTextListFormat.ListDisc
        states[self.numbered_action] = list_style == QTextListFormat.ListDecimal
        for action, checked in states.items():
            action.blockSignals(True); action.setChecked(bool(checked)); action.blockSignals(False)
        size = char_fmt.fontPointSize()
        if size > 0 and hasattr(self, "font_size_spin"):
            self.font_size_spin.blockSignals(True)
            self.font_size_spin.setValue(round(size))
            self.font_size_spin.blockSignals(False)

    def _update_status(self) -> None:
        map_name = self.model.active_map.get("name", "Map")
        self.statusBar().showMessage(
            f"{map_name} | Objekte: {len(self.model.data['objects'])} | "
            f"Beziehungen: {len(self.model.data['relations'])} | "
            f"Format: {self.model.data['format']['version']}"
        )


    def undo(self) -> None:
        if self.scene.undo():
            self._update_status()

    def redo(self) -> None:
        if self.scene.redo():
            self._update_status()

    def _create_initial_root_node(self) -> None:
        """Erzeugt in einem leeren Projekt sofort den ersten editierbaren Knoten."""
        if self.model.active_map.get("object_states"):
            return

        center = self.view.mapToScene(self.view.viewport().rect().center())
        self.scene.create_node_at(center)

        # Der automatisch erzeugte Startknoten gehört zum leeren Dokument
        # und soll nicht als erste Benutzeraktion in der Undo-Historie stehen.
        self.scene.clear_history()
        self._update_status()

    def create_node(self) -> None:
        center = self.view.mapToScene(self.view.viewport().rect().center())
        self.scene.create_node_at(center)
        self._update_status()

    def create_child(self) -> None:
        child_id = self.scene.create_child_for_selected()
        if child_id is None:
            QMessageBox.information(
                self,
                "Unterknoten",
                "Bitte zuerst einen vorhandenen Knoten auswählen.",
            )
            return
        self._update_status()

    def create_sibling(self) -> None:
        sibling_id = self.scene.create_sibling_for_selected()
        if sibling_id is None:
            QMessageBox.information(
                self,
                "Geschwisterknoten",
                "Bitte einen Knoten unterhalb der Wurzel auswählen.",
            )
            return
        self._update_status()

    def new_project(self) -> None:
        self.model = ProjectModel.create()
        self.current_path = None
        self.scene.model = self.model
        self.scene.clear_history()
        self.scene.active_node_id = None
        self.scene.rebuild()
        self._refresh_map_tabs()
        self._update_properties()
        self._update_status()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")

        # Auch "Neu" startet unmittelbar mit einem beschreibbaren Wurzelknoten.
        QTimer.singleShot(0, self._create_initial_root_node)

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Projekt öffnen",
            "",
            "MindMapper-Projekt (*.mmproj *.json);;Alle Dateien (*)",
        )
        if not path:
            return

        try:
            data = load_project(path)
        except UnsupportedFormatError as exc:
            QMessageBox.critical(self, "Nicht unterstütztes Dateiformat", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Fehler beim Öffnen", str(exc))
            return

     #   self.model = ProjectModel(data)
     #   self.current_path = Path(path)
     #   self.scene.model = self.model
    #  self.scene.clear_history()
    #    self.scene.active_node_id = None
    #    self.scene.rebuild()
    #    self._refresh_map_tabs()
    #    self._update_properties()
    #    self._update_status()
    #    self.setWindowTitle(f"{APP_NAME} {APP_VERSION} – {self.current_path.name}")
        new_model = ProjectModel(data)
        new_path = Path(path)

        document = DocumentState(
            model=new_model,
            path=new_path,
        )

        self.documents.append(document)
        self.active_document_index = len(self.documents) - 1

        self.model = new_model
        self.current_path = new_path

        self.scene.model = self.model
        self.scene.clear_history()
        self.scene.active_node_id = None
        self.scene.rebuild()

        self._refresh_map_tabs()
        self._update_properties()
        self._update_status()

        self.setWindowTitle(
            f"{APP_NAME} {APP_VERSION} – {self.current_path.name}"
        )

    def save_project(self) -> None:
        if self.current_path is None:
            self.save_project_as()
            return

        try:
            save_project(self.current_path, self.model.data)
        except Exception as exc:
            QMessageBox.critical(self, "Fehler beim Speichern", str(exc))
            return

        self._update_status()
        self.statusBar().showMessage(f"Gespeichert: {self.current_path}", 4000)

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Projekt speichern",
            "projekt.mmproj",
            "MindMapper-Projekt (*.mmproj)",
        )
        if not path:
            return

        if not path.lower().endswith(".mmproj"):
            path += ".mmproj"

        self.current_path = Path(path)
        self.documents[self.active_document_index].path = self.current_path

        self.save_project()
        self._refresh_map_tabs()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} – {self.current_path.name}")

    def closeEvent(self, event) -> None:
        self._closing = True

        self.settings.setValue("main_window/geometry", self.saveGeometry())
        self.settings.setValue("main_window/state_v1_3", self.saveState())
        self.settings.sync()
        event.accept()
