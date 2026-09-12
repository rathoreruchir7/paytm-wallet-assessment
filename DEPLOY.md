# Publish and capture assessment evidence

No deployment URL is claimed until the image has actually been deployed and probed.
Current blockers: GitHub and Render account connections are not available to this session.
The checked-in setup targets Render Free web service + Render Free managed PostgreSQL.

## 1. Publish source

Create a public GitHub repository named `paytm-wallet-assessment`. Upload this folder's
contents to its root, including `Dockerfile`, `render.yaml`, and `.github/workflows/ci.yml`.
Exclude `.env`, tokens, and local fixture responses. The ZIP contains no real credentials.
If using git locally, initialize a repository, commit the files, add your new repository's
actual remote, and push. Do not use a made-up remote URL from documentation.

Wait for the `wallet-correctness` workflow to pass. Its `wallet-evidence` artifact contains
the Docker build/test/burst/restart evidence. A public repository makes the CI run reviewable.

## 2. Create the free Render deployment

1. Open [Render](https://dashboard.render.com/) and choose **New > Blueprint**.
2. Connect the repository and use the root `render.yaml`.
3. Confirm **Free** for both the web service and PostgreSQL. Keep the account without
   a payment method for this assessment. If onboarding requires a card for your account,
   stop here and choose a currently eligible no-card host; do not select a paid tier.
4. Apply the Blueprint. It creates the database, injects its internal URL, generates
   `ADMIN_TOKEN`, and builds the Dockerfile. Migrations run automatically on app startup.
5. Copy the actual web-service HTTPS URL and the generated admin token from the private
   Environment panel. Keep the token out of your repo, public logs, and screenshots.
6. Check `/readyz` and `/docs`. Free services may take around a minute to wake after idle.

The database's `ipAllowList: []` prevents public DB connections. The app uses the private
connection from the same Render region. Public HTTP requests still require user tokens.
Do not replace the internal database URL with a non-TLS public connection.

## 3. Probe and save evidence

Set `ADMIN_TOKEN` as an environment variable in your terminal. In bash you can enter it
without displaying it using `read -rs ADMIN_TOKEN` followed by `export ADMIN_TOKEN`.
Then run (replace the hostname):

```sh
python scripts/burst.py --base-url https://ACTUAL-HOST.onrender.com --report evidence/live-burst.json
```

Use `/logs` as the publicly viewable JSON logs link. `/events` contains durable business
events. To stream in a terminal during a burst:

```sh
curl -N https://ACTUAL-HOST.onrender.com/logs/stream
```

For a screen recording, show this stream beside the burst output. Hide the Environment
panel and any fixture response that contains tokens. Request correlation IDs link each
transfer-created/debited/credited or decline event to its request.

Capture `/metrics` before and after the burst. Include the source commit SHA, the Render
deploy ID, the image/build logs, and the public GitHub Actions run link with your submission.

## 4. Submission fields

Fill these only after checking them from a separate browser/terminal:

| Deliverable | Actual value to supply |
| --- | --- |
| Live API | Render-assigned HTTPS URL |
| Public source | Your public GitHub repository URL |
| Public logs | Live API + `/logs` (optionally `/logs/stream` and `/events`) |
| Metrics | Live API + `/metrics` |
| Repro command | `python scripts/burst.py --base-url <live-api>` with reviewer admin token supplied privately |
| One-page reasoning | `docs/writeup.pdf` |
| Verification | Passing public CI run and `evidence/live-burst.json` |

## Cost and limits

The Blueprint selects free plans: intended cost **INR 0**, with no paid service or addon.
Render documents 750 free instance hours per workspace/month, idle suspension after
15 minutes, and a **30-day expiration** for free PostgreSQL. Free databases have no managed
backups. Schedule the assessment within that lifetime; this is not durable production hosting.
Accounts and free-tier eligibility still need to be verified at signup. No subscription
or card entry is necessary for the code or local Docker setup.

For a longer-lived assessment, use an eligible free Neon database and set `DATABASE_URL`
with its TLS connection string; keep the same PostgreSQL transaction logic. Do not rely
on an unclaimed temporary database remaining available for your interview.

Sources checked September 9, 2026:
[Render free plans](https://render.com/docs/free),
[Blueprint specification](https://render.com/docs/blueprint-spec),
[Docker deployment](https://render.com/docs/docker).
