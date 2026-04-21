"""
AndroidStream — Live Android-to-PC Virtual Camera
==================================================
Receives JPEG frames from an Android device over ADB port forwarding,
displays them in a real-time preview window, and pipes them into a
virtual camera driver so any application (OBS, Zoom, Teams, etc.)
can use the phone as a webcam.

Requirements
------------
    pip install PyQt6 opencv-python pyvirtualcam numpy

    ADB must be present in PATH.
    Install via: https://developer.android.com/tools/releases/platform-tools

    pyvirtualcam on Windows requires the OBS Virtual Camera driver.
    Install OBS Studio 28+ and launch it once to register the driver.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME   = "AndroidStream"
VERSION    = "1.0"
HOST       = "127.0.0.1"
DEFAULT_PORT = 8080
HEADER_FMT = ">I"          # 4-byte big-endian uint32 frame-size prefix
MAX_FRAME_BYTES = 10_000_000  # 10 MB safety ceiling per frame

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

DARK_BG      = "#0d0d12"
CARD_BG      = "#13131c"
ACCENT       = "#ff3c3c"
ACCENT2      = "#ff6b35"
TEXT_PRIMARY = "#f0f0f5"
TEXT_MUTED   = "#6b6b80"
BORDER       = "#1e1e2e"
SUCCESS      = "#2ecc71"


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class AdbWorker(QThread):
    """
    Verifies that an Android device is connected via USB and sets up
    TCP port forwarding between the host and the device.
    """

    result = pyqtSignal(bool, str)   # (success, message)

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    def run(self) -> None:
        try:
            # ---- Detect connected devices ---------------------------------
            probe = subprocess.run(
                ["adb", "devices"],
                capture_output=True, text=True, timeout=5,
            )
            devices = [
                line for line in probe.stdout.strip().splitlines()
                if "\t" in line and "device" in line
            ]
            if not devices:
                self.result.emit(False, "No Android device detected over USB")
                return

            device_serial = devices[0].split("\t")[0]

            # ---- Forward port --------------------------------------------
            forward = subprocess.run(
                ["adb", "forward", f"tcp:{self.port}", f"tcp:{self.port}"],
                capture_output=True, text=True, timeout=5,
            )
            if forward.returncode == 0:
                self.result.emit(True, f"ADB ready  |  device: {device_serial}")
            else:
                self.result.emit(False, f"ADB forward failed: {forward.stderr.strip()}")

        except FileNotFoundError:
            self.result.emit(
                False,
                "adb not found in PATH — install Android Platform Tools",
            )
        except subprocess.TimeoutExpired:
            self.result.emit(False, "ADB command timed out")
        except Exception as exc:  # noqa: BLE001
            self.result.emit(False, f"ADB error: {exc}")


class StreamWorker(QThread):
    """
    Connects to the Android broadcaster app via TCP, reads length-prefixed
    JPEG frames, decodes them with OpenCV, and emits each frame as a QImage
    for the preview widget.  Frames are simultaneously forwarded to the
    virtual camera driver when pyvirtualcam is available.

    Wire protocol
    -------------
    Each message is:
        [uint32 big-endian frame_size] [frame_size bytes of JPEG data]
    """

    frame_ready  = pyqtSignal(QImage)
    stats_update = pyqtSignal(int, float)   # fps, kbps
    error        = pyqtSignal(str)
    connected    = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(
        self,
        host: str,
        port: int,
        width: int,
        height: int,
        fps: int,
    ) -> None:
        super().__init__()
        self.host   = host
        self.port   = port
        self.width  = width
        self.height = height
        self.fps    = fps
        self._stop  = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        """Block until exactly *n* bytes have been received."""
        buf = bytearray()
        while len(buf) < n and not self._stop.is_set():
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionResetError("Connection closed by device")
            buf += chunk
        return bytes(buf)

    def _open_virtual_camera(self):
        """Return a pyvirtualcam.Camera instance or None if unavailable."""
        try:
            import pyvirtualcam  # type: ignore[import]
            return pyvirtualcam.Camera(
                width=self.width,
                height=self.height,
                fps=self.fps,
                fmt=pyvirtualcam.PixelFormat.RGB,
            )
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        vcam = self._open_virtual_camera()
        sock: socket.socket | None = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((self.host, self.port))
            sock.settimeout(None)
            self.connected.emit()

            frame_count    = 0
            bytes_received = 0
            t0             = time.monotonic()

            while not self._stop.is_set():
                # ---- Read frame header -----------------------------------
                header_raw = self._recv_exact(sock, 4)
                if len(header_raw) < 4:
                    break
                frame_size: int = struct.unpack(HEADER_FMT, header_raw)[0]

                if frame_size == 0 or frame_size > MAX_FRAME_BYTES:
                    continue   # skip malformed or oversized frames

                # ---- Read JPEG payload ----------------------------------
                jpeg = self._recv_exact(sock, frame_size)
                bytes_received += frame_size

                # ---- Decode ---------------------------------------------
                arr   = np.frombuffer(jpeg, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(
                        frame,
                        (self.width, self.height),
                        interpolation=cv2.INTER_LINEAR,
                    )

                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                # ---- Virtual camera output ------------------------------
                if vcam is not None:
                    try:
                        vcam.send(frame_rgb)
                        vcam.sleep_until_next_frame()
                    except Exception:  # noqa: BLE001
                        pass

                # ---- GUI preview output ---------------------------------
                h, w, ch = frame_rgb.shape
                qi = QImage(
                    frame_rgb.data, w, h, w * ch, QImage.Format.Format_RGB888
                ).copy()
                self.frame_ready.emit(qi)

                # ---- Statistics (update every second) ------------------
                frame_count += 1
                elapsed = time.monotonic() - t0
                if elapsed >= 1.0:
                    fps  = frame_count / elapsed
                    kbps = (bytes_received * 8) / elapsed / 1000
                    self.stats_update.emit(int(fps), round(kbps, 1))
                    frame_count    = 0
                    bytes_received = 0
                    t0 = time.monotonic()

        except ConnectionRefusedError:
            self.error.emit(
                "Connection refused — make sure the broadcaster app is running on the device"
            )
        except ConnectionResetError as exc:
            self.error.emit(f"Connection dropped: {exc}")
        except OSError as exc:
            if not self._stop.is_set():
                self.error.emit(f"Network error: {exc}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
            if vcam:
                try:
                    vcam.close()
                except Exception:  # noqa: BLE001
                    pass
            self.disconnected.emit()


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------

class LiveBadge(QLabel):
    """
    Pulsing LIVE indicator badge.
    Red when streaming, neutral grey when idle.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(70, 26)
        self._alpha = 255
        self._live  = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pulse)
        self.set_live(False)

    def set_live(self, on: bool) -> None:
        self._live = on
        if on:
            self._timer.start(600)
        else:
            self._timer.stop()
            self._alpha = 255
        self.update()

    def _pulse(self) -> None:
        self._alpha = 80 if self._alpha == 255 else 255
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._live:
            color = QColor(ACCENT)
            color.setAlpha(self._alpha)
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(0, 0, self.width(), self.height(), 13, 13)
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "  LIVE")
        else:
            p.setBrush(QColor(BORDER))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(0, 0, self.width(), self.height(), 13, 13)
            p.setPen(QColor(TEXT_MUTED))
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "  OFF")


class StatCard(QFrame):
    """Small metric card displaying a label and a large numeric value."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background: {CARD_BG}; border: 1px solid {BORDER};"
            f" border-radius: 10px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        header = QLabel(label)
        header.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px; border: none;")
        layout.addWidget(header)

        self._value = QLabel("--")
        self._value.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 20px; font-weight: 700; border: none;"
        )
        layout.addWidget(self._value)

    def set_value(self, val: str) -> None:
        self._value.setText(val)


class PreviewWidget(QLabel):
    """
    Scaled preview area.  Displays a grid placeholder when idle and
    renders incoming frames maintaining the original aspect ratio.
    """

    _PLACEHOLDER_W = 640
    _PLACEHOLDER_H = 360

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(self._PLACEHOLDER_W, self._PLACEHOLDER_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._draw_placeholder()

    def _draw_placeholder(self) -> None:
        w, h = self._PLACEHOLDER_W, self._PLACEHOLDER_H
        img  = QImage(w, h, QImage.Format.Format_RGB888)
        img.fill(QColor("#0a0a10"))

        p = QPainter(img)
        p.setPen(QPen(QColor(BORDER), 1))
        for x in range(0, w, 40):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, 40):
            p.drawLine(0, y, w, y)

        p.setPen(QColor(TEXT_MUTED))
        p.setFont(QFont("Segoe UI", 13))
        p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for stream...")
        p.end()

        self.setPixmap(QPixmap.fromImage(img))

    def update_frame(self, qi: QImage) -> None:
        pix = QPixmap.fromImage(qi).scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pix)

    def reset(self) -> None:
        self._draw_placeholder()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{VERSION}  —  Live Android Broadcaster")
        self.setMinimumSize(920, 680)

        self._stream_worker: StreamWorker | None = None
        self._adb_worker:    AdbWorker    | None = None
        self._streaming = False

        self._build_ui()
        self._apply_stylesheet()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        # Header row
        header = QHBoxLayout()

        title = QLabel(f"<b>{APP_NAME}</b>")
        title.setObjectName("appTitle")
        header.addWidget(title)

        subtitle = QLabel("Android  ->  Virtual Camera  ->  OBS / Zoom / Teams")
        subtitle.setObjectName("subtitle")
        header.addWidget(subtitle)
        header.addStretch()

        self.live_badge = LiveBadge()
        header.addWidget(self.live_badge)
        root.addLayout(header)

        # Horizontal rule
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator")
        root.addWidget(sep)

        # Content row: preview | controls
        content = QHBoxLayout()
        content.setSpacing(14)
        root.addLayout(content)

        self.preview = PreviewWidget()
        self.preview.setObjectName("preview")
        content.addWidget(self.preview, stretch=3)

        panel = QVBoxLayout()
        panel.setSpacing(10)
        content.addLayout(panel, stretch=1)

        # ADB card
        adb_card = self._make_card("ADB  &  Connection")
        adb_lay  = adb_card.layout()

        adb_lay.addWidget(self._field_label("TCP Port"))
        self.port_spin = QSpinBox()
        self.port_spin.setObjectName("spinbox")
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        adb_lay.addWidget(self.port_spin)

        self.adb_btn = QPushButton("Activate ADB")
        self.adb_btn.setObjectName("btnSecondary")
        self.adb_btn.clicked.connect(self._run_adb)
        adb_lay.addWidget(self.adb_btn)

        self.adb_status = QLabel("Not activated")
        self.adb_status.setObjectName("statusLabel")
        self.adb_status.setWordWrap(True)
        adb_lay.addWidget(self.adb_status)

        panel.addWidget(adb_card)

        # Stream settings card
        stream_card = self._make_card("Stream Settings")
        s_lay = stream_card.layout()

        s_lay.addWidget(self._field_label("Resolution"))
        self.res_combo = QComboBox()
        self.res_combo.setObjectName("combo")
        self.res_combo.addItems(["1920x1080", "1280x720", "854x480", "640x360"])
        self.res_combo.setCurrentIndex(1)
        s_lay.addWidget(self.res_combo)

        s_lay.addWidget(self._field_label("Target FPS"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setObjectName("spinbox")
        self.fps_spin.setRange(5, 60)
        self.fps_spin.setValue(30)
        s_lay.addWidget(self.fps_spin)

        panel.addWidget(stream_card)

        # Stats card
        stats_card = self._make_card("Statistics")
        st_lay = stats_card.layout()

        stats_row = QHBoxLayout()
        self.fps_stat  = StatCard("FPS")
        self.kbps_stat = StatCard("Kbps")
        stats_row.addWidget(self.fps_stat)
        stats_row.addWidget(self.kbps_stat)
        st_lay.addLayout(stats_row)

        panel.addWidget(stats_card)
        panel.addStretch()

        # Primary action button
        self.connect_btn = QPushButton("Start Stream")
        self.connect_btn.setObjectName("btnPrimary")
        self.connect_btn.setFixedHeight(52)
        self.connect_btn.clicked.connect(self._toggle_stream)
        panel.addWidget(self.connect_btn)

        # Info banner
        info = QLabel(
            "In OBS: Add Source  ->  Video Capture Device  ->  select the Virtual Camera"
        )
        info.setObjectName("infoBanner")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(info)

        # Status bar
        self.status_bar = self.statusBar()
        self.status_bar.showMessage(
            "Ready  —  connect your phone via USB and activate ADB first"
        )

    def _make_card(self, title: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(7)
        lbl = QLabel(title)
        lbl.setObjectName("cardTitle")
        lay.addWidget(lbl)
        return card

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("fieldLabel")
        return lbl

    # ------------------------------------------------------------------
    # Stylesheet
    # ------------------------------------------------------------------

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {DARK_BG};
                color: {TEXT_PRIMARY};
                font-family: "Segoe UI", "SF Pro Display", Arial;
                font-size: 13px;
            }}

            #appTitle {{
                font-size: 22px;
                font-weight: 800;
                color: {TEXT_PRIMARY};
                letter-spacing: 1px;
            }}

            #subtitle {{
                font-size: 12px;
                color: {TEXT_MUTED};
                margin-left: 10px;
            }}

            #separator {{
                border: none;
                border-top: 1px solid {BORDER};
                margin: 0;
            }}

            #preview {{
                background: #0a0a10;
                border: 2px solid {BORDER};
                border-radius: 14px;
            }}

            #card {{
                background: {CARD_BG};
                border: 1px solid {BORDER};
                border-radius: 12px;
            }}

            #cardTitle {{
                font-size: 11px;
                font-weight: 700;
                color: {TEXT_MUTED};
                text-transform: uppercase;
                letter-spacing: 1.5px;
            }}

            #fieldLabel {{
                font-size: 11px;
                color: {TEXT_MUTED};
            }}

            #statusLabel {{
                font-size: 11px;
                color: {SUCCESS};
                padding: 4px 0;
            }}

            QSpinBox#spinbox, QComboBox#combo {{
                background: {DARK_BG};
                border: 1px solid {BORDER};
                border-radius: 7px;
                padding: 6px 10px;
                color: {TEXT_PRIMARY};
                font-size: 13px;
            }}

            QSpinBox#spinbox::up-button,
            QSpinBox#spinbox::down-button {{
                width: 18px;
                background: {BORDER};
                border-radius: 4px;
            }}

            QComboBox#combo::drop-down {{
                border: none;
                width: 20px;
            }}

            QComboBox QAbstractItemView {{
                background: {CARD_BG};
                border: 1px solid {BORDER};
                color: {TEXT_PRIMARY};
                selection-background-color: {ACCENT};
            }}

            #btnPrimary {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 {ACCENT}, stop:1 {ACCENT2}
                );
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 15px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }}

            #btnPrimary:hover {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #ff5555, stop:1 #ff8050
                );
            }}

            #btnPrimary:pressed {{
                background: #cc2222;
            }}

            #btnSecondary {{
                background: {BORDER};
                color: {TEXT_PRIMARY};
                border: 1px solid #2a2a40;
                border-radius: 8px;
                padding: 7px;
                font-weight: 600;
            }}

            #btnSecondary:hover {{
                background: #2a2a40;
                border-color: {ACCENT};
            }}

            #infoBanner {{
                background: #131320;
                border: 1px solid {BORDER};
                border-radius: 8px;
                color: {TEXT_MUTED};
                font-size: 12px;
                padding: 7px;
            }}

            QStatusBar {{
                background: {CARD_BG};
                color: {TEXT_MUTED};
                font-size: 11px;
                border-top: 1px solid {BORDER};
            }}
        """)

    # ------------------------------------------------------------------
    # ADB
    # ------------------------------------------------------------------

    def _run_adb(self) -> None:
        self.adb_btn.setEnabled(False)
        self.adb_btn.setText("Activating...")
        self.adb_status.setText("...")

        self._adb_worker = AdbWorker(self.port_spin.value())
        self._adb_worker.result.connect(self._on_adb_result)
        self._adb_worker.start()

    def _on_adb_result(self, ok: bool, msg: str) -> None:
        self.adb_status.setText(msg)
        self.adb_status.setStyleSheet(
            f"color: {SUCCESS if ok else ACCENT}; font-size: 11px; padding: 4px 0;"
        )
        self.adb_btn.setEnabled(True)
        self.adb_btn.setText("Activate ADB")
        self.status_bar.showMessage(msg)

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def _parse_resolution(self) -> tuple[int, int]:
        text = self.res_combo.currentText()   # e.g. "1280x720"
        w, h = text.replace("x", "x").split("x")
        return int(w), int(h)

    def _toggle_stream(self) -> None:
        if self._streaming:
            self._stop_stream()
        else:
            self._start_stream()

    def _start_stream(self) -> None:
        width, height = self._parse_resolution()
        fps  = self.fps_spin.value()
        port = self.port_spin.value()

        self._stream_worker = StreamWorker(HOST, port, width, height, fps)
        self._stream_worker.frame_ready.connect(self._on_frame)
        self._stream_worker.stats_update.connect(self._on_stats)
        self._stream_worker.error.connect(self._on_stream_error)
        self._stream_worker.connected.connect(self._on_connected)
        self._stream_worker.disconnected.connect(self._on_disconnected)
        self._stream_worker.start()

        self.connect_btn.setText("Connecting...")
        self.connect_btn.setEnabled(False)
        self.status_bar.showMessage("Connecting to device...")

    def _stop_stream(self) -> None:
        if self._stream_worker:
            self._stream_worker.stop()
            self._stream_worker.wait(3000)
            self._stream_worker = None

        self._streaming = False
        self.connect_btn.setText("Start Stream")
        self.connect_btn.setEnabled(True)
        self.live_badge.set_live(False)
        self.preview.reset()
        self.fps_stat.set_value("--")
        self.kbps_stat.set_value("--")
        self.status_bar.showMessage("Stream stopped")

    def _on_connected(self) -> None:
        self._streaming = True
        self.connect_btn.setText("Stop Stream")
        self.connect_btn.setEnabled(True)
        self.live_badge.set_live(True)
        self.status_bar.showMessage("Live  —  virtual camera is active")

    def _on_disconnected(self) -> None:
        if self._streaming:
            self._stop_stream()
            self.status_bar.showMessage("Connection lost")

    def _on_stream_error(self, msg: str) -> None:
        self.status_bar.showMessage(msg)
        self.adb_status.setText(msg)
        self.adb_status.setStyleSheet(
            f"color: {ACCENT}; font-size: 11px; padding: 4px 0;"
        )
        self._stop_stream()

    def _on_frame(self, qi: QImage) -> None:
        self.preview.update_frame(qi)

    def _on_stats(self, fps: int, kbps: float) -> None:
        self.fps_stat.set_value(str(fps))
        self.kbps_stat.set_value(f"{kbps:.0f}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_stream()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
