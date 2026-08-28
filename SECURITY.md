# Security

## Reporting
Found a vulnerability? Open a GitHub issue or email albertalcance@gmail.com. This is a hackathon project running against Alpaca's **paper trading** environment only — no real funds are ever at risk from this codebase as published.

## Secrets policy
- No API keys, tokens, or credentials are ever committed. Secrets live in `.env` (gitignored); `.env.example` documents the required variables.
- Pre-commit runs [gitleaks](https://github.com/gitleaks/gitleaks) and private-key detection. GitHub secret scanning + push protection are enabled on the repo.
- If a secret ever leaks: rotate it at the provider immediately (Alpaca dashboard / Fireworks console). Rotation beats history rewriting.

## Runtime security model
- The public dashboard is **read-only** — it cannot place orders or reach controls.
- The kill switch and all controls are CLI/file-based on the host only.
- The supervisor process uses separate Alpaca credentials from the trader.
- Server: UFW allows 22/80/443 only; secrets injected via environment, never baked into images.
