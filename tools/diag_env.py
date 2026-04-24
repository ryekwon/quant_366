import sys
import os

print(f"Python: {sys.version}")
print(f"Path: {sys.path}")

try:
    import pandas as pd
    print(f"Pandas: {pd.__version__}")
except ImportError as e:
    print(f"Pandas missing: {e}")

try:
    import psutil
    print(f"Psutil: {psutil.__version__}")
except ImportError as e:
    print(f"Psutil missing: {e}")

try:
    from xtquant import xtdata
    print("xtquant: Available")
except ImportError as e:
    print(f"xtquant missing: {e}")

try:
    import yaml
    print(f"PyYAML: {yaml.__version__}")
except ImportError as e:
    print(f"PyYAML missing: {e}")

try:
    import requests
    print(f"Requests: {requests.__version__}")
except ImportError as e:
    print(f"Requests missing: {e}")
