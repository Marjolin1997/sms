"""Healthcheck i worker-it: dështon nëse cikli nuk ka lëvizur brenda MAX_AGE sekondash.
python -m scripts.worker_health [max_age_seconds]"""

import os
import sys
import time
from pathlib import Path

path = Path(os.environ.get("SMS_WORKER_HEARTBEAT", "/tmp/sms-worker-alive"))  # nosec B108: heartbeat pe /tmp (tmpfs i kontejnerit)
max_age = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
try:
    age = time.time() - path.stat().st_mtime
except OSError:
    sys.exit("worker has not started")
sys.exit(0 if age <= max_age else f"worker stalled ({age:.0f}s)")
