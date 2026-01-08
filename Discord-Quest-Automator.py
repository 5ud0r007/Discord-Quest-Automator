import sys
import os
import time
import json
import subprocess
import requests
import websocket
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel,
                             QProgressBar, QPushButton, QFrame, QHBoxLayout)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint

JS_CODE = r"""
(async function() {
    window.questStatus = { name: "Loading...", current: 0, total: 1, status: "init" };

    delete window.$;
    let wpRequire = webpackChunkdiscord_app.push([[Symbol()], {}, r => r]);
    webpackChunkdiscord_app.pop();

    let ApplicationStreamingStore = Object.values(wpRequire.c).find(x => x?.exports?.Z?.__proto__?.getStreamerActiveStreamMetadata).exports.Z;
    let RunningGameStore = Object.values(wpRequire.c).find(x => x?.exports?.ZP?.getRunningGames).exports.ZP;
    let QuestsStore = Object.values(wpRequire.c).find(x => x?.exports?.Z?.__proto__?.getQuest).exports.Z;
    let FluxDispatcher = Object.values(wpRequire.c).find(x => x?.exports?.Z?.__proto__?.flushWaitQueue).exports.Z;
    let api = Object.values(wpRequire.c).find(x => x?.exports?.tn?.get).exports.tn;

    let quests = [...QuestsStore.quests.values()].filter(x => 
        x.id !== "1412491570820812933" && 
        x.userStatus?.enrolledAt && 
        !x.userStatus?.completedAt && 
        new Date(x.config.expiresAt).getTime() > Date.now()
    );

    if(quests.length === 0) {
        window.questStatus.status = "no_quest";
        return;
    }

    const updatePython = (name, curr, tot, stat) => {
        window.questStatus.name = name;
        window.questStatus.current = curr;
        window.questStatus.total = tot;
        window.questStatus.status = stat;
    };

    for (const quest of quests) {
        const pid = Math.floor(Math.random() * 30000) + 1000;
        const applicationId = quest.config.application.id;
        const questName = quest.config.messages.questName;
        const taskConfig = quest.config.taskConfig ?? quest.config.taskConfigV2;
        const taskName = ["WATCH_VIDEO", "PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP", "PLAY_ACTIVITY", "WATCH_VIDEO_ON_MOBILE"].find(x => taskConfig.tasks[x] != null);
        const secondsNeeded = taskConfig.tasks[taskName].target;
        let secondsDone = quest.userStatus?.progress?.[taskName]?.value ?? 0;

        updatePython(questName, secondsDone, secondsNeeded, "running");

        if(taskName === "WATCH_VIDEO" || taskName === "WATCH_VIDEO_ON_MOBILE") {
            const speed = 7;
            while(secondsDone < secondsNeeded) {
                const timestamp = secondsDone + speed;
                await api.post({url: `/quests/${quest.id}/video-progress`, body: {timestamp: Math.min(secondsNeeded, timestamp + Math.random())}});
                secondsDone = Math.min(secondsNeeded, timestamp);
                updatePython(questName, secondsDone, secondsNeeded, "running");
                await new Promise(r => setTimeout(r, 1000));
            }
            await api.post({url: `/quests/${quest.id}/video-progress`, body: {timestamp: secondsNeeded}});

        } else if(taskName === "PLAY_ON_DESKTOP") {
            await new Promise(resolve => {
                api.get({url: `/applications/public?application_ids=${applicationId}`}).then(res => {
                    const appData = res.body[0];
                    const exeName = appData.executables.find(x => x.os === "win32").name.replace(">","");
                    const fakeGame = {
                        cmdLine: `C:\\Program Files\\${appData.name}\\${exeName}`,
                        exeName, exePath: `c:/program files/${appData.name.toLowerCase()}/${exeName}`,
                        hidden: false, isLauncher: false, id: applicationId, name: appData.name,
                        pid: pid, pidPath: [pid], processName: appData.name, start: Date.now(),
                    };

                    const realGetRunningGames = RunningGameStore.getRunningGames;
                    const realGetGameForPID = RunningGameStore.getGameForPID;
                    RunningGameStore.getRunningGames = () => [fakeGame];
                    RunningGameStore.getGameForPID = (pid) => [fakeGame].find(x => x.pid === pid);
                    FluxDispatcher.dispatch({type: "RUNNING_GAMES_CHANGE", removed: [], added: [fakeGame], games: [fakeGame]});

                    let checkInterval = setInterval(() => {
                        secondsDone += 1; 
                        if (secondsDone > secondsNeeded) secondsDone = secondsNeeded;
                        updatePython(questName, secondsDone, secondsNeeded, "running");

                        if(secondsDone >= secondsNeeded) {
                            clearInterval(checkInterval);
                            RunningGameStore.getRunningGames = realGetRunningGames;
                            RunningGameStore.getGameForPID = realGetGameForPID;
                            FluxDispatcher.dispatch({type: "RUNNING_GAMES_CHANGE", removed: [fakeGame], added: [], games: []});
                            resolve();
                        }
                    }, 1000);
                });
            });

        } else if(taskName === "STREAM_ON_DESKTOP") {
             await new Promise(resolve => {
                let realFunc = ApplicationStreamingStore.getStreamerActiveStreamMetadata;
                ApplicationStreamingStore.getStreamerActiveStreamMetadata = () => ({ id: applicationId, pid, sourceName: null });

                let checkInterval = setInterval(() => {
                    secondsDone += 1;
                    if (secondsDone > secondsNeeded) secondsDone = secondsNeeded;
                    updatePython(questName, secondsDone, secondsNeeded, "running");

                    if(secondsDone >= secondsNeeded) {
                        clearInterval(checkInterval);
                        ApplicationStreamingStore.getStreamerActiveStreamMetadata = realFunc;
                        resolve();
                    }
                }, 1000);
            });
        }

        updatePython(questName, secondsNeeded, secondsNeeded, "finished_one");
        await new Promise(r => setTimeout(r, 1000));
    }

    window.questStatus.status = "completed_all";
    window.questStatus.current = window.questStatus.total; 
})();
"""


class QuestWorker(QThread):
    progress_signal = pyqtSignal(str, int, int)
    status_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()

    def get_discord_path(self):
        local_app_data = os.environ.get('LOCALAPPDATA')
        if not local_app_data: return None
        discord_path = Path(local_app_data) / "Discord"
        if not discord_path.exists(): return None
        for item in discord_path.iterdir():
            if item.is_dir() and item.name.startswith("app-"):
                exe = item / "Discord.exe"
                if exe.exists(): return str(exe)
        return None

    def run(self):
        discord_exe = self.get_discord_path()
        if not discord_exe:
            self.status_signal.emit("Discord not found")
            return

        self.status_signal.emit("Restarting Discord...")
        os.system("taskkill /f /im Discord.exe >nul 2>&1")
        time.sleep(1.5)

        subprocess.Popen([discord_exe, "--remote-debugging-port=9222", "--remote-allow-origins=*"])

        for i in range(15):
            self.status_signal.emit(f"Waiting for launch ({15 - i})...")
            time.sleep(1)

        try:
            self.status_signal.emit("Connecting...")
            resp = requests.get("http://127.0.0.1:9222/json").json()
            ws_url = next((t['webSocketDebuggerUrl'] for t in resp if
                           'discord' in t.get('url', '') or t.get('title') == 'Discord'), None)

            if not ws_url and len(resp) > 0: ws_url = resp[0]['webSocketDebuggerUrl']
            if not ws_url: raise Exception("Debug URL not found")

            ws = websocket.create_connection(ws_url)

            ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": JS_CODE, "includeCommandLineAPI": True, "returnByValue": True}
            }))
            ws.recv()

            self.status_signal.emit("Syncing...")

            while True:
                ws.send(json.dumps({
                    "id": 2, "method": "Runtime.evaluate",
                    "params": {"expression": "window.questStatus", "returnByValue": True}
                }))

                result_raw = json.loads(ws.recv())

                if 'result' in result_raw and 'result' in result_raw['result'] and 'value' in result_raw['result'][
                    'result']:
                    data = result_raw['result']['result']['value']
                    status = data.get('status')

                    if status == 'no_quest':
                        self.progress_signal.emit("No active quests", 0, 100)
                        self.status_signal.emit("No quests found")
                        break

                    if status == 'completed_all':
                        self.status_signal.emit("All quests finished!")
                        self.progress_signal.emit("Done", 100, 100)
                        self.finished_signal.emit()
                        break

                    name = data.get('name', 'Quest')
                    curr = data.get('current', 0)
                    total = data.get('total', 100)

                    self.progress_signal.emit(name, curr, total)
                    self.status_signal.emit(f"Running: {name}")

                time.sleep(0.1)

            ws.close()

        except Exception as e:
            self.status_signal.emit(f"Error: {str(e)}")


class ModernQuestUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 250)
        self.initUI()
        self.oldPos = self.pos()

    def initUI(self):
        self.container = QFrame(self)
        self.container.setGeometry(0, 0, 400, 250)
        self.container.setStyleSheet("""
            QFrame {
                background-color: #2b2d31;
                border-radius: 20px;
                border: 1px solid #1e1f22;
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)

        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        header_layout = QHBoxLayout()

        title = QLabel("Discord Quest Spoofer")
        title.setStyleSheet("color: #f2f3f5; font-family: 'Segoe UI'; font-size: 16px; font-weight: bold;")

        close_btn = QPushButton("×")
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        close_btn.setStyleSheet("""
            QPushButton { color: #b5bac1; background: transparent; font-size: 20px; font-weight: bold; border: none; }
            QPushButton:hover { color: #f2f3f5; }
        """)

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(close_btn)
        layout.addLayout(header_layout)

        self.quest_label = QLabel("Ready to start")
        self.quest_label.setStyleSheet(
            "color: #b5bac1; font-family: 'Segoe UI'; font-size: 13px; background: transparent;")
        self.quest_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.quest_label)

        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(12)
        self.pbar.setTextVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar {
                border-radius: 6px;
                background-color: #1e1f22;
                border: none;
            }
            QProgressBar::chunk {
                border-radius: 6px;
                background-color: #5865F2;
            }
        """)
        layout.addWidget(self.pbar)

        self.progress_text = QLabel("0:00 / 0:00")
        self.progress_text.setStyleSheet("color: #949ba4; font-size: 11px; background: transparent;")
        self.progress_text.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.progress_text)

        layout.addStretch()

        self.btn = QPushButton("Start Quests")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setFixedHeight(40)
        self.btn.clicked.connect(self.start_quest)
        self.btn.setStyleSheet("""
            QPushButton {
                background-color: #5865F2;
                color: white;
                border-radius: 10px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #4752c4; }
            QPushButton:pressed { background-color: #3c45a5; }
            QPushButton:disabled { 
                background-color: #313338; 
                color: #80848e; 
                border: 1px solid #404249;
            }
        """)
        layout.addWidget(self.btn)

    def start_quest(self):
        self.btn.setEnabled(False)
        self.btn.setText("Running...")
        self.worker = QuestWorker()
        self.worker.status_signal.connect(self.update_status)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def update_status(self, text):
        self.quest_label.setText(text)

    def update_progress(self, name, current, total):
        self.quest_label.setText(name)
        self.pbar.setMaximum(total)
        self.pbar.setValue(current)

        cur_min, cur_sec = divmod(current, 60)
        tot_min, tot_sec = divmod(total, 60)
        self.progress_text.setText(f"{cur_min}:{cur_sec:02d} / {tot_min}:{tot_sec:02d}")

    def on_finished(self):
        self.btn.setText("Completed")
        self.quest_label.setText("All tasks finished")
        self.pbar.setValue(self.pbar.maximum())

    def mousePressEvent(self, event):
        self.oldPos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        delta = QPoint(event.globalPosition().toPoint() - self.oldPos)
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self.oldPos = event.globalPosition().toPoint()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = ModernQuestUI()
    window.show()
    sys.exit(app.exec())