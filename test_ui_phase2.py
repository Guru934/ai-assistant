from PyQt6.QtWidgets import QApplication
import sys
import threading
import time
from cat_talker.main import RadialVisualizerWindow

def kill_app():
    time.sleep(1)
    app = QApplication.instance()
    if app:
        app.quit()

app = QApplication(sys.argv)
win = RadialVisualizerWindow()
win.show()

# Simulate states
win._on_state('listening')
win.update()
win._on_state('thinking')
win.update()
win._on_state('talking')
win.update()

t = threading.Thread(target=kill_app)
t.start()
app.exec()
print("UI spawned and closed successfully.")
