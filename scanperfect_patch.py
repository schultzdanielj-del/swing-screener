# Patch script: apply Create New Setup dialog to scanperfect.py
# Run on Dan's desktop: python scanperfect_patch.py
import re

FILE = 'scanperfect.py'

with open(FILE, 'r', encoding='utf-8') as f:
    content = f.read()

# Edit 1: Add QDialog to imports
old_import = '''    QGridLayout, QLineEdit, QTextEdit, QSlider,
)'''
new_import = '''    QGridLayout, QLineEdit, QTextEdit, QSlider, QDialog,
)'''
assert old_import in content, 'Import block not found'
content = content.replace(old_import, new_import, 1)

# Edit 2: Add "+" button after setup combo
old_toolbar = '''        self._setup_combo = QComboBox()
        self._setup_combo.currentIndexChanged.connect(self._on_setup_changed)
        tl.addWidget(self._setup_combo)

        ml.addWidget(top)'''
new_toolbar = '''        self._setup_combo = QComboBox()
        self._setup_combo.currentIndexChanged.connect(self._on_setup_changed)
        tl.addWidget(self._setup_combo)

        add_setup_btn = QPushButton("+")
        add_setup_btn.setFixedSize(28, 28)
        add_setup_btn.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:1px solid %s; border-radius:4px; "
            "font-size:16px; font-weight:700; } "
            "QPushButton:hover { background:%s; }" % (C["surface"], C["white"], C["border"], C["border"])
        )
        add_setup_btn.setToolTip("Create new setup type")
        add_setup_btn.clicked.connect(self._add_setup_dialog)
        tl.addWidget(add_setup_btn)

        ml.addWidget(top)'''
assert old_toolbar in content, 'Toolbar block not found'
content = content.replace(old_toolbar, new_toolbar, 1)

# Edit 3: Add _add_setup_dialog method after _on_setup_changed
old_after = '''    def _on_setup_changed(self):
        data = self._setup_combo.currentData()
        if data:
            self._setup_type = data
            self._pipeline.set_setup(data)
            self._pipeline.refresh()

    def _on_tick(self):'''
new_after = '''    def _on_setup_changed(self):
        data = self._setup_combo.currentData()
        if data:
            self._setup_type = data
            self._pipeline.set_setup(data)
            self._pipeline.refresh()

    def _add_setup_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Create New Setup")
        dlg.setFixedSize(360, 220)
        dlg.setStyleSheet("QDialog { background:%s; } QLabel { color:%s; font-size:12px; } "
                          "QLineEdit, QComboBox { background:%s; color:%s; border:1px solid %s; "
                          "border-radius:4px; padding:6px; font-size:12px; } "
                          "QPushButton { background:%s; color:%s; border:1px solid %s; "
                          "border-radius:4px; padding:8px 20px; font-weight:600; } "
                          "QPushButton:hover { background:%s; }"
                          % (C["surface"], C["white"], C["bg"], C["white"], C["border"],
                             C["surface"], C["white"], C["border"], C["border"]))
        lay = QVBoxLayout(dlg)
        lay.setSpacing(10)
        lay.setContentsMargins(20, 20, 20, 20)

        lay.addWidget(QLabel("Setup Code (e.g. brk, unr, para-s):"))
        code_inp = QLineEdit()
        code_inp.setPlaceholderText("lowercase, short identifier")
        lay.addWidget(code_inp)

        lay.addWidget(QLabel("Display Name (e.g. Consolidated Breakout):"))
        name_inp = QLineEdit()
        name_inp.setPlaceholderText("human-readable name")
        lay.addWidget(name_inp)

        lay.addWidget(QLabel("Direction:"))
        dir_combo = QComboBox()
        dir_combo.addItem("Long", "long")
        dir_combo.addItem("Short", "short")
        lay.addWidget(dir_combo)

        msg = QLabel("")
        msg.setStyleSheet("color:%s; font-size:11px;" % C["red"])
        lay.addWidget(msg)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        create_btn = QPushButton("Create")
        btn_row.addWidget(create_btn)
        lay.addLayout(btn_row)

        def _do_create():
            code = code_inp.text().strip().lower().replace(" ", "-")
            name = name_inp.text().strip()
            direction = dir_combo.currentData()
            if not code:
                msg.setText("Setup code is required"); return
            if not name:
                msg.setText("Display name is required"); return
            if len(code) > 20:
                msg.setText("Code too long (max 20 chars)"); return
            try:
                with get_db() as db:
                    if db.execute("SELECT 1 FROM setups WHERE setup_type=?", (code,)).fetchone():
                        msg.setText("Setup \'%s\' already exists" % code); return
                    db.execute("INSERT INTO setups (setup_type, name, description, direction) VALUES (?,?,?,?)",
                               (code, name, "", direction))
                self._load_setups()
                for i in range(self._setup_combo.count()):
                    if self._setup_combo.itemData(i) == code:
                        self._setup_combo.setCurrentIndex(i)
                        break
                dlg.accept()
            except Exception as e:
                msg.setText("Error: %s" % str(e))

        create_btn.clicked.connect(_do_create)
        dlg.exec()

    def _on_tick(self):'''
assert old_after in content, 'on_setup_changed block not found'
content = content.replace(old_after, new_after, 1)

with open(FILE, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done! 3 edits applied to scanperfect.py')
print('  1. Added QDialog import')
print('  2. Added + button next to setup combo')
print('  3. Added _add_setup_dialog method')
