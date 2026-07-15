import os

import pytest

from src.database.session import _get_database_url, reset_engine


@pytest.fixture(autouse=True)
def _clean_engine():
    reset_engine()
    yield
    reset_engine()


@pytest.fixture
def _env_backup():
    old_app_env = os.environ.get("APP_ENV")
    old_db_url = os.environ.get("DATABASE_URL")
    yield
    if old_app_env is None:
        os.environ.pop("APP_ENV", None)
    else:
        os.environ["APP_ENV"] = old_app_env
    if old_db_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old_db_url


class TestProductionDatabaseEnforcement:
    def test_sqlite_allowed_in_development(self, _env_backup):
        os.environ["APP_ENV"] = "development"
        os.environ.pop("DATABASE_URL", None)
        url = _get_database_url()
        assert url.startswith("sqlite:///")

    def test_sqlite_rejected_in_production(self, _env_backup):
        os.environ["APP_ENV"] = "production"
        os.environ["DATABASE_URL"] = "sqlite:///test.db"
        with pytest.raises(RuntimeError, match="SQLite is not supported in production"):
            _get_database_url()

    def test_missing_database_url_rejected_in_production(self, _env_backup):
        os.environ["APP_ENV"] = "production"
        os.environ.pop("DATABASE_URL", None)
        with pytest.raises(RuntimeError, match="DATABASE_URL is required in production"):
            _get_database_url()

    def test_postgresql_url_accepted_in_production(self, _env_backup):
        os.environ["APP_ENV"] = "production"
        os.environ["DATABASE_URL"] = "postgresql://user:pass@host:5432/dbname"
        url = _get_database_url()
        assert url == "postgresql://user:pass@host:5432/dbname"

    def test_postgres_url_rewritten_in_production(self, _env_backup):
        os.environ["APP_ENV"] = "production"
        os.environ["DATABASE_URL"] = "postgres://user:pass@host:5432/dbname"
        url = _get_database_url()
        assert url.startswith("postgresql://")

    def test_default_app_env_is_development(self, _env_backup):
        os.environ.pop("APP_ENV", None)
        os.environ.pop("DATABASE_URL", None)
        url = _get_database_url()
        assert url.startswith("sqlite:///")
