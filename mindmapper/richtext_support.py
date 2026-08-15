from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWidgets import QApplication


def clipboard_image_html(max_width: int = 700) -> str | None:
    """Embed the current clipboard image as a self-contained PNG data URI."""
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
