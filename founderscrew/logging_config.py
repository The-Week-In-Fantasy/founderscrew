import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

CONFIG_DIR = Path.home() / ".founderscrew"

def setup_logging():
    """Sets up a robust, rotating file logger and console logger."""
    log_dir = CONFIG_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "founderscrew.log"
    
    # Define detailed format for file, simpler format for console
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    # Rotating File Handler: Max 5 MB per file, keep last 5 logs
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=5 * 1024 * 1024, 
        backupCount=5, 
        encoding="utf-8"
    )
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.INFO)
    
    # Console Handler for stdout/stderr
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)
    
    root_logger = logging.getLogger()
    # Avoid adding multiple handlers if called multiple times
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
    # Wire standard library loggers (like uvicorn and fastapi) to write to our log file too
    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"]:
        lib_logger = logging.getLogger(logger_name)
        # Check if file handler is already added
        has_file_handler = False
        for handler in lib_logger.handlers:
            if isinstance(handler, RotatingFileHandler):
                has_file_handler = True
                break
        if not has_file_handler:
            lib_logger.addHandler(file_handler)
