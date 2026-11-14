import os
import subprocess
from pathlib import Path


def test_clean_database_migration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, APP_ENV='test',
               DATABASE_URL=f'sqlite:///{tmp_path / "migration.db"}',
               AUTO_CREATE_TABLES='false')
    run = subprocess.run(['alembic', 'upgrade', 'head'], cwd=root, env=env,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    verify = subprocess.run(['alembic', 'check'], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert verify.returncode == 0, verify.stderr
