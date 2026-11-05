# Read automatically by gunicorn from the working directory.
# One worker only: the score state (and its `seq` counter) lives in memory, so
# several workers would each hand the ESP32 a different copy.
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1
threads = 4
preload_app = False
