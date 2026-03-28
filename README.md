# ChatSyncAuto — v7.6 Stable Vault Edition

A desktop companion app and AI memory manager for the **AI Influence** mod for
**Mount & Blade II: Bannerlord**. Built in Python with CustomTkinter.

The AI Influence mod gives NPCs AI-driven dialogue written to per-character JSON files.
ChatSyncAuto watches those files in real-time and handles everything the mod itself
doesn't — scene awareness, memory compression, mail systems, and world editing.

---

## Features

### Scene Sync
When a player talks to an NPC, the mod writes that dialogue to the NPC's JSON file.
ChatSyncAuto mirrors new lines to every other NPC in the same scene — so nearby
characters stay aware of conversations happening around them.

- Real-time file monitoring via `watchdog`
- SHA1 deduplication prevents echo loops
- Settlement ID-based location grouping (auto-adds NPCs who share a location)
- Smart filtering: skips OOC player prompts (`(continue)` etc.) and letter events
- Mirror-NPC-only mode and dry run mode for testing

### NPC Profile Editor
A multi-tab editor for each NPC's JSON file:

| Tab | Contents |
|-----|----------|
| Easy Editor | Personality, backstory, speech quirks, known info, secrets |
| Dialogue Reader | Formatted script view of conversation history |
| Chat History | Direct editor for the conversation array |
| Raw JSON | Full file view with syntax validation |

All tabs support live syntax validation and multi-level undo.

### AI Saga Archiver
When an NPC's conversation history exceeds a configurable threshold, the app sends
the older messages to an LLM to generate a compressed third-person summary.

- The summary replaces archived messages as a `MEMORY ARCHIVE` entry in the JSON
- Summaries are also saved to a **Lore Library** as numbered chapters
- Keeps NPC context windows manageable while preserving narrative continuity
- Supports: Ollama (local), OpenAI, Groq, OpenRouter, Anthropic

### Mailbox System
Detects letter and messenger events in NPC dialogue via regex pattern matching:

- Filters `[MESSENGER]` / `[LETTER]` events out of the normal scene sync flow
- Archives them in a per-NPC mailbox with real-world timestamps
- Can scan existing campaign files to recover old letters retroactively

### World Events Vault
Monitors the mod's global JSON files for dynamic events and diplomatic statements:

- Vaults events into a persistent archive automatically
- Optional AI summarization into a **World Chronicle** lorebook chapter

### World Editor
Direct editor for global mod configuration files:

- `world.txt`, action rules, event generator rules, kingdom statement rules
- JSON validation and save/undo on all files

### Scene Management
Two-panel NPC manager:

- Add/remove characters to the active scene
- Search by name
- Scene presets — save and load named NPC groups
- Location-based auto-grouping by Settlement ID
- Auto-include the current interlocutor (whoever the player is talking to)

### Other
- Auto-copy NPC lines to clipboard for pasting into the game
- Configurable reply wait timers and reply timeout
- Appearance theming
- Multi-level file undo system
- C# mod cache clearing (Quick Rewind)

---

## Requirements

```
Python 3.10+
customtkinter
watchdog
```

LLM API key required only for the Saga Archiver feature (optional).
Supported backends: Ollama, OpenAI, Groq, OpenRouter, Anthropic.

---

## Setup

1. Install dependencies:
   ```bash
   pip install customtkinter watchdog
   ```

2. Run the app:
   ```bash
   python ChatSyncAuto.py
   ```

3. Point it at your AI Influence mod's `save_data` folder.
   The app will auto-detect common Steam and Game Pass install locations.

---

## Mod Compatibility

Designed for the **AI Influence** mod for Mount & Blade II: Bannerlord.
The mod writes NPC dialogue to JSON files in its `save_data` directory —
ChatSyncAuto manages those files.

---

## License

GPLv3 — see [LICENSE](LICENSE)
