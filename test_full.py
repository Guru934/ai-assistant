import sys
from cat_talker.db import init_db, purge_empty_sessions
init_db()
purge_empty_sessions()

from PyQt6.QtWidgets import QApplication
from cat_talker.ui_scratchpad import ScratchpadWindow

app = QApplication(sys.argv)
w = ScratchpadWindow()
print("Success")
