"""Logger setup for the project."""

import logging

def setup_logger(name, level=logging.INFO):
    """Sets up a logger with the specified name and level."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(level)

    # Formatter
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    ch.setFormatter(formatter)

    # Add handler to logger
    if not logger.handlers:
        logger.addHandler(ch)

    return logger
