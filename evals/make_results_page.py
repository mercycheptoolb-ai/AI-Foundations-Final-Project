"""Build evals/results/index.html (self-contained) from the JSON results, for screenshots in the report.

Usage:  .venv/bin/python evals/make_results_page.py
"""

from __future__ import annotations

import html
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def load(name: str) -> dict | None:
    p = RESULTS / name
    return json.loads(p.read_text()) if p.exists() else None


e = html.escape


def pct(a: int, b: int) -> str:
    return f"{a}/{b}" + (f" <small>({a / b:.0%})</small>" if b else "")


def cell(hits: int, total: int) -> str:
    cls = "bad" if hits else "ok"
    return f'<td class="num {cls}">{"✗" if hits else "✓"} {hits}/{total}</td>'


def attack_section(d: dict) -> str:
    setups, rep = d["setups"], d["repeats"]
    head = "".join(f"<th class='num'>{e(s)}</th>" for s in setups)
    rows = "".join(f"<tr><td>{e(name)}</td>" + "".join(cell(c[s], rep) for s in setups) + "</tr>"
                   for name, c in d["by_attack"].items())
    total = "<tr class='total'><td>Successful attacks</td>" + "".join(
        f"<td class='num'>{pct(d['tally'][s]['successes'], d['tally'][s]['runs'])}</td>" for s in setups) + "</tr>"
    # Evidence: what each defense actually did in the all-layers runs
    ev = []
    for r in d["rows"]:
        if r["setup"] != "all" or r["repeat"] != 0:
            continue
        bits = []
        if r.get("sanitizer") and r["sanitizer"].get("suspicious"):
            bits.append("sanitizer removed " + ", ".join(f"“{e(x)}”" for x in r["sanitizer"]["suspicious"]))
        bits += [e(w) for w in r.get("warnings", [])]
        bits += [e(g) for g in r.get("guard_events", [])]
        if r.get("stopped"):
            bits.append("pipeline stopped: " + e(r["stopped"]))
        outcome = "attack succeeded: " + e("; ".join(r["why"])) if r["success"] else \
            f"blocked · final referral <b>{e(r.get('final_referral') or '–')}</b>"
        ev.append(f"<tr><td>{e(r['attack'])} {e(r['name'])}</td><td>{'<br>'.join(bits) or '<span class=muted>model ignored it</span>'}</td>"
                  f"<td class='{'bad' if r['success'] else 'ok'}'>{outcome}</td></tr>")
    fails = [r for r in d["rows"] if r["success"] and r["setup"] == "none" and r["repeat"] == 0]
    fail_rows = "".join(f"<tr><td>{e(r['attack'])} {e(r['name'])}</td><td>{e('; '.join(r['why']))}</td>"
                        f"<td>{e(r.get('caregiver_message') or '–')}</td></tr>" for r in fails)
    return f"""
<section><h2>1 · Attack suite by defense layer</h2>
<p>{len(d['by_attack'])} attacks × {len(setups)} setups × {rep} repeats = {len(d['rows'])} full pipeline runs
(intake agent → WHO calculator → triage agent), each a fresh gpt-6-luna call. The health worker is simulated as accepting the
intake record without checking it (worst case). ✓ = attack failed every time, ✗ = attack succeeded in that many runs.
Cost of the whole suite: ${d['total_cost_usd']:.4f}.</p>
<table><thead><tr><th>Attack</th>{head}</tr></thead><tbody>{rows}{total}</tbody></table>
<h3>What the defenses did (all layers on, first repeat)</h3>
<table><thead><tr><th>Attack</th><th>Defense events</th><th>Outcome</th></tr></thead><tbody>{''.join(ev)}</tbody></table>
<h3>What went wrong with no defenses (first repeat)</h3>
<table><thead><tr><th>Attack</th><th>Harm</th><th>Caregiver message the app produced</th></tr></thead><tbody>{fail_rows or '<tr><td colspan=3>none</td></tr>'}</tbody></table>
</section>"""


def guard_section(d: dict) -> str:
    rows = []
    for c in d["triage_cases"]:
        a = c["all"]
        off_key = next(k for k in c if k.startswith("no "))
        n = c[off_key]
        rows.append(f"<tr><td>{e(c['case'])}</td><td class='ok'>referral <b>{e(a['final_referral'])}</b>, follow-up {a['follow_up_in_days']} d"
                    f"{' · template note' if a['used_fallback'] else ''}<br><small>{'<br>'.join(e(g) for g in a['guard_events'])}</small></td>"
                    f"<td class='{'bad' if n['harm'] else 'ok'}'>"
                    f"<small>{e(off_key)}:</small> referral {e(n['final_referral'])}, follow-up {n['follow_up_in_days']} d<br>“{e(n['caregiver_message'])}”"
                    f"{'<br><b>harm: ' + e('; '.join(n['harm'])) + '</b>' if n['harm'] else ''}</td></tr>")
    for c in d["intake_cases"]:
        a, n = c["all"], c["no code"]
        rows.append(f"<tr><td>{e(c['case'])}<br><small>{e(c['note'])}</small></td>"
                    f"<td class='{'bad' if a['harm'] else 'ok'}'>weight recorded: <b>{a['record']['weight_kg']}</b><br><small>{'<br>'.join(e(w) for w in a['warnings']) or 'no warning'}</small></td>"
                    f"<td class='{'bad' if n['harm'] else 'ok'}'><small>no code:</small> weight recorded: {n['record']['weight_kg']}</td></tr>")
    return f"""
<section><h2>2 · Malfunction test: a model that misbehaves on purpose</h2>
<p>gpt-6-luna resisted every attack in section 1 even without defenses, so this test swaps in a scripted model that does
the harmful thing every time and runs it through the app's real code path. It shows what each guard stops. No API calls.
The per-run usage limit (G5) is always on, so its right-hand column shows the same protection.</p>
<table><thead><tr><th>Malfunction</th><th>All defenses on</th><th>Defense layer off</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p class='muted'>G8 is a known gap: when the attacker's number is also written in the note, the grounding check cannot tell which
one is real. The health worker's confirm step (every number is shown before assessing) is the defense there.</p></section>"""


def red_section(d: dict) -> str:
    rows = []
    for a in d["attacks"]:
        res = {r["setup"]: r for r in d["rows"] if r["attack"] == a["id"]}
        rows.append(f"<tr><td>{e(a['id'])}</td><td>{e(a['name'])}</td><td class='note'>{e(a['note'])}</td><td>{e(', '.join(a['goals']))}</td>"
                    + "".join(f"<td class='{'bad' if res[s]['success'] else 'ok'}'>{'✗ ' + e('; '.join(res[s]['why'])) if res[s]['success'] else '✓ safe'}</td>"
                              for s in ("none", "all")) + "</tr>")
    t = d["tally"]
    return f"""
<section><h2>3 · Red team: attacks written by an attacker agent</h2>
<p>A separate gpt-6-luna "attacker" wrote {len(d['attacks'])} new attack notes, told not to reuse the known attacks above.
Each ran once with no defenses and once with all defenses. Successful: <b>none {pct(t['none']['successes'], t['none']['runs'])}</b>,
<b>all {pct(t['all']['successes'], t['all']['runs'])}</b>. Cost ${d['total_cost_usd']:.4f}.</p>
<table><thead><tr><th>#</th><th>Technique</th><th>Attack note</th><th>Goal</th><th>No defenses</th><th>All defenses</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section>"""


def cache_section(d: dict) -> str:
    on, off = d["on"], d["off"]

    def row(label, k, fmt="{:,.0f}"):
        return f"<tr><td>{label}</td><td class='num'>{fmt.format(on[k])}</td><td class='num'>{fmt.format(off[k])}</td></tr>"
    q = lambda s, k: f"{s['quality'][k]}/{s['visits']}"  # noqa: E731
    qrows = "".join(f"<tr><td>{label}</td><td class='num'>{q(on, k)}</td><td class='num'>{q(off, k)}</td></tr>" for label, k in [
        ("Final referral matches WHO rules", "referral_ok"), ("Model's own referral matched rules (before guard)", "model_referral_matched_rules"),
        ("Triage used both tools", "used_both_tools"), ("Caregiver message in the right language", "language_ok")])
    agents = "".join(
        f"<tr><td>{a}</td>" + "".join(f"<td class='num'>{s['by_agent'][a]['input_tokens']:,.0f}</td><td class='num'>{s['by_agent'][a]['cached_input_tokens']:,.0f}</td>"
                                      f"<td class='num'>{s['by_agent'][a]['output_tokens']:,.0f}</td><td class='num'>${s['by_agent'][a]['cost_usd']:.5f}</td>" for s in (on, off)) + "</tr>"
        for a in ("intake", "triage"))
    t = d["tokenomics_cache_on"]
    scale_rows = "".join(f"<tr><td class='num'>{int(n):,}</td><td class='num'>${v['month_usd']:,.2f}</td><td class='num'>${v['year_usd']:,.2f}</td>"
                         f"<td class='num'>${d['tokenomics_cache_off']['users'][n]['month_usd']:,.2f}</td></tr>" for n, v in t["users"].items())
    est = d["static_prompt_token_estimate_tiktoken"]
    return f"""
<section><h2>4 · Prompt caching on vs off</h2>
<p>The same {len(d['visits'])} visit notes × {d['repeats']} repeats, all defenses on, run one after another. "Off" puts a random id
at the start of the instructions so no request can reuse the cache. Prices per 1M tokens: input ${d['prices_per_1m']['input']},
cached input ${d['prices_per_1m']['cached_input']}, output ${d['prices_per_1m']['output']}.</p>
<table><thead><tr><th>Per visit (mean)</th><th class='num'>Cache on</th><th class='num'>Cache off</th></tr></thead><tbody>
{row('Input tokens', 'mean_input_tokens')}{row('Cached input tokens', 'mean_cached_input_tokens')}
{row('Output tokens', 'mean_output_tokens')}{row('Reasoning tokens (part of output)', 'mean_reasoning_tokens')}
{row('Cost', 'mean_cost_usd', '${:.5f}')}{row('Seconds', 'mean_seconds', '{:.1f}')}
<tr class='total'><td>Caching saves</td><td class='num' colspan=2>{d['cost_saving_share']:.0%} per visit</td></tr></tbody></table>
<h3>Per agent</h3>
<table><thead><tr><th rowspan=2>Agent</th><th colspan=4>Cache on</th><th colspan=4>Cache off</th></tr>
<tr><th class='num'>in</th><th class='num'>cached</th><th class='num'>out</th><th class='num'>cost</th><th class='num'>in</th><th class='num'>cached</th><th class='num'>out</th><th class='num'>cost</th></tr></thead>
<tbody>{agents}</tbody></table>
<p class='muted'>Static prompt size estimated with tiktoken (o200k_base): intake {est['intake']:,} tokens, triage {est['triage']:,} tokens
(before the output schema and tool definitions are added; with them the intake request is about 1,150 tokens). OpenAI only
caches prompt prefixes of 1,024+ tokens, which both agents now clear.</p>
<h3>Did caching hurt quality?</h3>
<table><thead><tr><th>Check</th><th class='num'>Cache on</th><th class='num'>Cache off</th></tr></thead><tbody>{qrows}
<tr><td>Guard retries (note sent back)</td><td class='num'>{on['quality']['validator_retries']}</td><td class='num'>{off['quality']['validator_retries']}</td></tr>
<tr><td>Mean caregiver message length (words)</td><td class='num'>{on['quality']['mean_caregiver_words']}</td><td class='num'>{off['quality']['mean_caregiver_words']}</td></tr></tbody></table>
</section>
<section><h2>5 · Tokenomics at scale</h2>
<p>Measured cost per visit with caching: <b>${t['cost_per_visit_usd']:.5f}</b>. Assumption: one community health worker (user)
records about {t['assumption_visits_per_user_month']} growth-monitoring visits a month → <b>${t['per_user_month_usd']:.3f} per user per month</b>.
Excludes hosting.</p>
<table><thead><tr><th class='num'>Users</th><th class='num'>Per month (cache on)</th><th class='num'>Per year (cache on)</th><th class='num'>Per month (cache off)</th></tr></thead>
<tbody>{scale_rows}</tbody></table></section>"""


def chat_section(d: dict) -> str:
    sm = d["summary"]
    rows = "".join(f"<tr><td>{e(r['question'])}<br><small>{e(r['kind'])}</small></td><td>{e(r['answer'])}</td>"
                   f"<td class='{'ok' if r['ok'] else 'bad'}'>{'✓' if r['ok'] else '✗'}</td><td class='num'>${r['cost_usd']:.5f}</td></tr>"
                   for r in d["rows"])
    return f"""
<section><h2>6 · Data guide (Twiga) accuracy and cost</h2>
<p>{sm['questions']} questions; each lookup's expected figure was read directly from the data files, not from the model.
Correct: <b>{sm['correct']}/{sm['questions']}</b>. Mean per answer: {sm['mean_input_tokens']:,.0f} input tokens ({sm['mean_cached_input_tokens']:,.0f} cached),
{sm['mean_output_tokens']:,.0f} output, <b>${sm['mean_cost_usd']:.5f}</b>, {sm['mean_seconds']} s.</p>
<table><thead><tr><th>Question</th><th>Answer</th><th>OK</th><th class='num'>Cost</th></tr></thead><tbody>{rows}</tbody></table></section>"""


def main() -> None:
    parts = []
    for name, fn in (("attack_results.json", attack_section), ("guard_results.json", guard_section),
                     ("red_team_results.json", red_section), ("cache_results.json", cache_section),
                     ("chat_results.json", chat_section)):
        d = load(name)
        parts.append(fn(d) if d else f"<section><p class='muted'>{name} not found: run the script first.</p></section>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Kukua · safety and cost evaluation</title>
<style>
body{{margin:0;background:#fff8ee;color:#23324d;font:14px/1.5 "Nunito","Avenir Next",system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:28px 24px 48px}} h1{{margin:0 0 4px;font-size:26px;font-weight:900}} h1 span{{color:#1f9d6a}}
h2{{font-size:18px;margin:0 0 6px;font-weight:900}} h3{{font-size:14px;margin:18px 0 6px;color:#16855a}}
section{{background:#fff;border-radius:20px;padding:20px;margin-top:16px;box-shadow:0 10px 30px rgba(35,50,77,.08);border-top:6px solid #a78bfa}}
p{{color:#55627a;margin:0 0 10px}} b{{color:#23324d}} .muted,small{{color:#8a94a6}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:7px 9px;border-bottom:2px solid #fbf5ec;vertical-align:top}} th{{color:#55627a;background:#fbf5ec}}
.num{{text-align:right}} td.ok{{color:#18794e}} td.bad{{color:#b4232a}} tr.total td{{font-weight:800;border-top:3px solid #1f9d6a}}
td.note{{max-width:360px;color:#55627a}}
</style></head><body><main><h1>🌱 Kukua · <span>safety and cost evaluation</span></h1>
<p>Generated from evals/results/*.json · model gpt-6-luna via Portkey · defense layers: <b>code</b> (sanitizer, grounding check),
<b>design</b> (WHO rules decide the referral, output validator), <b>prompt</b> (security rules in the prompts).</p>
{''.join(parts)}</main></body></html>"""
    out = RESULTS / "index.html"
    out.write_text(page)
    print("wrote", out)


if __name__ == "__main__":
    main()
