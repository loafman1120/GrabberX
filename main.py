import os
import sys
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit,
    QFileDialog, QProgressBar, QMessageBox
)

import yt_dlp


@dataclass
class DownloadOptions:
    url: str
    out_dir: str
    cookies_file: str | None = None


class YtDlpWorker(QObject):
    progress = Signal(int)          # 0-100 (best-effort)
    status = Signal(str)            # short status
    log = Signal(str)               # verbose log
    finished = Signal(bool, str)    # success, message

    def __init__(self, opts: DownloadOptions):
        super().__init__()
        self.opts = opts
        self._cancel = False

    def cancel(self):
        self._cancel = True
        self.log.emit("取消请求已发送，等待 yt-dlp 停止…")

    def _hook(self, d):
        # 进度回调在下载线程里触发
        if self._cancel:
            raise yt_dlp.utils.DownloadError("Cancelled by user")

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes")
            if total and downloaded is not None:
                pct = int(downloaded * 100 / total)
                self.progress.emit(max(0, min(100, pct)))

            spd = d.get("speed")
            eta = d.get("eta")
            filename = os.path.basename(d.get("filename", ""))
            msg = f"下载中: {filename}"
            if spd:
                msg += f" | {spd/1024/1024:.2f} MiB/s"
            if eta is not None:
                msg += f" | ETA {eta}s"
            self.status.emit(msg)

        elif status == "finished":
            self.progress.emit(100)
            self.status.emit("下载完成，正在后处理/合并…")
            self.log.emit("文件下载完成，等待后处理…")

    def run(self):
        try:
            os.makedirs(self.opts.out_dir, exist_ok=True)

            ydl_opts = {
                # 输出模板：目录/标题[ID].ext
                "outtmpl": os.path.join(self.opts.out_dir, "%(title)s [%(id)s].%(ext)s"),
                "progress_hooks": [self._hook],
                "noplaylist": False,  # B站合集/多P会当 playlist 处理；你也可以改 True
                "retries": 5,
                "fragment_retries": 5,
                "concurrent_fragment_downloads": 4,
                "ignoreerrors": False,
                "quiet": True,        # 我们自己把信息写到 log
                "no_warnings": True,
                "merge_output_format": "mp4",  # 需要 ffmpeg
            }

            # 可选 cookies（登录态）
            if self.opts.cookies_file:
                ydl_opts["cookiefile"] = self.opts.cookies_file

            # 你也可以按需加 UA / referer（某些情况有用）
            # ydl_opts["http_headers"] = {"User-Agent": "...", "Referer": "https://www.bilibili.com"}

            self.log.emit(f"开始下载: {self.opts.url}")
            self.log.emit(f"保存目录: {self.opts.out_dir}")
            if self.opts.cookies_file:
                self.log.emit(f"使用 cookies: {self.opts.cookies_file}")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 先 extract 信息，便于早发现错误
                info = ydl.extract_info(self.opts.url, download=False)
                title = info.get("title") if isinstance(info, dict) else None
                if title:
                    self.status.emit(f"解析成功: {title}")
                    self.log.emit(f"标题: {title}")

                # 真正下载
                ydl.download([self.opts.url])

            self.finished.emit(True, "下载完成")
        except Exception as e:
            self.finished.emit(False, str(e))


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bilibili 下载 Demo (yt-dlp + PySide6)")
        self.resize(720, 520)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("输入 Bilibili 链接，如 https://www.bilibili.com/video/BV...")

        self.dir_label = QLabel("保存目录：未选择")
        self.choose_dir_btn = QPushButton("选择目录")

        self.cookies_edit = QLineEdit()
        self.cookies_edit.setPlaceholderText("可选：cookies.txt 路径（用于登录态/风控）")
        self.choose_cookies_btn = QPushButton("选择 cookies")

        self.start_btn = QPushButton("开始下载")
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.status_label = QLabel("就绪")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        top = QVBoxLayout(self)

        top.addWidget(QLabel("视频链接："))
        top.addWidget(self.url_edit)

        row1 = QHBoxLayout()
        row1.addWidget(self.dir_label, 1)
        row1.addWidget(self.choose_dir_btn)
        top.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(self.cookies_edit, 1)
        row2.addWidget(self.choose_cookies_btn)
        top.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(self.start_btn)
        row3.addWidget(self.cancel_btn)
        top.addLayout(row3)

        top.addWidget(self.progress)
        top.addWidget(self.status_label)
        top.addWidget(QLabel("日志："))
        top.addWidget(self.log_view, 1)

        self.out_dir = ""
        self.worker = None
        self.thread = None

        self.choose_dir_btn.clicked.connect(self.choose_dir)
        self.choose_cookies_btn.clicked.connect(self.choose_cookies)
        self.start_btn.clicked.connect(self.start_download)
        self.cancel_btn.clicked.connect(self.cancel_download)

    @Slot()
    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if d:
            self.out_dir = d
            self.dir_label.setText(f"保存目录：{d}")

    @Slot()
    def choose_cookies(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", filter="Text Files (*.txt);;All Files (*)")
        if f:
            self.cookies_edit.setText(f)

    def append_log(self, s: str):
        self.log_view.append(s)

    def set_busy(self, busy: bool):
        self.start_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.choose_dir_btn.setEnabled(not busy)
        self.choose_cookies_btn.setEnabled(not busy)

    @Slot()
    def start_download(self):
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入链接")
            return
        if not self.out_dir:
            QMessageBox.warning(self, "提示", "请选择保存目录")
            return

        cookies = self.cookies_edit.text().strip() or None

        self.progress.setValue(0)
        self.log_view.clear()
        self.status_label.setText("启动下载线程…")
        self.set_busy(True)

        opts = DownloadOptions(url=url, out_dir=self.out_dir, cookies_file=cookies)
        self.worker = YtDlpWorker(opts)

        # 用 Python 线程即可（避免阻塞 UI）
        self.thread = threading.Thread(target=self.worker.run, daemon=True)

        # 连接信号
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(self.status_label.setText)
        self.worker.log.connect(self.append_log)
        self.worker.finished.connect(self.on_finished)

        self.thread.start()

    @Slot()
    def cancel_download(self):
        if self.worker:
            self.worker.cancel()
            self.status_label.setText("正在取消…")

    @Slot(bool, str)
    def on_finished(self, ok: bool, msg: str):
        self.set_busy(False)
        if ok:
            self.status_label.setText("完成")
            self.append_log("✅ " + msg)
        else:
            self.status_label.setText("失败")
            self.append_log("❌ " + msg)
            QMessageBox.critical(self, "下载失败", msg)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()