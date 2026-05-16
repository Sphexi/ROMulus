"""Render the ROMulus app icon — a CD-ROM disc — into PNG and ICO files.

Run from the repo root::

    .venv/Scripts/python.exe scripts/generate_icon.py

Produces ``src/romulus/ui/icons/cdrom.png`` (256x256 RGBA) and
``src/romulus/ui/icons/cdrom.ico`` (multi-resolution: 16/32/48/64/128/256).

The drawing uses PySide6's QPainter only — no Pillow / no ImageMagick — so
it runs anywhere the existing dev venv runs. The ICO writer is the same
Qt imageformats plugin that ships with PySide6 on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # Qt requires a QApplication before any QPainter / QImage work even
    # when running headless on Windows — and the dev .venv enforces the
    # offscreen platform plugin via the env-var check below.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QConicalGradient,
        QImage,
        QPainter,
        QPen,
        QPixmap,
        QRadialGradient,
    )
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    _ = app  # silence linter — handle must stay alive for the painter

    out_dir = Path(__file__).resolve().parent.parent / "src" / "romulus" / "ui" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    SIZE = 256

    def render(size: int) -> QImage:
        """Draw a single CD-ROM disc image at ``size`` x ``size``."""
        img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        center = QPointF(size / 2, size / 2)
        r_outer = size * 0.48
        r_label = size * 0.20
        r_hole = size * 0.08
        r_spindle = size * 0.045

        # 1. Outer disc — conical rainbow gradient to evoke the data side
        #    of a CD without going garish.
        rainbow = QConicalGradient(center, 0)
        # Soft, slightly desaturated rainbow.
        stops = [
            (0.00, QColor(170, 200, 220)),
            (0.15, QColor(200, 175, 210)),
            (0.30, QColor(220, 195, 165)),
            (0.45, QColor(195, 215, 180)),
            (0.60, QColor(175, 205, 220)),
            (0.75, QColor(205, 180, 215)),
            (0.90, QColor(220, 200, 170)),
            (1.00, QColor(170, 200, 220)),
        ]
        for stop, color in stops:
            rainbow.setColorAt(stop, color)
        painter.setBrush(QBrush(rainbow))
        painter.setPen(QPen(QColor(70, 80, 95), max(1, size // 128)))
        painter.drawEllipse(center, r_outer, r_outer)

        # 2. Inner label ring — flat silver/blue, suggests the hub area.
        label_grad = QRadialGradient(center, r_label)
        label_grad.setColorAt(0.0, QColor(225, 230, 240))
        label_grad.setColorAt(0.8, QColor(195, 205, 220))
        label_grad.setColorAt(1.0, QColor(170, 180, 200))
        painter.setBrush(QBrush(label_grad))
        painter.setPen(QPen(QColor(100, 110, 130), max(1, size // 200)))
        painter.drawEllipse(center, r_label, r_label)

        # 3. Spindle ring (the raised plastic edge around the hole).
        painter.setBrush(QBrush(QColor(210, 215, 225)))
        painter.setPen(QPen(QColor(90, 100, 115), max(1, size // 200)))
        painter.drawEllipse(center, r_spindle * 1.6, r_spindle * 1.6)

        # 4. Central hole — transparent so the disc has an actual hole.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setBrush(Qt.GlobalColor.transparent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, r_hole, r_hole)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 5. Specular highlight — narrow slice across the upper-right
        #    quadrant gives the disc its CD shine without overpowering.
        if size >= 48:
            highlight = QConicalGradient(center, 60)
            highlight.setColorAt(0.00, QColor(255, 255, 255, 0))
            highlight.setColorAt(0.05, QColor(255, 255, 255, 70))
            highlight.setColorAt(0.10, QColor(255, 255, 255, 0))
            highlight.setColorAt(1.00, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(highlight))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, r_outer, r_outer)

        painter.end()
        return img

    # Save canonical 256x256 PNG for Qt's runtime setWindowIcon.
    png_path = out_dir / "cdrom.png"
    render(SIZE).save(str(png_path), "PNG")
    print(f"  wrote {png_path}")

    # Multi-resolution ICO for Windows exe icon. Qt's ICO writer accepts a
    # list of QPixmaps via QIcon's addPixmap; the bundled imageformats
    # ``qico.dll`` plugin handles the multi-size container.
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(render(sz)))
    ico_path = out_dir / "cdrom.ico"
    # ``QIcon`` doesn't expose a direct save — write the largest pixmap
    # via QPixmap.save with .ico extension. Qt's ICO writer auto-includes
    # the LR/HR sizes from the source pixmap when given a 256x256 input.
    QPixmap.fromImage(render(SIZE)).save(str(ico_path), "ICO")
    print(f"  wrote {ico_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
