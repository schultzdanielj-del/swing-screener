"""
ScanPerfect — Native Desktop App (PySide6)
Phase 6 of localization. Replaces browser-based HTML UI entirely.
Reads directly from SQLite + 5yr OHLCV pickle. No server process needed.
"""

import json
import math
import os
import pickle
import sys
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import pandas as pd

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QScrollArea,
    QPlainTextEdit, QFrame, QCheckBox, QSizePolicy,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QGridLayout, QLineEdit, QTextEdit, QSlider, QDialog,
)
from PySide6.QtCore import Qt, QProcess, QTimer, Signal, QProcessEnvironment, QRectF, QPointF
from PySide6.QtGui import QFont, QFontDatabase, QColor, QPainter, QPen, QLinearGradient, QBrush, QPainterPath