"""
memory_bank.py — ChromaDB-backed NPC memory store for ChatSyncAuto.

Indexes NPC saga chapters as embeddings and retrieves the most relevant
ones at conversation-start time for injection into CharacterDescription.

Requires: chromadb  (pip install chromadb)
Uses chromadb's built-in default embedding (all-MiniLM-L6-v2 via sentence-transformers).
No API key needed — fully local.
"""

from pathlib import Path
import json
import re

_db         = None
_collection = None
_DB_PATH    = Path(__file__).parent / "memory_bank_db"

_COLLECTION_NAME = "npc_memories"


def _get_collection():
    global _db, _collection
    if _collection is not None:
        return _collection
    try:
        import chromadb
        _db = chromadb.PersistentClient(path=str(_DB_PATH))
        _collection = _db.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        return _collection
    except Exception as e:
        print(f"[MemoryBank] ChromaDB unavailable: {e}")
        return None


def is_available() -> bool:
    """Return True if ChromaDB is installed and the collection is accessible."""
    return _get_collection() is not None


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def index_chapter(npc_name: str, chapter_num: int, chapter_text: str) -> bool:
    """Upsert a single saga chapter into the memory store."""
    col = _get_collection()
    if col is None:
        return False
    try:
        doc_id = f"{npc_name}__ch{chapter_num}"
        col.upsert(
            ids=[doc_id],
            documents=[chapter_text],
            metadatas=[{"npc": npc_name, "chapter": chapter_num}],
        )
        return True
    except Exception as e:
        print(f"[MemoryBank] index_chapter error ({npc_name} ch{chapter_num}): {e}")
        return False


def index_npc_saga(npc_name: str, saga_path: Path) -> int:
    """Index all chapters from a ChatSyncSagas JSON file. Returns count indexed."""
    try:
        data = json.loads(saga_path.read_text(encoding="utf-8"))
        chapters = data.get("chapters", [])
        count = 0
        for ch in chapters:
            if index_chapter(npc_name, ch["chapter"], ch["content"]):
                count += 1
        return count
    except Exception as e:
        print(f"[MemoryBank] index_npc_saga error ({npc_name}): {e}")
        return 0


def index_conversation_history(npc_name: str, convo_array: list, chunk_size: int = 3) -> int:
    """
    Index raw ConversationHistory turns from an NPC JSON directly — no LLM needed.
    Groups turns into chunks of `chunk_size` and upserts into the memory store.
    Chunk IDs are content-hashed so re-indexing the same text is a safe no-op.
    Returns count of chunks indexed.
    """
    col = _get_collection()
    if col is None or not convo_array:
        return 0

    import hashlib
    import re as _re

    def _turn_text(t) -> str:
        if isinstance(t, dict):
            return t.get("Text") or ""
        return str(t)

    def _turn_speaker_text(t):
        if isinstance(t, dict):
            return t.get("Speaker", "?"), (t.get("Text") or "").strip()
        raw = str(t).strip()
        m = _re.match(r"^\s*([^:\n\r]{1,64})\s*:\s*(.*)", raw, _re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "?", raw

    # Strip MEMORY ARCHIVE entries — they're already summarized elsewhere
    turns = [
        t for t in convo_array
        if not _turn_text(t).startswith("MEMORY ARCHIVE")
    ]

    count = 0
    for i in range(0, len(turns), chunk_size):
        group = turns[i : i + chunk_size]
        lines = []
        for turn in group:
            speaker, text = _turn_speaker_text(turn)
            if text:
                lines.append(f"{speaker}: {text}")
        if not lines:
            continue
        content = "\n".join(lines)
        if len(content) < 30:
            continue
        doc_id = (
            f"{npc_name}__raw__{hashlib.sha256(content.encode()).hexdigest()[:24]}"
        )
        try:
            col.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[{"npc": npc_name, "chapter": 0, "raw": 1}],
            )
            count += 1
        except Exception as e:
            print(
                f"[MemoryBank] index_conversation_history error"
                f" ({npc_name} chunk {i}): {e}"
            )
    return count


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def query(npc_name: str, context_text: str, n: int = 3) -> list:
    """
    Retrieve top-N relevant chapter texts for this NPC given the context string.
    Returns a list of chapter text strings (may be shorter than n if fewer exist).
    """
    col = _get_collection()
    if col is None:
        return []
    try:
        # Don't request more results than exist for this NPC
        total = count_npc(npc_name)
        if total == 0:
            return []
        n_req = min(n, total)
        results = col.query(
            query_texts=[context_text],
            n_results=n_req,
            where={"npc": npc_name},
        )
        return results.get("documents", [[]])[0]
    except Exception as e:
        print(f"[MemoryBank] query error ({npc_name}): {e}")
        return []


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def count_npc(npc_name: str) -> int:
    """Return the number of indexed chapters for this NPC."""
    col = _get_collection()
    if col is None:
        return 0
    try:
        result = col.get(where={"npc": npc_name})
        return len(result.get("ids", []))
    except Exception:
        return 0


def get_all_chunks(npc_name: str) -> list:
    """Return all stored chunks for npc_name as list of {id, text, meta} dicts."""
    col = _get_collection()
    if col is None:
        return []
    try:
        result = col.get(
            where={"npc": npc_name},
            include=["documents", "metadatas"],
        )
        return [
            {"id": id_, "text": doc, "meta": meta}
            for id_, doc, meta in zip(
                result.get("ids", []),
                result.get("documents", []),
                result.get("metadatas", []),
            )
        ]
    except Exception as e:
        print(f"[MemoryBank] get_all_chunks error ({npc_name}): {e}")
        return []


def list_npcs() -> list:
    """Return list of NPC names that have indexed memories."""
    col = _get_collection()
    if col is None:
        return []
    try:
        result = col.get()
        names = set()
        for m in result.get("metadatas", []):
            if m and "npc" in m:
                names.add(m["npc"])
        return sorted(names)
    except Exception:
        return []


def clear_npc(npc_name: str) -> bool:
    """Delete all memories for a specific NPC."""
    col = _get_collection()
    if col is None:
        return False
    try:
        col.delete(where={"npc": npc_name})
        return True
    except Exception as e:
        print(f"[MemoryBank] clear_npc error ({npc_name}): {e}")
        return False


def clear_all() -> bool:
    """Wipe the entire memory store."""
    global _db, _collection
    col = _get_collection()
    if col is None:
        return False
    try:
        _db.delete_collection(_COLLECTION_NAME)
        _collection = None
        return True
    except Exception as e:
        print(f"[MemoryBank] clear_all error: {e}")
        return False
