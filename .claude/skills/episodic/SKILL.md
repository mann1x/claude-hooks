---
name: episodic
description: Search past Claude Code conversations using episodic-memory. Works from any host by querying the remote episodic-server API. Use when the user asks 'I did this before', 'how did I fix X', 'find the session where', 'search my history', or references past work.
---

# /episodic -- Search Past Conversations

Search across all indexed Claude Code sessions using semantic search.

## Instructions

Read the claude-hooks config to find the episodic server URL:

```bash
python3 -c "
import json, os
cfg_paths = [
    os.path.expanduser('~/.claude/claude-hooks.json'),
    'config/claude-hooks.json',
]
for p in cfg_paths:
    if os.path.exists(p):
        with open(p) as f:
            cfg = json.load(f)
        url = cfg.get('episodic', {}).get('server_url', 'http://192.168.178.2:11435')
        print(f'URL: {url}')
        break
else:
    print('URL: http://192.168.178.2:11435')
"
```

Then search using curl:

```bash
curl -s "http://SERVER:11435/search?q=QUERY&limit=10" | python3 -m json.tool
```

Replace SERVER with the episodic server URL from config, and QUERY with the user's search terms (URL-encoded).

Present results to the user showing:
- The match quote/context
- Which project/date it came from
- The file location if they want to dig deeper

## When to use

- When the user says "I did this before", "how did I fix X last time"
- When looking for a past decision, fix, or approach
- When the user references work from a previous session
- When context from a prior conversation would help the current task
