"""Logging configuration for Cat Talker."""

import logging
import sys
import os
from typing import Optional


def setup_logging(level: Optional[str] = None, log_file: Optional[str] = None) -> logging.Logger:
    """Set up structured logging for Cat Talker.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to INFO or LOG_LEVEL env var.
        log_file: Optional path to log file. If None, logs to console only.
    
    Returns:
        Configured logger instance.
    """
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()
    
    numeric_level = getattr(logging, level, logging.INFO)
    
    # Create logger
    logger = logging.getLogger("cat_talker")
    logger.setLevel(numeric_level)
    logger.propagate = False
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str = "cat_talker") -> logging.Logger:
    """Get logger instance for a module."""
    return logging.getLogger(name)


# Default logger instance
logger = setup_logging()