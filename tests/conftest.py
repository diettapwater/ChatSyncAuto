import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture
def mb(tmp_path):
    """Fresh memory_bank with isolated temp ChromaDB for each test."""
    if "memory_bank" in sys.modules:
        del sys.modules["memory_bank"]
    import memory_bank as _mb
    _mb._DB_PATH = tmp_path / "db"
    _mb._db = None
    _mb._collection = None
    yield _mb
    if "memory_bank" in sys.modules:
        del sys.modules["memory_bank"]
