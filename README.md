# Kukua – growth monitoring assistant for community health workers

**Repo:** https://github.com/mercycheptoolb-ai/AI-Foundations-Final-Project · Report: `report.html` · Slides: `slides.html` (open by double-clicking)

Kukua ("to grow" in Swahili) helps community health workers and clinic nurses in Kenya assess a child's growth.
Kukua lets a community health worker describe a visit the way she naturally would, in words, in Swahili or with a photo, and gives back a WHO-exact answer the AI can't change, plus what to do and what to tell the family. The AI handles the messy input and the words; code built on the WHO tables makes every number and every referral decision;
the worker confirms the numbers in between.
The health worker types or dictates a visit note in English or Swahili, or attaches a photo of the growth card. Kukua then:

1. **Intake agent** (gpt-6-luna) turns the note into a structured record: sex, age, weight, length, MUAC (mid-upper arm circumference), oedema (swelling) and danger signs.
2. **The health worker confirms** every number on screen.
3. **The WHO calculator** (plain Python, no AI) computes the z-scores from the WHO Child Growth Standards and classifies the child against WHO/UNICEF cut-offs: severe or moderate acute malnutrition, stunting, underweight, overweight, and weight loss since the last visit.
4. **Triage agent** (gpt-6-luna) explains the result, lists next steps, and writes a message for the caregiver in English or Swahili. It uses two tools: the child's visit history and Kenya's national survey figures (KDHS 2022 via WHO). A code guard rejects any note whose referral differs from the WHO rules.

## Run it locally

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --port 8000
```

Open http://localhost:8000, click one of the example notes, then **Read note** → **Check growth**. You can use it as a
guest, or sign in from the sidebar to save visits.

**Demo account** (fictional, created on first run; defined in `backend/auth.py` as `DEMO_USER`):
username `demo`, password `kukua-demo`. You can also click **Create account** to register your own health-worker
account. Each worker sees their own recent visits; child records (by ID) are shared.

**API key:** the app reads `PORTKEY_API_KEY` from a `.env` file in this folder or, if that file is missing, from the folder
above it. See `.env.example`. The only model used is `gpt-6-luna` through Portkey. Without a key, **Enter by hand** still works
if you untick "Write AI counselling note": the WHO calculator needs no AI.

## Tests

```bash
.venv/bin/python tests/test_growth.py
```

The tests check the calculator against values printed in WHO's expanded z-score tables, plus the classification rules, the
input range checks and the growth-faltering check.

## Data guide chatbot (Twiga 🦒)

The "Ask Twiga" button opens a chat about Kenya's nutrition data: how common stunting, wasting, underweight, child
overweight and women's thinness/overweight/obesity are in each of Kenya's counties and former provinces (Kenya DHS 2014
and 2022, from the DHS Program API), how they changed, and how they differ by sex, urban/rural, wealth, mother's
education and child age (WHO GHO). It is a third gpt-6-luna agent with six read-only tools over
`data/kenya_dhs_regions.json` and `data/kenya_anthropometry.json`; a code guard sends back any answer containing a figure
no tool returned. Rebuild the county data with `python scripts/build_region_data.py`.

## Accounts

Signing in is optional; the sign-in panel is in the sidebar. **Guests** can read notes, check growth and get the AI
counselling note, but nothing is saved, no stored child or history is looked up, and each device is limited to 20 AI calls an
hour. **Signed-in workers** also get child IDs, visit history, weight-trend checks and a list of their own visits.
Passwords are stored as salted scrypt hashes, sessions are random tokens kept server-side (only a SHA-256 of the token
is stored) in an HttpOnly cookie that expires after 12 hours, and an account is locked for 10 minutes after 5 wrong passwords.

## Child IDs

Visit history is linked by **child ID**, never by name, so two children with the same name never share records.
Leave the ID blank for a new child and Kukua creates one (KK-0001, KK-0002, …) for the worker to write on the child's card.
Typing a name lists the registered children with that name, so the worker can pick the right ID. The server refuses a
visit when the ID belongs to a child of the other sex, or to a child older than the age entered. The demo data includes
two different girls named Amani (KK-0001 and KK-0003).

## Safety and cost controls

- The model never computes a z-score or decides a referral. An output validator sends the triage note back if its referral
  differs from the rule-based one, and falls back to a template note if the model keeps failing.
- Grounding check: any number the intake agent returns that does not appear in the note (allowing for unit conversions) is
  removed and the worker is asked to enter it.
- Sanitizer: strips invisible characters, caps the note length, and flags instruction-like text ("ignore your rules", "mark as healthy").
- Implausible measurements are rejected (range checks and WHO flags for impossible z-scores).
- Per-run token limits (`UsageLimits`) and a daily spend cap (`KUKUA_DAILY_BUDGET_USD`, default $2.00). Every agent run is
  logged to `output/usage_log.jsonl` with its token counts and cost.

## Safety and cost evaluation

Three scripts make real gpt-6-luna calls and save their results to `evals/results/`. The three defense layers
(code, design, prompt) can each be switched off for testing; the app always runs with all of them on.

```bash
.venv/bin/python evals/attack_test.py --repeats 2   # 9 attacks x 5 defense setups (about $0.10)
.venv/bin/python evals/guard_test.py                # scripted misbehaving model vs each guard (no API calls)
.venv/bin/python evals/red_team.py --n 12           # attacker agent writes new attacks, run with no vs all defenses
.venv/bin/python evals/cache_test.py --repeats 2    # prompt caching on vs off, quality check, tokenomics at scale
.venv/bin/python evals/chat_test.py                 # data guide: 10 questions checked against the data files
.venv/bin/python evals/photo_test.py --runs 8       # photo intake: a real handwritten note read 8 times, field by field
.venv/bin/python evals/make_results_page.py         # builds evals/results/index.html
```

## Report and slides

`report.html` and `slides.html` (reveal.js) are self-contained: screenshots are embedded, so they open by double-clicking.
Both are generated from the eval results and the screenshots, so every number in them comes from a real run:

```bash
.venv/bin/pip install playwright                     # drives your installed Google Chrome; no browser download
.venv/bin/python docs/capture_screenshots.py         # needs the app running on :8000
.venv/bin/python docs/build_docs.py
```

## Data

- `data/who_growth_standards.json`: WHO Child Growth Standards LMS tables (weight-for-age, length/height-for-age,
  weight-for-length, weight-for-height, MUAC-for-age). Rebuild with `python scripts/build_who_tables.py`.
- `data/kenya_anthropometry.json`: Kenya national anthropometry indicators from the WHO Global Health Observatory.
- `data/kukua.db` (created on first run): visits, seeded with three fictional demo visits.

Kukua supports, and does not replace, clinical judgement.
