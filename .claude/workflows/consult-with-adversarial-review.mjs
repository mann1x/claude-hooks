export const meta = {
  name: 'consult-with-adversarial-review',
  description:
    'Run a /consultants council, adversarially verify its answer with a ' +
    'skeptic panel, then accept or compose a focused follow-up and loop',
  whenToUse:
    'When a wrong-but-plausible council answer would be costly and you ' +
    'want the answer refuted before you trust it. Pass args.question + ' +
    'args.cwd (absolute). Optional: effort, verifyBudget, maxRounds.',
  phases: [
    { title: 'Consult', detail: 'start the council, wait for the answer' },
    { title: 'Review', detail: 'critique for wrong assumptions + gaps' },
    { title: 'Verify', detail: 'skeptic panel refutes load-bearing claims' },
    { title: 'Decide', detail: 'accept, or compose a follow-up and loop' },
  ],
}

// ----------------------------------------------------------------- //
// This is the Q2 deliverable: a Claude Code Workflow that drives the
// /consultants council programmatically (ask -> review -> skeptic panel
// -> accept|follow-up). The composeChallenge() step is where Q1 meets
// Q2 — surviving refutations become a focused, engine-fed follow-up.
//
// Four MANDATORY disciplines are baked in (see the SKILL "Driving the
// council from a Workflow" section for the why):
//   1. Thread --cwd on EVERY claude-consultants call (every prompt does).
//   2. Detect the followup cap on JSON `ok == false`, never on $?.
//   3. Gate on the per-run answer being COMPLETE before trusting
//      consultancy.status — `--wait` only prints the result once the
//      run reaches `completed`, so the wait IS the gate.
//   4. Cap the skeptic panel by verify_budget.
// ----------------------------------------------------------------- //

const question = (args && args.question) || ''
const cwd = (args && args.cwd) || ''
if (!question || !cwd) {
  throw new Error(
    'args.question and args.cwd (absolute) are both required')
}
const effort = (args && args.effort) || ''
const effortFlag = effort ? ` --effort ${effort}` : ''

// Discipline #4: skeptic-panel size driven by verify_budget. Mirrors
// the engine's config tiers (minimal=2 / bounded=3 / generous=5) but
// takes an explicit override so a Workflow run isn't coupled to the
// daemon's current config.
const BUDGET = (args && args.verifyBudget) || 'bounded'
const PANEL = ({ minimal: 2, bounded: 3, generous: 5 })[BUDGET] || 3
const MAX_ROUNDS = Number((args && args.maxRounds) || 4)

const CWD_RULE =
  `MANDATORY: pass --cwd "${cwd}" to every claude-consultants call.`

// ---- schemas ---------------------------------------------------- //

const CONSULT_SCHEMA = {
  type: 'object',
  properties: {
    root_sid: { type: 'string' },
    answer: { type: 'string' },
    consultancy_status: { type: 'string' },
    ok: { type: 'boolean' },
    reason: { type: 'string' },
  },
  required: ['root_sid', 'answer', 'ok'],
  additionalProperties: true,
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['accept', 'followup'] },
    concerns: { type: 'array', items: { type: 'string' } },
    claims_to_verify: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          claim: { type: 'string' },
          where: { type: 'string' },
        },
        required: ['claim'],
      },
    },
  },
  required: ['verdict', 'concerns', 'claims_to_verify'],
  additionalProperties: true,
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    claim: { type: 'string' },
    refuted: { type: 'boolean' },
    why: { type: 'string' },
  },
  required: ['claim', 'refuted', 'why'],
  additionalProperties: true,
}

// ---- prompts ---------------------------------------------------- //

function consultPrompt() {
  return [
    `Run a /consultants council on this question and return its answer.`,
    ``,
    `QUESTION: ${question}`,
    ``,
    CWD_RULE,
    `Run exactly:`,
    `  claude-consultants consult --message ${JSON.stringify(question)}` +
      ` --cwd "${cwd}"${effortFlag} --wait`,
    ``,
    `--wait blocks until the council reaches a terminal status, then`,
    `prints one JSON object on stdout. Parse it. The "ok" field is the`,
    `signal — true on a completed run, false otherwise (e.g. a failed`,
    `run). Extract: root_sid from consultancy.root_sid (fall back to the`,
    `top-level sid), answer from summary_markdown, consultancy_status`,
    `from consultancy.status. Return those plus ok and any reason.`,
    `Do NOT invent values — if ok is false, return ok:false with the`,
    `reason and empty answer.`,
  ].join('\n')
}

function reviewPrompt(answer) {
  return [
    `You are a skeptical staff engineer reviewing a council's answer to`,
    `the question below. Do not rubber-stamp it.`,
    ``,
    `QUESTION: ${question}`,
    ``,
    `COUNCIL ANSWER:`,
    answer,
    ``,
    `Find: (a) wrong assumptions about this project (a file/flag/API it`,
    `claims that may not exist, or a claim that contradicts how the repo`,
    `actually works); (b) gaps — an important sub-question left`,
    `unanswered; (c) load-bearing claims worth independently verifying.`,
    `${CWD_RULE} You MAY read the repo to sanity-check, but your job here`,
    `is to TRIAGE, not to fully verify — the skeptic panel does that.`,
    ``,
    `Return verdict "accept" if you have no material concern, else`,
    `"followup". List concerns (one line each) and claims_to_verify`,
    `(the most load-bearing first; the panel checks the top ${PANEL}).`,
    `For each claim include a "where" pointer (path:line or a search`,
    `hint) when you can.`,
  ].join('\n')
}

function skepticPrompt(claim, where) {
  return [
    `You are an adversarial verifier. Try to REFUTE this single claim`,
    `made by a council answering: ${question}`,
    ``,
    `CLAIM: ${claim}`,
    where ? `WHERE: ${where}` : ``,
    ``,
    `${CWD_RULE} Read the actual repo under that cwd (grep, open files,`,
    `check the line). Hunt for the way this claim is false or overstated`,
    `— a missing file, a flag that doesn't exist, a path:line that`,
    `doesn't say what it's cited for, a generalization that's false`,
    `here. Default to refuted:true ONLY with concrete evidence; if the`,
    `claim checks out, return refuted:false. "why" must cite what you`,
    `looked at.`,
  ].filter(Boolean).join('\n')
}

function followupPrompt(sid, brief) {
  return [
    `Issue a focused follow-up to an existing /consultants consultancy`,
    `to re-ground the unresolved concerns below, then return the new`,
    `answer.`,
    ``,
    `FOLLOW-UP BRIEF:`,
    brief,
    ``,
    CWD_RULE,
    `Run exactly:`,
    `  claude-consultants follow-up ${sid}` +
      ` --message ${'<the brief as one --message string>'}` +
      ` --cwd "${cwd}" --wait`,
    ``,
    `Discipline: the engine may refuse with HTTP 200 and a JSON body`,
    `{"ok": false, "reason": "followup_limit_reached"} when the followup`,
    `cap is hit. Detect that on the JSON "ok" field — NEVER on the shell`,
    `exit code. If ok is false, return ok:false with the reason and do`,
    `not retry. Otherwise parse the --wait result JSON and return`,
    `root_sid, answer (summary_markdown), consultancy_status, ok:true.`,
  ].join('\n')
}

function acceptPrompt(sid) {
  return [
    `The council's answer survived adversarial review. Mark the`,
    `consultancy accepted (terminal).`,
    ``,
    CWD_RULE,
    `Run: claude-consultants accept ${sid} --cwd "${cwd}"`,
    `Return the JSON it prints.`,
  ].join('\n')
}

// Q1-meets-Q2: turn the surviving refutations + triage concerns into a
// single focused follow-up brief. Plain JS — no agent needed.
function composeChallenge(review, survived) {
  const lines = []
  if (survived.length) {
    lines.push('The following claims FAILED adversarial verification — ' +
      're-ground or correct each:')
    for (const v of survived) {
      lines.push(`- ${v.claim} — refuted because: ${v.why}`)
    }
  }
  const concerns = (review.concerns || []).filter(Boolean)
  if (concerns.length) {
    lines.push('Also address these open concerns:')
    for (const c of concerns) lines.push(`- ${c}`)
  }
  if (!lines.length) {
    lines.push('Sharpen the answer: tighten any unsupported claim and ' +
      'cite path:line for each codebase-dependent statement.')
  }
  return lines.join('\n')
}

// ---- the loop --------------------------------------------------- //

phase('Consult')
const first = await agent(consultPrompt(),
  { phase: 'Consult', schema: CONSULT_SCHEMA, label: 'consult' })
if (!first || first.ok === false) {
  return {
    accepted: false,
    error: 'initial consult did not complete',
    detail: first && first.reason,
  }
}

let sid = first.root_sid
let answer = first.answer
let round = 0

while (true) {
  round++

  const review = await agent(reviewPrompt(answer),
    { phase: 'Review', schema: REVIEW_SCHEMA, label: `review:${round}` })

  const claims = (review.claims_to_verify || []).slice(0, PANEL)
  if (claims.length < (review.claims_to_verify || []).length) {
    log(`verify_budget=${BUDGET}: checking top ${PANEL} of ` +
      `${review.claims_to_verify.length} claims (panel cap)`)
  }

  const verdicts = (await parallel(claims.map((c, i) => () =>
    agent(skepticPrompt(c.claim, c.where),
      { phase: 'Verify', schema: VERDICT_SCHEMA,
        label: `skeptic:${round}:${i}` }))
  )).filter(Boolean)
  const survived = verdicts.filter((v) => v.refuted)

  log(`round ${round}: verdict=${review.verdict}, ` +
    `${survived.length}/${verdicts.length} claims refuted`)

  // Accept iff the reviewer is satisfied AND nothing survived refutation.
  if (review.verdict === 'accept' && survived.length === 0) {
    phase('Decide')
    await agent(acceptPrompt(sid),
      { phase: 'Decide', label: 'accept' })
    return {
      accepted: true, root_sid: sid, rounds: round, answer,
    }
  }

  if (round >= MAX_ROUNDS) {
    log(`reached maxRounds=${MAX_ROUNDS} without acceptance — ` +
      `surfacing for human review (NOT auto-accepting)`)
    return {
      accepted: false, root_sid: sid, rounds: round, answer,
      unresolved: { concerns: review.concerns, refutations: survived },
    }
  }

  // Q1 meets Q2: compose + fire the focused follow-up, then loop.
  phase('Decide')
  const brief = composeChallenge(review, survived)
  const fu = await agent(followupPrompt(sid, brief),
    { phase: 'Decide', schema: CONSULT_SCHEMA, label: `followup:${round}` })

  // Discipline #2: cap detection on the JSON ok field.
  if (!fu || fu.ok === false) {
    log(`follow-up refused (${fu && fu.reason || 'unknown'}) — ` +
      `surfacing for human approval (do not auto-retry past the cap)`)
    return {
      accepted: false, root_sid: sid, rounds: round, answer,
      awaiting_approval: (fu && fu.reason) === 'followup_limit_reached',
      reason: fu && fu.reason,
      unresolved: { concerns: review.concerns, refutations: survived },
    }
  }

  answer = fu.answer
  sid = fu.root_sid || sid
}
