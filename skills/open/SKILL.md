---
name: open
description: Open the Surya Memory app to see, check, correct and talk to memory, and review what AI tools changed. Use when the user asks to see, browse, open or review their memory.
---

# Open the memory app

Run this in the background, so it doesn't hold up the session:

```bash
nohup python3 "${CLAUDE_PLUGIN_ROOT}/scripts/surya-memory" app >/dev/null 2>&1 &
```

It opens in the browser on this machine only, and stops by itself about 15 minutes after the tab is closed.

Tell the user in one sentence that it's open, and that **Changes** shows what each AI tool changed, with undo.
