"""et_suite — Suite de Eye-Tracking y análisis de mirada en tiempo real."""

from .filters import OneEuroFilter, OneEuroFilter2D
from .camera import CameraInfo, auto_open, open_camera, probe_cameras
from .gaze_tracker import GazeTracker, GazeSample, GazeCalibrator
from .pupillometry import PupilTracker, IVTClassifier, IDTClassifier, AOI
from .calibration_engine import CalibrationEngine
from .data_logger import DataLogger
from .experiment_server import ExperimentServer
from .heatmap_generator import HeatmapGenerator
from .report import session_report

__all__ = [
    "OneEuroFilter", "OneEuroFilter2D",
    "CameraInfo", "auto_open", "open_camera", "probe_cameras",
    "GazeTracker", "GazeSample", "GazeCalibrator",
    "PupilTracker", "IVTClassifier", "IDTClassifier", "AOI",
    "CalibrationEngine",
    "DataLogger",
    "ExperimentServer",
    "HeatmapGenerator",
    "session_report",
]
__version__ = "2.1.0"
