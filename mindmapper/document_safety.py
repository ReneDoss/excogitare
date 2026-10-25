"""Document state, safe saving and crash recovery, separate from window layout."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .model import ProjectModel, iso_now
from .project_validation import validate_project
from .storage import atomic_write, save_project


def content_digest(data: dict) -> str:
    """Ignore bookkeeping timestamps so Undo can return to a clean document."""
    data = deepcopy(data)
    data.pop('generator', None)
    data.get('project', {}).pop('modified', None)
    for obj in data.get('objects', {}).values():
        obj.pop('modified', None)
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def disk_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


@dataclass
class DocumentState:
    model: ProjectModel
    path: Path | None = None
    undo_stack: list = field(default_factory=list)
    redo_stack: list = field(default_factory=list)
    active_node_id: str | None = None
    recovery_id: str = field(default_factory=lambda: uuid4().hex)
    pristine: bool = True
    recovered: bool = False
    disk_digest: str | None = None
    saved_digest: str = field(init=False)

    def __post_init__(self):
        self.mark_saved()

    def mark_saved(self):
        self.saved_digest = content_digest(self.model.data)

    @property
    def name(self):
        if self.path is not None:
            return self.path.stem
        return 'Wiederhergestellt' if self.recovered else 'Unbenannt'


class RecoveryStore:
    """Each running process owns a lock and writes its own recovery files."""
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(QStandardPaths.writableLocation(
            QStandardPaths.AppLocalDataLocation)) / 'excogitare-recovery'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = uuid4().hex
        self.lock = QLockFile(str(self.directory / (self.session + '.lock')))
        self.lock.setStaleLockTime(0)
        if not self.lock.tryLock(0):
            raise OSError('Wiederherstellungsordner kann nicht gesperrt werden.')
        self.last_digest: dict[str, str] = {}

    def path(self, document):
        return self.directory / f'{self.session}-{document.recovery_id}.recovery.json'

    def write(self, document, data):
        digest = content_digest(data)
        if self.last_digest.get(document.recovery_id) == digest:
            return
        validate_project(data)
        payload = {'project_data': data, 'source_path': str(document.path) if document.path else None,
                   'saved_at': iso_now()}
        atomic_write(self.path(document), json.dumps(payload, ensure_ascii=False,
                     allow_nan=False).encode('utf-8'))
        self.last_digest[document.recovery_id] = digest

    def remove(self, document):
        self.path(document).unlink(missing_ok=True)
        self.last_digest.pop(document.recovery_id, None)

    def available(self):
        for path in sorted(self.directory.glob('*.recovery.json')):
            session = path.name.split('-', 1)[0]
            if session == self.session:
                continue
            lock = QLockFile(str(self.directory / (session + '.lock')))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                continue  # Never offer another live process's snapshots.
            try:
                yield path
            finally:
                lock.unlock()


class DocumentSafety:
    def __init__(self, window, recovery_directory=None):
        self.window = window
        self.recovery = None
        self.recovery_error = None
        try:
            self.recovery = RecoveryStore(recovery_directory)
        except OSError as exc:
            self.recovery_error = str(exc)
        self.indicator_timer = QTimer(window)
        self.indicator_timer.timeout.connect(self.refresh_indicators)
        self.indicator_timer.start(750)
        self.recovery_timer = QTimer(window)
        self.recovery_timer.timeout.connect(self.autosave)
        self.recovery_timer.start(60_000)

    def live_data(self, document):
        """Capture unfinished text without interrupting typing for autosave."""
        w = self.window
        if document.model is not w.model or w.scene.editing_item is None:
            return document.model.data
        editor = w.scene.editing_item
        data = deepcopy(document.model.data)
        if hasattr(editor, 'drawing_id'):
            drawing = data['maps'][document.model.active_map_id]['drawings'].get(editor.drawing_id)
            if drawing is not None and editor.toHtml() != getattr(editor, '_original_html', ''):
                drawing['html'], drawing['text'] = editor.toHtml(), editor.toPlainText()
        elif hasattr(editor, 'owner') and hasattr(editor.owner, 'object_id'):
            obj = data['objects'].get(editor.owner.object_id)
            if obj is not None and editor.toHtml() != getattr(editor, '_original_html', ''):
                obj['rich_html'] = editor.toHtml()
                obj['title'] = editor.toPlainText().strip() or 'Unbenannter Knoten'
        return data

    def dirty(self, document):
        return document.recovered or content_digest(self.live_data(document)) != document.saved_digest

    def flush_editor(self):
        editor = self.window.scene.editing_item
        if editor is not None:
            editor.finish_edit()

    def stash_active(self):
        w = self.window
        self.flush_editor()
        document = w.documents[w.active_document_index]
        document.undo_stack = w.scene.undo_stack
        document.redo_stack = w.scene.redo_stack
        document.active_node_id = w.scene.active_node_id

    def refresh_indicators(self):
        w = self.window
        if w._closing:
            return
        for index in range(w.map_tabs.count()):
            document_index = w.map_tabs.tabData(index)
            if document_index is None or not 0 <= document_index < len(w.documents):
                continue
            document = w.documents[document_index]
            w.map_tabs.setTabText(index, document.name + (' *' if self.dirty(document) else ''))
        document = w.documents[w.active_document_index]
        from . import APP_NAME, APP_VERSION
        w.setWindowTitle(f'{APP_NAME} {APP_VERSION} – {document.name}'
                         + (' *' if self.dirty(document) else ''))

    def save(self, document, save_as=False):
        self.flush_editor()
        path = document.path
        if path is None or save_as:
            name = str(path) if path else 'projekt.mmproj'
            filename, _ = QFileDialog.getSaveFileName(self.window, 'Projekt speichern', name,
                                                      'MindMapper-Projekt (*.mmproj)')
            if not filename:
                return False
            if not filename.lower().endswith('.mmproj'):
                filename += '.mmproj'
            path = Path(filename)
        if any(other is not document and other.path is not None
               and other.path.resolve() == path.resolve() for other in self.window.documents):
            QMessageBox.warning(self.window, 'Datei bereits geöffnet',
                                'Diese Datei ist bereits in einem anderen Tab geöffnet.\n'
                                'Bitte einen anderen Dateinamen wählen.')
            return False
        try:
            if (document.path is not None and path.resolve() == document.path.resolve()
                    and document.disk_digest is not None and
                    (not path.exists() or disk_digest(path) != document.disk_digest)):
                answer = QMessageBox.question(self.window, 'Datei wurde außerhalb geändert',
                    'Die Datei wurde seit dem Öffnen außerhalb von Excogitare geändert oder entfernt.\n'
                    'Ja: Datei überschreiben. Nein: unter anderem Namen speichern.',
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Cancel)
                if answer == QMessageBox.Cancel:
                    return False
                if answer == QMessageBox.No:
                    return self.save(document, save_as=True)
            save_project(path, document.model.data, create_backup=True)
        except Exception as exc:
            QMessageBox.critical(self.window, 'Fehler beim Speichern', str(exc))
            return False
        # Update document identity only AFTER the atomic write succeeded.
        document.path = path
        document.pristine = False
        document.recovered = False
        document.mark_saved()
        try:
            document.disk_digest = disk_digest(path)
        except OSError:
            document.disk_digest = None
        if self.window.model is document.model:
            self.window.current_path = path
        self.remove_recovery(document)
        self.refresh_indicators()
        self.window.statusBar().showMessage(f'Gespeichert: {path}', 4000)
        return True

    def can_close(self, document):
        if not self.dirty(document):
            return True
        answer = QMessageBox.question(self.window, 'Ungespeicherte Änderungen',
                    f'Änderungen an „{document.name}“ speichern?',
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save(document)
        return answer == QMessageBox.Discard

    def remove_recovery(self, document):
        if self.recovery is not None:
            try:
                self.recovery.remove(document)
            except OSError as exc:
                self.report_recovery_error(exc)

    def report_recovery_error(self, exc):
        self.window.statusBar().showMessage(f'Wiederherstellungssicherung fehlgeschlagen: {exc}', 15000)

    def autosave(self):
        if self.window._closing:
            return
        if self.recovery is None:
            self.report_recovery_error(self.recovery_error or 'Nicht verfügbar')
            return
        for document in self.window.documents:
            try:
                if self.dirty(document):
                    self.recovery.write(document, self.live_data(document))
                else:
                    self.recovery.remove(document)
            except Exception as exc:
                self.report_recovery_error(exc)

    def offer_recovery(self):
        if self.recovery is None:
            QMessageBox.warning(self.window, 'Automatische Sicherung nicht verfügbar',
                                self.recovery_error or 'Unbekannter Fehler')
            return
        for path in self.recovery.available():
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                data = payload['project_data']
                validate_project(data)
                source = payload.get('source_path') or 'Unbenannt'
            except Exception as exc:
                QMessageBox.warning(self.window, 'Beschädigte Wiederherstellungsdatei',
                                    f'{path.name}\n{exc}\nDie Datei bleibt erhalten.')
                continue
            answer = QMessageBox.question(self.window, 'Projekt wiederherstellen',
                    f'Eine automatische Sicherung wurde gefunden:\n{source}\n'
                    f'Stand: {payload.get("saved_at", "unbekannt")}\n\n'
                    'Ja: als neues Dokument wiederherstellen.\n'
                    'Nein: Sicherung verwerfen. Abbrechen: für später behalten.',
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
            if answer == QMessageBox.Yes:
                document = DocumentState(ProjectModel(data), pristine=False, recovered=True)
                # Persist the new recovery copy before removing the previous session's copy.
                try:
                    self.recovery.write(document, data)
                except Exception as exc:
                    self.report_recovery_error(exc)
                    continue
                self.window._append_document(document)
            if answer in (QMessageBox.Yes, QMessageBox.No):
                try:
                    path.unlink()
                except OSError as exc:
                    self.report_recovery_error(exc)
