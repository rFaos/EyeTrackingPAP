"""et_suite — Suite de Eye-Tracking y análisis de mirada en tiempo real."""

from .filters import OneEuroFilter, OneEuroFilter2D
from .camera import CameraInfo, auto_open, open_camera, probe_cameras
from .gaze_tracker import GazeTracker, GazeSample, GazeCalibrator
from .pupillometry import (PupilTracker, BlinkDetector, IVTClassifier,
                           IDTClassifier, AOI)
from .calibration_engine import CalibrationEngine
from .validation_engine import ValidationEngine
from .data_logger import DataLogger
from .experiment_server import ExperimentServer
from .web_bridge import WebBridge
from .study import Study, StudyError, load_study, scan_studies
from .metrics import Analysis, analyze_csv, analyze_session, load_session
from .heatmap_generator import HeatmapGenerator
from .group_analysis import analyze_study
from .report import session_report

__all__ = [
    "OneEuroFilter", "OneEuroFilter2D",
    "CameraInfo", "auto_open", "open_camera", "probe_cameras",
    "GazeTracker", "GazeSample", "GazeCalibrator",
    "PupilTracker", "BlinkDetector", "IVTClassifier", "IDTClassifier", "AOI",
    "CalibrationEngine", "ValidationEngine",
    "DataLogger",
    "ExperimentServer", "WebBridge",
    "Study", "StudyError", "load_study", "scan_studies",
    "Analysis", "analyze_csv", "analyze_session", "load_session",
    "HeatmapGenerator", "analyze_study",
    "session_report",
]
__version__ = "3.0.0"
