import sys
import json
import requests
import csv
import threading
from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QLineEdit, QFormLayout, QTextEdit, QFileDialog
)
from PySide6.QtNetwork import QUdpSocket, QHostAddress
from PySide6.QtCore import QObject, Signal, Slot, QThread
import pyqtgraph as pg


class UdpParseWorker(QObject):
    parsed = Signal(str, float, float, float, float, float, float)  # ts, ax, ay, az, gx, gy, gz
    bad = Signal(str)

    @Slot(bytes)
    def process(self, b: bytes):
        try:
            raw = b.decode("utf-8", errors="ignore")
            msg = json.loads(raw)

            ts = msg.get("timestamp", "")
            ax = msg["accel"]["x"]
            ay = msg["accel"]["y"]
            az = msg["accel"]["z"]
            gx = msg["gyro"]["x"]
            gy = msg["gyro"]["y"]
            gz = msg["gyro"]["z"]

            self.parsed.emit(ts, ax, ay, az, gx, gy, gz)
        except Exception as e:
            self.bad.emit(f"Bad packet: {e}")


class MainWindow(QWidget):
    # Signals to cross threads
    datagram_signal = Signal(bytes)
    http_done = Signal(str, str)  # path, text
    http_err = Signal(str, str)   # path, err text

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IMU Control UI")

        # Layouts
        main_layout = QVBoxLayout()
        form = QFormLayout()

        self.status_label = QLabel("Status: not requested")
        main_layout.addWidget(self.status_label)

        # Calibration status
        self.calib_label = QLabel("Calibration: unknown")
        main_layout.addWidget(self.calib_label)

        # ESP URL field
        self.esp_url = QLineEdit("http://192.168.18.186:80")
        form.addRow("ESP URL", self.esp_url)

        # UDP listening port
        self.local_port_spin = QSpinBox()
        self.local_port_spin.setRange(1000, 65535)
        self.local_port_spin.setValue(9000)
        form.addRow("Local UDP Port", self.local_port_spin)

        # Stream + control buttons
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start Stream")
        self.stop_btn = QPushButton("Stop Stream")
        self.toggle_btn = QPushButton("Toggle Stream")
        self.status_btn = QPushButton("Get Status")
        self.listen_btn = QPushButton("Bind UDP")
        self.recalib_btn = QPushButton("Recalibrate")
        self.clear_btn = QPushButton("Clear Data")
        self.save_btn = QPushButton("Save CSV")
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.toggle_btn)
        btn_layout.addWidget(self.status_btn)
        btn_layout.addWidget(self.listen_btn)
        btn_layout.addWidget(self.recalib_btn)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addWidget(self.save_btn)
        main_layout.addLayout(btn_layout)

        # Delay control
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(10, 2000)
        self.delay_spin.setValue(50)
        form.addRow("IMU Delay (ms)", self.delay_spin)

        # Target control
        self.target_ip = QLineEdit("192.168.18.93")
        self.target_port = QSpinBox()
        self.target_port.setRange(1, 65535)
        self.target_port.setValue(9000)
        form.addRow("Target IP", self.target_ip)
        form.addRow("Target Port", self.target_port)

        self.set_target_btn = QPushButton("Save Target")
        self.reset_target_btn = QPushButton("Reset Target")
        form.addRow(self.set_target_btn, self.reset_target_btn)

        # Realtime numeric readouts
        mono = "font-family: monospace"
        self.ax_lbl = QLabel("0.000000"); self.ax_lbl.setStyleSheet(mono)
        self.ay_lbl = QLabel("0.000000"); self.ay_lbl.setStyleSheet(mono)
        self.az_lbl = QLabel("0.000000"); self.az_lbl.setStyleSheet(mono)
        self.gx_lbl = QLabel("0.000000"); self.gx_lbl.setStyleSheet(mono)
        self.gy_lbl = QLabel("0.000000"); self.gy_lbl.setStyleSheet(mono)
        self.gz_lbl = QLabel("0.000000"); self.gz_lbl.setStyleSheet(mono)
        form.addRow("Accel X/Y/Z (g)", self._hbox(self.ax_lbl, self.ay_lbl, self.az_lbl))
        form.addRow("Gyro X/Y/Z (deg/s)", self._hbox(self.gx_lbl, self.gy_lbl, self.gz_lbl))

        main_layout.addLayout(form)

        # Log window
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        main_layout.addWidget(self.log)

        # Accelerometer plot with legend
        self.plot_accel = pg.PlotWidget(title="Accelerometer X/Y/Z (g)")
        self.plot_accel.addLegend()
        self.accel_curves = [
            self.plot_accel.plot(pen="r", name="Accel X"),
            self.plot_accel.plot(pen="g", name="Accel Y"),
            self.plot_accel.plot(pen="b", name="Accel Z")
        ]
        self.data_ax, self.data_ay, self.data_az = [], [], []
        main_layout.addWidget(self.plot_accel)

        # Gyroscope plot with legend
        self.plot_gyro = pg.PlotWidget(title="Gyroscope X/Y/Z (deg/s)")
        self.plot_gyro.addLegend()
        self.gyro_curves = [
            self.plot_gyro.plot(pen="r", name="Gyro X"),
            self.plot_gyro.plot(pen="g", name="Gyro Y"),
            self.plot_gyro.plot(pen="b", name="Gyro Z")
        ]
        self.data_gx, self.data_gy, self.data_gz = [], [], []
        main_layout.addWidget(self.plot_gyro)

        self.setLayout(main_layout)

        # Extra: store timestamps for CSV
        self.timestamps = []

        # UDP socket (not bound until you press "Bind UDP")
        self.udp_socket = QUdpSocket()

        # Background UDP parse thread
        self.udp_thread = QThread(self)
        self.udp_worker = UdpParseWorker()
        self.udp_worker.moveToThread(self.udp_thread)
        self.datagram_signal.connect(self.udp_worker.process)     # main -> worker
        self.udp_worker.parsed.connect(self.on_parsed)            # worker -> main
        self.udp_worker.bad.connect(self.on_bad_packet)           # worker -> main
        self.udp_thread.start()

        # Signals
        self.start_btn.clicked.connect(lambda: self.send_http("/stream/start"))
        self.stop_btn.clicked.connect(lambda: self.send_http("/stream/stop"))
        self.toggle_btn.clicked.connect(lambda: self.send_http("/stream/toggle"))
        self.status_btn.clicked.connect(lambda: self.send_http("/status"))
        self.recalib_btn.clicked.connect(lambda: self.send_http("/recalibrate", post=True))
        self.clear_btn.clicked.connect(self.clear_data)
        self.save_btn.clicked.connect(self.save_csv)
        self.delay_spin.editingFinished.connect(self.set_delay)
        self.set_target_btn.clicked.connect(self.save_target)
        self.reset_target_btn.clicked.connect(lambda: self.send_http("/target/reset"))
        self.listen_btn.clicked.connect(self.bind_udp)

        # HTTP results back to UI thread
        self.http_done.connect(self.on_http_done)
        self.http_err.connect(self.on_http_err)

    def _hbox(self, *widgets):
        hb = QHBoxLayout()
        for w in widgets:
            hb.addWidget(w)
        container = QWidget()
        container.setLayout(hb)
        return container

    def base_url(self):
        return self.esp_url.text().strip()

    def send_http(self, path, post=False):
        # Run requests in a background thread
        def work():
            try:
                url = f"{self.base_url()}{path}"
                if path.startswith("/imu/delay"):
                    r = requests.get(url, params={"ms": self.delay_spin.value()}, timeout=2)
                elif path.startswith("/target/set"):
                    r = requests.get(url, params={
                        "ip": self.target_ip.text(),
                        "port": self.target_port.value()
                    }, timeout=2)
                elif post:
                    r = requests.post(url, timeout=2)
                else:
                    r = requests.get(url, timeout=2)
                self.http_done.emit(path, r.text)
            except Exception as e:
                self.http_err.emit(path, str(e))
        threading.Thread(target=work, daemon=True).start()

    @Slot(str, str)
    def on_http_done(self, path, text):
        if path == "/status":
            self.status_label.setText(f"Status: {text}")
            try:
                data = json.loads(text)
                if "last_calibration" in data:
                    self.calib_label.setText(
                        f"Calibration: last={data['last_calibration']} calibrating={data['calibrating']}"
                    )
            except Exception:
                pass
        self.log.append(f"HTTP {path}: {text}")

    @Slot(str, str)
    def on_http_err(self, path, err):
        self.status_label.setText(f"Error: {err}")
        self.log.append(f"Error calling {path}: {err}")

    def set_delay(self):
        self.send_http(f"/imu/delay")

    def save_target(self):
        self.send_http(f"/target/set")

    def bind_udp(self):
        port = self.local_port_spin.value()
        if self.udp_socket.bind(QHostAddress.Any, port):
            self.udp_socket.readyRead.connect(self.on_udp)
            self.log.append(f"Listening on UDP port {port}")
        else:
            self.log.append(f"Failed to bind UDP port {port}")

    def clear_data(self):
        # Reset accel, gyro, and timestamps
        self.data_ax.clear()
        self.data_ay.clear()
        self.data_az.clear()
        self.data_gx.clear()
        self.data_gy.clear()
        self.data_gz.clear()
        self.timestamps.clear()

        # Reset the plot visuals
        for c in self.accel_curves + self.gyro_curves:
            c.setData([])

        self.plot_accel.enableAutoRange()
        self.plot_gyro.enableAutoRange()

        # Reset numeric readouts
        self.ax_lbl.setText("0.000000")
        self.ay_lbl.setText("0.000000")
        self.az_lbl.setText("0.000000")
        self.gx_lbl.setText("0.000000")
        self.gy_lbl.setText("0.000000")
        self.gz_lbl.setText("0.000000")

        self.log.append("Data cleared and plots reset")

    def save_csv(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Save Data", "imu_data.csv", "CSV Files (*.csv)")
        if not filename:
            return

        n = min(len(self.timestamps), len(self.data_ax), len(self.data_ay), len(self.data_az),
                len(self.data_gx), len(self.data_gy), len(self.data_gz))

        DECIMALS = 6
        def f(x):
            return f"{float(x):.{DECIMALS}f}"

        try:
            with open(filename, "w", newline="") as fcsv:
                writer = csv.writer(fcsv)
                writer.writerow(["index", "timestamp", "ax", "ay", "az", "gx", "gy", "gz"])
                for i in range(n):
                    writer.writerow([
                        i,
                        self.timestamps[i],
                        f(self.data_ax[i]),
                        f(self.data_ay[i]),
                        f(self.data_az[i]),
                        f(self.data_gx[i]),
                        f(self.data_gy[i]),
                        f(self.data_gz[i])
                    ])
            self.log.append(f"Saved {n} samples to {filename}")
        except Exception as e:
            self.log.append(f"Error saving CSV: {e}")

    # Main thread: minimal work, just read datagrams and hand bytes to worker
    def on_udp(self):
        while self.udp_socket.hasPendingDatagrams():
            datagram, _, _ = self.udp_socket.readDatagram(self.udp_socket.pendingDatagramSize())
            # hand off to worker thread
            self.datagram_signal.emit(bytes(datagram))

    # Parsed result comes back from worker here, update UI/plots
    @Slot(str, float, float, float, float, float, float)
    def on_parsed(self, ts, ax, ay, az, gx, gy, gz):
        self.timestamps.append(ts)

        self.data_ax.append(ax); self.data_ay.append(ay); self.data_az.append(az)
        self.data_gx.append(gx); self.data_gy.append(gy); self.data_gz.append(gz)

        if len(self.data_ax) > 200:
            self.data_ax.pop(0); self.data_ay.pop(0); self.data_az.pop(0)
        if len(self.data_gx) > 200:
            self.data_gx.pop(0); self.data_gy.pop(0); self.data_gz.pop(0)

        self.accel_curves[0].setData(self.data_ax)
        self.accel_curves[1].setData(self.data_ay)
        self.accel_curves[2].setData(self.data_az)

        self.gyro_curves[0].setData(self.data_gx)
        self.gyro_curves[1].setData(self.data_gy)
        self.gyro_curves[2].setData(self.data_gz)

        self.ax_lbl.setText(f"{ax:.6f}")
        self.ay_lbl.setText(f"{ay:.6f}")
        self.az_lbl.setText(f"{az:.6f}")
        self.gx_lbl.setText(f"{gx:.6f}")
        self.gy_lbl.setText(f"{gy:.6f}")
        self.gz_lbl.setText(f"{gz:.6f}")

        self.log.append(
            f"UDP packet: ts={ts} accel=({ax:.3f},{ay:.3f},{az:.3f}) "
            f"gyro=({gx:.3f},{gy:.3f},{gz:.3f})"
        )

    @Slot(str)
    def on_bad_packet(self, msg):
        self.log.append(msg)

    def closeEvent(self, event):
        # Ensure worker thread exits cleanly
        if self.udp_thread.isRunning():
            self.udp_thread.quit()
            self.udp_thread.wait(1000)
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
