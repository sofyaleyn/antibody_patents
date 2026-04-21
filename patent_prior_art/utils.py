import logging
from datetime import datetime
from pathlib import Path


def setup_file_logging(output_dir: Path, label: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger(__name__).info(f"Logging to {log_path}")
