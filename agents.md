# Vibecoding Workflow Rules

For any given feature or change, you must strictly follow this workflow:

1. **study**: Analyze the request and write a Markdown file in `doc/study/` discussing the request, checking for feasibility and tradeoffs. Do not write implementation code yet.
2. **plan**: Write a checklist Markdown file in `doc/plan/` detailing the concrete steps needed to achieve the outcome based on the study.
3. **execute plan**: Take the existing plan doc and execute the steps. This should be done on a separate Git branch from main.
4. **rendezvous**: Merge the Git branch back to main and ensure the codebase is in a workable state.
5. **sync docs**: Ensure the "living" documentation in `doc/wiki/` is up-to-date with the current state of the codebase.

**Commit Guidelines:**
All code changes must be strictly scoped to conventional commits (e.g., `feat:`, `fix:`, `chore:`, `docs:`, `build:`).