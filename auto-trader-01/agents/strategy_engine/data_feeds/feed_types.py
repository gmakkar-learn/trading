from enum import Enum, auto


class DataFeedType(Enum):
    ANNOUNCEMENTS = auto()
    OHLCV_CANDLES = auto()
    TICKS = auto()
