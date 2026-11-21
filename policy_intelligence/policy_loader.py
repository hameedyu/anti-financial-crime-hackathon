from pathlib import Path

def load_policy(path):
    return Path(path).read_text(encoding="utf-8")