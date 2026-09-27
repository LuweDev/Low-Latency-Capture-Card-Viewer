"""On-screen text drawn over the video: status, info panel and volume toasts.

Each box is rendered into its own small image (cached until its content
changes), so any renderer can draw them: QPainter-based ones blit the images,
the Direct3D one uploads them as textures and blends them over the video.
"""

import math
import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

_BACKGROUND = QColor(0, 0, 0, 170)
_TEXT = QColor(255, 255, 255)
_BAR_BG = QColor(80, 80, 80)
_MARGIN = 12
TOAST_FADE_SECONDS = 0.45


class OverlayLayer:
    """A rendered box: premultiplied ARGB image plus its place in the window."""

    def __init__(self, key, image, top_left):
        self.key = key              # changes whenever the image content changes
        self.image = image          # QImage, Format_ARGB32_Premultiplied, device pixels
        self.top_left = top_left    # QPoint in logical (widget) coordinates
        self.opacity = 1.0


class Overlay:
    def __init__(self):
        self.status = ""
        self.info_visible = False
        self.info_lines = []
        self._toast = ""
        self._toast_level = None
        self._toast_until = 0.0
        self._font = QFont("Segoe UI", 11)
        self._status_font = QFont("Segoe UI", 14)
        self._mono_font = QFont("Consolas", 10)
        self._cache = {}

    def show_toast(self, text, level=None, duration=1.2):
        """Brief message near the bottom; level (0-1) adds a bar. It stays for
        `duration` seconds, then fades out."""
        self._toast = text
        self._toast_level = level
        self._toast_until = time.monotonic() + duration

    def _toast_visible(self):
        return bool(self._toast) and time.monotonic() < self._toast_until + TOAST_FADE_SECONDS

    def toast_opacity(self):
        remaining = self._toast_until + TOAST_FADE_SECONDS - time.monotonic()
        return max(0.0, min(1.0, remaining / TOAST_FADE_SECONDS))

    def animating(self):
        """True while something needs regular repaints (a toast fading out)."""
        return self._toast_visible() and time.monotonic() >= self._toast_until - 0.05

    def active(self):
        return bool(self.status) or self.info_visible or self._toast_visible()

    def layers(self, rect: QRect, dpr: float):
        """The visible boxes for a widget area `rect` (logical coordinates)."""
        result = []
        if self.status:
            layer = self._box("status", self.status, self._status_font, Qt.AlignHCenter,
                              rect.width() - 4 * _MARGIN, dpr)
            layer.top_left = rect.center() - QPoint(round(layer.image.width() / dpr / 2),
                                                    round(layer.image.height() / dpr / 2))
            result.append(layer)
        if self.info_visible and self.info_lines:
            layer = self._box("info", "\n".join(self.info_lines), self._mono_font, Qt.AlignLeft,
                              rect.width() - 4 * _MARGIN, dpr)
            layer.top_left = rect.topLeft() + QPoint(_MARGIN, _MARGIN)
            result.append(layer)
        if self._toast_visible():
            layer = self._toast_box(rect, dpr)
            size_w = round(layer.image.width() / dpr)
            size_h = round(layer.image.height() / dpr)
            layer.top_left = QPoint(rect.center().x() - size_w // 2, rect.bottom() - 48 - size_h)
            layer.opacity = self.toast_opacity()
            result.append(layer)
        return result

    def paint(self, painter: QPainter, rect: QRect):
        """Draw the boxes with QPainter (OpenGL and software renderers)."""
        for layer in self.layers(rect, painter.device().devicePixelRatioF()):
            painter.setOpacity(layer.opacity)
            painter.drawImage(layer.top_left, layer.image)
        painter.setOpacity(1.0)

    # --- rendering -----------------------------------------------------------

    def _cached(self, key, make):
        if key not in self._cache:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[key] = make()
        return OverlayLayer(key, self._cache[key], QPoint())

    @staticmethod
    def _new_image(width, height, dpr):
        image = QImage(max(1, math.ceil(width * dpr)), max(1, math.ceil(height * dpr)),
                       QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.transparent)
        return image

    def _box(self, kind, text, font, align, max_width, dpr):
        key = (kind, text, max_width, dpr)

        def make():
            metrics = QFontMetrics(font)
            pad = 10
            text_rect = metrics.boundingRect(QRect(0, 0, max(1, max_width), 10000),
                                             Qt.TextWordWrap | Qt.AlignLeft, text)
            box = QRect(0, 0, text_rect.width() + 2 * pad, text_rect.height() + 2 * pad)
            image = self._new_image(box.width(), box.height(), dpr)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.TextAntialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(_BACKGROUND)
            painter.drawRoundedRect(QRectF(box), 6, 6)
            painter.setPen(_TEXT)
            painter.setFont(font)
            painter.drawText(box.adjusted(pad, pad, -pad, -pad), Qt.TextWordWrap | align, text)
            painter.end()
            return image

        return self._cached(key, make)

    def _toast_box(self, rect, dpr):
        level = None if self._toast_level is None else round(self._toast_level, 3)
        max_text_width = max(200, min(760, rect.width() - 80))
        key = ("toast", self._toast, level, max_text_width, dpr)

        def make():
            metrics = QFontMetrics(self._font)
            pad, bar_h = 12, 6
            text_size = metrics.boundingRect(QRect(0, 0, max_text_width, 10000),
                                             Qt.TextWordWrap | Qt.AlignHCenter, self._toast)
            width = max(220, text_size.width() + 2 * pad)
            height = text_size.height() + 2 * pad + (bar_h + 8 if level is not None else 0)
            image = self._new_image(width, height, dpr)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.TextAntialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(_BACKGROUND)
            painter.drawRoundedRect(QRectF(0, 0, width, height), 6, 6)
            painter.setPen(_TEXT)
            painter.setFont(self._font)
            text_rect = QRect(pad, pad, width - 2 * pad, text_size.height())
            painter.drawText(text_rect, Qt.TextWordWrap | Qt.AlignHCenter, self._toast)
            if level is not None:
                bar = QRect(pad, text_rect.bottom() + 8, width - 2 * pad, bar_h)
                painter.setBrush(_BAR_BG)
                painter.drawRoundedRect(QRectF(bar), 3, 3)
                filled = QRect(bar)
                filled.setWidth(round(bar.width() * max(0.0, min(1.0, level))))
                painter.setBrush(_TEXT)
                painter.drawRoundedRect(QRectF(filled), 3, 3)
            painter.end()
            return image

        return self._cached(key, make)
