# Setup — optional integrations

The pipeline runs and saves all artifacts to disk without any of this; these
steps only enable **push to GitHub** and **email delivery of the daily report**.

All secrets go in `config.json` (gitignored — never committed). Start from the
template:

```powershell
copy config.example.json config.json
```

---

## 1) GitHub repo + auto-push

1. Create a repo (private if the harvested PDFs should not be public), e.g. at
   <https://github.com/new>, or with the `gh` CLI: `gh repo create`.
2. Authenticate git to push without prompts — either GitHub Desktop / a
   credential manager, or a **fine-grained Personal Access Token**
   (<https://github.com/settings/tokens>, Contents: Read/Write). If you use a
   token file, keep it **outside the repo** and point at it with
   `github.token_file` in `config.json` or the `GITHUB_TOKEN_FILE` environment
   variable. The sync script only ever passes it as an in-memory argument.
3. Fill in `config.json`:
   ```json
   "github": { "remote_url": "https://github.com/<you>/AI_DS_ML_DL_Researcher.git",
               "branch": "main", "enable_push": true,
               "token_file": "C:/path/outside/repo/token.txt",
               "git_user_name": "<you>", "git_user_email": "you@example.com" }
   ```
4. First push:
   ```powershell
   powershell -File scripts\git_autopush.ps1
   ```
   After this, the pipeline pushes automatically at the end of each cycle.

> Optional: enable **GitHub Pages** (Settings → Pages → branch `main`, folder
> `/webapp`) to serve the generated site.

---

## 2) Daily email report

Pick **one** provider, fill it into `config.json → email`, set `"enabled": true`.

**Gmail:**
1. Enable 2-Step Verification on the Google account.
2. Create an **App Password**: <https://myaccount.google.com/apppasswords>.
3. Config:
   ```json
   "email": { "enabled": true, "provider": "gmail_smtp",
              "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "you@example.com", "app_password": "<16-char app password>",
              "from_addr": "you@example.com", "to_addr": "you@example.com" }
   ```

**SendGrid (alternative):** set `"provider": "sendgrid"` and
`"sendgrid_api_key": "<key>"`.

Test it:
```powershell
.\.venv\Scripts\python.exe scripts\send_report.py --dry-run
```
If credentials are missing it saves the report to
`state/daily/report-<date>.html` and exits cleanly — the pipeline never breaks
on email.

---

## Verify everything

```powershell
powershell -File scripts\check_gate.ps1                       # gate status
.\.venv\Scripts\python.exe scripts\fetch_papers.py --dry-run --top-k 5
.\.venv\Scripts\python.exe -m pytest tests -q                 # harness tests
```
