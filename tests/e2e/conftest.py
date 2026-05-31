# -*- coding: utf-8 -*-
"""Pytest fixtures for E2E tests."""

import pytest
from pathlib import Path
from tests.e2e.db_setup import setup_test_db, cleanup_test_db


@pytest.fixture(scope="session")
def test_db():
    """会话级别的测试数据库。"""
    db_path = setup_test_db()
    yield db_path
    # 不自动清理，保留用于排查
