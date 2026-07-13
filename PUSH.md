# Pushing this to GitHub

## 1. Create an EMPTY repo on GitHub

Go to github.com/new. Name it `lab-agent`. **Do not** check "Add a README",
"Add .gitignore", or "Choose a license" — this repo already has all three, and an
empty remote means nothing to merge on the first push.

## 2. Push

```bash
cd lab-agent            # this folder

git init
git add .
git status              # <-- CHECK: you should see .env.example, NOT .env

git commit -m "Lab Agent: spoken intent to validated protocol execution"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/lab-agent.git
git push -u origin main
```

## 3. Sanity checks before you commit

- `git status` shows **`.env.example`** but **never `.env`** (your real API key must not
  go up — `.gitignore` covers it, but look anyway).
- No `__pycache__/` or `.venv/` in the staged list.
- The README says the web console is planned, not built. Keep it honest until it exists.

## 4. Verify it runs from a clean clone

```bash
git clone https://github.com/YOUR_USERNAME/lab-agent.git
cd lab-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m tests.test_pipeline    # 8 passed
python -m demo.demo_script       # both scenarios
```

Both run **with no API key** — the planner falls back to a deterministic mock, so a
fresh clone (or a hackathon judge) can run the demo offline immediately.

## 5. Adding the web console later

Keep it in its own folder so the backend stays clean:

```bash
mkdir web
# ...build the UI in web/...
git add web
git commit -m "Add web console"
git push
```

The `.gitignore` already excludes `web/node_modules/`, `web/dist/`, and `web/.next/`.
