import os
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from founderscrew.logging_config import setup_logging

def test_setup_logging(tmp_path, monkeypatch):
    # Mock CONFIG_DIR to use our temp path so we don't mess with real logs
    monkeypatch.setattr("founderscrew.logging_config.CONFIG_DIR", tmp_path)
    
    # Reset root logger handlers for a clean test run
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers = []
    
    try:
        setup_logging()
        
        # Verify handlers were registered
        assert len(root.handlers) >= 2
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(file_handlers) == 1
        
        # Check that target log directory and file path were structured
        expected_log_file = tmp_path / "logs" / "founderscrew.log"
        assert expected_log_file.parent.exists()
        
        # Write test log output
        test_logger = logging.getLogger("founderscrew.test_logging_spec")
        test_logger.info("Verification of rotating log output.")
        
        # Verify log file was written and contains correct content
        assert expected_log_file.exists()
        with open(expected_log_file, "r", encoding="utf-8") as f:
            content = f.read()
            assert "Verification of rotating log output." in content
            assert "INFO" in content
            assert "test_logging" in content
            
    finally:
        # Restore original root handlers
        root.handlers = original_handlers

def test_setup_logging_filters_litellm_worker_noise(tmp_path, monkeypatch):
    monkeypatch.setattr("founderscrew.logging_config.CONFIG_DIR", tmp_path)

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers = []
    litellm_logger = logging.getLogger("LiteLLM")
    original_filters = litellm_logger.filters[:]
    litellm_logger.filters = []

    try:
        setup_logging()
        assert any(
            filt.__class__.__name__ == "_LiteLLMLoggingWorkerNoiseFilter"
            for filt in litellm_logger.filters
        )
    finally:
        root.handlers = original_handlers
        litellm_logger.filters = original_filters

def test_setup_logging_filters_adk_secondary_noise(tmp_path, monkeypatch):
    monkeypatch.setattr("founderscrew.logging_config.CONFIG_DIR", tmp_path)

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    root.handlers = []
    runner_logger = logging.getLogger("google_adk.google.adk.runners")
    otel_logger = logging.getLogger("opentelemetry.context")
    original_runner_filters = runner_logger.filters[:]
    original_otel_filters = otel_logger.filters[:]
    runner_logger.filters = []
    otel_logger.filters = []

    try:
        setup_logging()
        assert any(
            filt.__class__.__name__ == "_ADKSecondaryNoiseFilter"
            for filt in runner_logger.filters
        )
        assert any(
            filt.__class__.__name__ == "_ADKSecondaryNoiseFilter"
            for filt in otel_logger.filters
        )
    finally:
        root.handlers = original_handlers
        runner_logger.filters = original_runner_filters
        otel_logger.filters = original_otel_filters
