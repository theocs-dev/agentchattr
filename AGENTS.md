# AgentChattr Agent Notes

## Project Summary

AgentChattr is a local chat server for coordinating humans and AI coding
agents in shared channels. It provides a Python server, a browser UI, terminal
wrappers for agent CLIs, and MCP tools so agents can read and write the chat
without manual copy-paste.

The main surfaces are:

- `app.py`, `store.py`, `registry.py`, `router.py`: server, state, routing.
- `wrapper*.py`, `mcp_*.py`: terminal wrappers and MCP integration.
- `static/`: browser UI.
- `tests/`: regression tests.
- `UPDATE_MODEL_PROFILES.md`: required checklist for model, effort, profile,
  or Claude fast-mode changes.

## Workflow Rules

- Treat the worktree as shared. Run `git status -sb` before edits and do not
  overwrite user changes.
- In Theo's workspace, `origin` is the upstream repository and `theo` is the
  writable fork. Use `theo/main` as the main branch for this fork unless the
  user explicitly asks to work against upstream.
- For each new code change, start from the latest fork main and create a fresh
  branch:

```bash
git fetch --prune --all
git switch -c <short-topic-branch> theo/main
```

- Do not keep adding unrelated work to an already-merged feature branch. After
  a merge, start the next task from updated main or a new branch based on it.
- Keep commits focused. Push the branch to `theo` and open a PR against
  `theo/main` when the user wants the change published.
- Merge only after the user has approved the result, relevant tests/checks have
  passed or their absence is clearly reported, and there are no known blockers.

## Change Discipline

- Follow existing patterns before adding new abstractions.
- Keep UI, server behavior, terminal wrapper behavior, commands, help text,
  docs, and tests in sync when a feature crosses those boundaries.
- For model/profile/default-effort changes, follow `UPDATE_MODEL_PROFILES.md`
  end to end. That checklist exists because persisted settings and running
  wrappers can mask config changes.
