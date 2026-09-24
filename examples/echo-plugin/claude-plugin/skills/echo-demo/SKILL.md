---
name: echo-demo
description: Demo skill from the example plugin. Only reachable while the `echo` plugin is enabled in the Plugins tab — use it to confirm that plugin enablement really does gate skill advertisement.
---

# Echo demo

This skill exists to prove one thing: a plugin's skills are advertised to
spawned Claude Code sessions only while that plugin is **enabled** in
`workspace/plugins.yaml`.

If you can see this skill in your available-skills listing, the `echo` plugin is
enabled. Disable it in the Plugins tab, restart the daemon, and it disappears.

When invoked, just say so — there is nothing else to do.
