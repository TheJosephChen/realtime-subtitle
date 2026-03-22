from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QGraphicsDropShadowEffect,
                             QHBoxLayout, QLabel, QFrame)
from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette

from ctypes import c_void_p
import time

# macOS: Make window visible on all desktops (Spaces)
try:
    from AppKit import NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary
    import objc
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False


class ResizeHandle(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setText("◢")
        self.setStyleSheet("color: rgba(255, 255, 255, 100); font-size: 16px;")
        self.setFixedSize(20, 20)
        self.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.startPos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.startPos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.startPos:
            delta = event.globalPosition().toPoint() - self.startPos
            new_width = max(self.parent_window.minimumWidth(), self.parent_window.width() + delta.x())
            new_height = max(self.parent_window.minimumHeight(), self.parent_window.height() + delta.y())
            self.parent_window.resize(new_width, new_height)
            self.startPos = event.globalPosition().toPoint()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.startPos = None


class OverlayWindow(QWidget):
    stop_requested = pyqtSignal()

    def __init__(self, display_duration=None, window_width=400, window_height=None):
        super().__init__()
        self.window_width = window_width

        screen_geometry = QApplication.primaryScreen().availableGeometry()
        self.window_height = window_height if window_height else screen_geometry.height()

        self.initUI()
        self.oldPos = self.pos()

    def showEvent(self, event):
        super().showEvent(event)
        if HAS_APPKIT:
            self._set_all_spaces()

    def _set_all_spaces(self):
        try:
            win_id = int(self.winId())
            ns_view = objc.objc_object(c_void_p=c_void_p(win_id))
            ns_window = ns_view.window()
            ns_window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
            )
            print("Window set to appear on all Spaces")
        except Exception as e:
            print(f"Could not set all-spaces behavior: {e}")

    def initUI(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        self.setLayout(layout)

        # Container with dark background
        self.container = QFrame()
        self.container.setStyleSheet("background-color: rgba(0, 0, 0, 150); border-radius: 10px;")
        container_layout = QVBoxLayout()
        container_layout.setContentsMargins(10, 10, 10, 10)
        self.container.setLayout(container_layout)

        # Single translation label
        self.translation_label = QLabel("")
        self.translation_label.setWordWrap(True)
        self.translation_label.setStyleSheet(
            "color: #ffffff; font-family: Arial; font-size: 20px; font-weight: bold;"
        )
        self.translation_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        container_layout.addWidget(self.translation_label)

        layout.addWidget(self.container)

        # Bottom control bar
        grip_layout = QHBoxLayout()

        from PyQt6.QtWidgets import QPushButton
        self.save_btn = QPushButton("💾 Save")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.setFixedWidth(80)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255, 255, 255, 50);
                color: white;
                border-radius: 5px;
                padding: 5px;
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 100);
            }
        """)
        self.save_btn.clicked.connect(self._save_transcript)
        grip_layout.addWidget(self.save_btn)

        self.stop_btn = QPushButton("⏹")
        self.stop_btn.setToolTip("Stop Translator")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.setFixedSize(30, 30)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(243, 139, 168, 150);
                color: white;
                border-radius: 15px;
                border: none;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(243, 139, 168, 200);
            }
        """)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        grip_layout.addWidget(self.stop_btn)

        grip_layout.addStretch()
        self.grip_label = ResizeHandle(self)
        grip_layout.addWidget(self.grip_label)
        layout.addLayout(grip_layout)

        self.resize(self.window_width, self.window_height)

        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.x() + screen.width() - self.window_width - 20
        y = screen.y()
        self.move(x, y)

        # Transcript history for saving
        self.transcript_data = {}  # chunk_id -> {timestamp, original, translated}

        # Display queue state
        self.translation_queue = []   # completed translations waiting to be shown
        self.displayed_chunk_id = None
        self.showing_completed = False

        # Timer: fires when the current completed translation has been shown for 2 seconds
        self.display_timer = QTimer()
        self.display_timer.setSingleShot(True)
        self.display_timer.timeout.connect(self._advance_queue)

        self.is_moving = False
        self.setMouseTracking(True)

    def update_text(self, chunk_id, original_text, translated_text):
        """Receive a transcription/translation update and manage the display queue."""
        print(f"[Overlay] Received update for #{chunk_id}: {original_text} -> {translated_text}")

        # Update transcript history
        if chunk_id not in self.transcript_data:
            self.transcript_data[chunk_id] = {
                'timestamp': time.strftime("%H:%M:%S"),
                'original': original_text,
                'translated': translated_text
            }
        else:
            if translated_text:
                self.transcript_data[chunk_id]['translated'] = translated_text

        is_placeholder = not translated_text or translated_text.strip() in ("", "...", "(translating...)")

        if is_placeholder:
            # Show a loading indicator only when the overlay is idle
            if self.displayed_chunk_id is None:
                self.translation_label.setText("...")
                self.displayed_chunk_id = chunk_id
                self.showing_completed = False
        else:
            if self.displayed_chunk_id == chunk_id and not self.showing_completed:
                # The placeholder for this chunk is on screen — replace it immediately
                self._show_translation(chunk_id, translated_text)
            elif not self.showing_completed:
                # Idle or showing another chunk's placeholder — show immediately
                self._show_translation(chunk_id, translated_text)
            else:
                # A completed translation is currently on screen — queue this one
                self.translation_queue.append(translated_text)
                print(f"[Overlay] Queued translation for #{chunk_id} ({len(self.translation_queue)} in queue)")

    def _show_translation(self, chunk_id, text):
        """Display a completed translation and start the 2-second hold timer."""
        self.translation_label.setText(text)
        self.displayed_chunk_id = chunk_id
        self.showing_completed = True
        self.display_timer.start(2000)
        print(f"[Overlay] Showing translation for #{chunk_id}")

    def _advance_queue(self):
        """Called after 2 seconds — show the next queued translation or go idle."""
        if self.translation_queue:
            text = self.translation_queue.pop(0)
            self.translation_label.setText(text)
            self.showing_completed = True
            self.display_timer.start(2000)
            print(f"[Overlay] Advanced queue, {len(self.translation_queue)} remaining")
        else:
            # No more queued translations — go idle but keep the last text visible
            self.showing_completed = False
            self.displayed_chunk_id = None

    def _save_transcript(self):
        """Save transcript history to file."""
        import os
        if not self.transcript_data:
            print("[Overlay] Nothing to save.")
            return

        os.makedirs("transcripts", exist_ok=True)
        filename = f"transcripts/transcript_{time.strftime('%Y%m%d_%H%M%S')}.txt"

        sorted_ids = sorted(self.transcript_data.keys())

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"Transcript saved at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 50 + "\n\n")
                for cid in sorted_ids:
                    data = self.transcript_data[cid]
                    f.write(
                        f"[{data['timestamp']}] (ID: {cid})\n"
                        f"Original: {data['original']}\n"
                        f"Translation: {data['translated']}\n"
                        f"{'-' * 30}\n"
                    )
            print(f"[Overlay] Saved to {filename}")
            original_text = self.save_btn.text()
            self.save_btn.setText("Saved!")
            QTimer.singleShot(2000, lambda: self.save_btn.setText(original_text))
        except Exception as e:
            print(f"[Overlay] Error saving transcript: {e}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_moving = True
            self.oldPos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self.is_moving:
            delta = event.globalPosition().toPoint() - self.oldPos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.oldPos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self.is_moving = False
        self.setCursor(Qt.CursorShape.ArrowCursor)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    window = OverlayWindow()
    window.show()
    window.update_text(1, "Hello world", "(translating...)")
    QTimer.singleShot(800, lambda: window.update_text(1, "Hello world", "你好，世界"))
    QTimer.singleShot(1000, lambda: window.update_text(2, "How are you?", "你好吗？"))
    QTimer.singleShot(1200, lambda: window.update_text(3, "Good morning", "早上好"))
    sys.exit(app.exec())
