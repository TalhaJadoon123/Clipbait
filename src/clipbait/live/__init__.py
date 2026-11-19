"""Live clipping: chat sources, spike detection, rolling recording."""

from .chat import ChatSource, DemoChatSource, JsonlChatSource, TwitchChatSource, YouTubeChatSource
from .orchestrator import active_monitors, start, status, stop, stop_all
from .recorder import Recorder, build_recorder, select_segments
from .spikes import MomentCandidate, SpikeConfig, SpikeDetector

__all__ = [
    "ChatSource",
    "DemoChatSource",
    "JsonlChatSource",
    "MomentCandidate",
    "Recorder",
    "SpikeConfig",
    "SpikeDetector",
    "TwitchChatSource",
    "YouTubeChatSource",
    "active_monitors",
    "build_recorder",
    "select_segments",
    "start",
    "status",
    "stop",
    "stop_all",
]