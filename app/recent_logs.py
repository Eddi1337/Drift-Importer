"""Bounded application log view without filesystem writes."""
import logging
from collections import deque


class RecentLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=1000)

    def emit(self, record):
        self.lines.append({"level": record.levelname, "message": self.format(record)})

    def read(self, limit, minimum):
        self.acquire()
        try:
            return [line.copy() for line in self.lines
                    if logging.getLevelNamesMapping().get(line["level"], 20) >= minimum][-limit:]
        finally:
            self.release()


recent_logs = RecentLogs()
