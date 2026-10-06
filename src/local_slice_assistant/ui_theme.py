"""Shared visual hierarchy for the desktop workflow and its dialogs."""
APP_STYLE = """
QWidget { font-family: 'Microsoft YaHei UI'; font-size: 14px; color: #24324b; }
QMainWindow, QDialog { background: #f4f6fb; }
QWidget#workspaceSurface { background: #f8fafe; }
QLabel { background: transparent; }
QLabel#projectTitle { font-size: 25px; font-weight: 700; color: #17243d; }
QLabel#nextTitle, QLabel[role="title"] { font-size: 21px; font-weight: 700; color: #17243d; }
QLabel[role="section"] { font-size: 16px; font-weight: 700; color: #24324b; }
QLabel[role="muted"], QLabel#saveState { font-size: 13px; color: #66758c; }
QLabel[role="summary"] { font-size: 16px; font-weight: 700; color: #4d3da1; background: #eeeafa; border-radius: 8px; padding: 10px 12px; }
QLabel[role="warning"] { font-size: 13px; color: #885710; background: #fff4db; border: 1px solid #ead5a5; border-radius: 8px; padding: 10px 12px; }
QLabel[role="status"] { font-size: 14px; font-weight: 600; color: #405572; padding: 6px 0; }
QGroupBox { background: white; border: 1px solid #dce3ed; border-radius: 10px; margin-top: 16px; padding: 16px 12px 12px; font-size: 16px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #253653; }
QGroupBox#workflowSteps { background: #edf1f9; border: 0; }
QTabWidget::pane { border: 1px solid #dce3ed; background: #f8fafe; border-radius: 8px; }
QTabBar::tab { color: #64738a; padding: 12px 20px; background: #e9edf5; border: 0; font-size: 15px; }
QTabBar::tab:selected { background: #f8fafe; color: #5145a5; font-weight: 700; border-bottom: 3px solid #6754bf; }
QPushButton { background: white; border: 1px solid #d8dfeb; border-radius: 7px; padding: 8px 12px; font-weight: 500; }
QPushButton:hover { background: #eef1fb; border-color: #b9b1d9; }
QPushButton:pressed { background: #e5e0f7; }
QPushButton:focus { border: 2px solid #7562c5; }
QPushButton:disabled { color: #8b96a6; background: #f1f3f7; border-color: #e1e6ee; }
QPushButton[primary="true"] { background: #5947ad; color: white; border-color: #5947ad; padding: 10px 16px; font-weight: 700; }
QPushButton[primary="true"]:hover { background: #493692; }
QPushButton[primary="true"]:disabled { color: #8b86a0; background: #e4e0ed; border-color: #e4e0ed; }
QPushButton[danger="true"] { color: #a63f46; }
QPushButton[workflowStep="true"] { text-align: left; padding: 12px; background: transparent; border: 1px solid transparent; }
QPushButton[workflowStep="true"]:checked { background: white; color: #5145a5; border: 2px solid #8170cb; font-weight: 700; }
QPushButton[workflowStep="true"]:disabled { color: #939eaf; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #d8dfeb; border-radius: 6px; padding: 6px 8px; min-height: 22px; }
QPlainTextEdit, QTextEdit { background: white; border: 1px solid #d8dfeb; border-radius: 7px; padding: 8px; }
QTableWidget { background: white; alternate-background-color: #f5f7fc; gridline-color: #e7ebf3; border: 1px solid #dce3ed; border-radius: 7px; selection-background-color: #e7e1fc; selection-color: #31245d; }
QHeaderView::section { background: #eef2f8; color: #40516d; border: 0; border-bottom: 1px solid #dce3ed; padding: 9px 10px; font-weight: 700; }
QMenuBar { background: #f4f6fb; color: #57667e; }
QMenuBar::item { padding: 7px 12px; }
QStatusBar { background: #eaf0f8; color: #43546e; }
QScrollArea { border: 0; background: transparent; }
"""


def label_role(label, role):
    label.setProperty('role', role)
    label.setWordWrap(True)
    return label
