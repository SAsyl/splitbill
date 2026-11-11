# Agent notes

## Backlog

- Planned features and bugs live in **GitHub Issues**: https://github.com/SAsyl/splitbill/issues
- Before starting work, check open issues. When a new feature or idea comes up, create an issue instead of keeping it in chat or in code comments.
- Reference the issue in the commit or PR (`Closes #N`) so it closes on merge to `main`.

## Layout

- `src/` code and UI, `tests/` end-to-end check, `assets/` logos, `deploy/` systemd unit. See `README.md`.
- Run the check: `python tests/test_app.py` (needs `pip install -r requirements.txt`).

## Rules

- Single branch `main`; use short `feat/...` branches for larger changes.
- Never commit `.env`, `bills.db`, or any tokens. The repo is public.
