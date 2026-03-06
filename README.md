# ⚔️ ChatSyncAuto - The AI Influence Companion
![Version](https://img.shields.io/badge/version-v6.5-blue)
![License](https://img.shields.io/badge/license-GPLv3-green)

ChatSyncAuto is a powerful, standalone companion application designed for the *Mount & Blade II: Bannerlord* **AI Influence** mod. It acts as an automated memory manager, scene director, and global world editor, preventing your save files from bloating while keeping your AI NPCs perfectly in character.

## ✨ Key Features
* **🤖 Auto Scene Sync:** Silently mirrors conversations to everyone in your immediate vicinity (reading exact Settlement IDs) for seamless group roleplay.
* **📚 Infinite Saga Archiver:** Automatically tracks message limits and uses AI to condense old chat history into dense, 3rd-person "Lorebook" chapters. NPCs never forget their past!
* **📬 Smart Mailbox:** Automatically detects courier `[MESSENGER]` and `[LETTER]` events, prevents them from broadcasting to the room, and saves them to a dedicated Mailbox tab with real-world timestamps.
* **🎬 Director Mode Filter:** Automatically hides Out-of-Character prompts like `(continue)` from syncing to other NPCs.
* **⏪ Quick Rewind:** Instantly undo bad AI generations and wipe internal C# mod caches with a single click.
* **🌍 Global World Editor:** Edit `world.txt`, `cultural_traditions.json`, and other global rulesets in a safe, syntax-validated environment. 

## 📥 Installation & Usage
1. Go to the **[Releases](../../releases)** section on the right side of this page.
2. Download the latest `ChatSyncAuto.exe`.
3. Run the application alongside Bannerlord.
4. Click **Browse save_data...** and target your `Modules/AIInfluence/save_data` folder.

## ⚠️ Important Note on Bannerlord Memory
Because Bannerlord stores chat history in your PC's RAM while playing, any changes made in this editor (like Quick Rewinds, Summaries, or World Edits) require you to **Save and Exit to the Main Menu**, then Reload your save to force the game to read the updated files. Do not click "Speak" before reloading!
