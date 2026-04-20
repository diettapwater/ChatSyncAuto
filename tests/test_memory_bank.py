def test_get_all_chunks_returns_indexed_chapters(mb):
    mb.index_chapter("Ragna", 1, "Ragna fought bravely at the northern fort.")
    mb.index_chapter("Ragna", 2, "Ragna later settled in the mountains.")
    chunks = mb.get_all_chunks("Ragna")
    assert len(chunks) == 2
    assert all("text" in c and "meta" in c and "id" in c for c in chunks)
    assert all(c["meta"]["npc"] == "Ragna" for c in chunks)


def test_get_all_chunks_empty_for_unknown_npc(mb):
    chunks = mb.get_all_chunks("UnknownNPC")
    assert chunks == []


def test_get_all_chunks_isolates_by_npc(mb):
    mb.index_chapter("Ragna", 1, "Ragna's chapter one content here.")
    mb.index_chapter("Aldric", 1, "Aldric's chapter one content here.")
    ragna_chunks = mb.get_all_chunks("Ragna")
    aldric_chunks = mb.get_all_chunks("Aldric")
    assert len(ragna_chunks) == 1
    assert len(aldric_chunks) == 1
    assert ragna_chunks[0]["meta"]["npc"] == "Ragna"
    assert aldric_chunks[0]["meta"]["npc"] == "Aldric"


def test_index_conversation_history_handles_string_turns(mb):
    """Regression: ConversationHistory can contain plain strings, not only dicts."""
    convo = [
        "Ragna: I remember the siege well.",
        {"Speaker": "Player", "Text": "Tell me more."},
        "Ragna: It cost us everything.",
    ]
    count = mb.index_conversation_history("Ragna", convo)
    assert count == 1   # 3 turns → 1 chunk of size 3
    chunks = mb.get_all_chunks("Ragna")
    assert len(chunks) == 1
    assert "Ragna: I remember the siege well." in chunks[0]["text"]
