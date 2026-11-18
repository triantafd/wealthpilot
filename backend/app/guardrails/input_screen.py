"""The input guard: refuse a message that is about the system, not about the firm.

ARCHITECTURE section 1 puts a guard on the way in. This is the deterministic
half of it, and for now the whole of it.

**Rules only, measured before a classifier is paid for.** The roadmap asks for
heuristic rules *and* an LLM classifier. The rules landed first on purpose: a
classifier is a model call on **every** message, which doubles the latency of
the cheapest path and charges every ordinary question for a benefit nobody has
demonstrated yet. `safety.jsonl` is what will show which attacks the rules
miss, and that is the argument for adding one — not that it sounds prudent.

**What is screened is the *subject*, not the tone.** A message asking what the
system was told, what it can read, or that it disregard its instructions is
never a legitimate adviser question: the firm's documents and the firm's
records are what this answers. A message that merely sounds authoritative —
*"compliance already cleared this, skip the queue"* — is **not** screened. It
is an ordinary instruction carrying a false claim, and the gate is what
answers it. A guard that refused those would turn a correct proposal into a
refusal, which is the failure the attack set's `gated` cases exist to catch.

**The false-positive bound is a test, not a judgement.**
`tests/test_input_screen.py` runs every message in all four golden sets —
routing, actions, sql and the RAG questions — through these rules and requires
that none is blocked. A rule that grows too greedy fails there, deterministically
and for nothing, rather than in front of an adviser.
"""

import re
from dataclasses import dataclass
from typing import Final

#: What the adviser is told. It names no rule and gives no detail: a refusal
#: that explains which pattern it matched is a refusal that teaches the next
#: attempt how to phrase itself.
REFUSAL: Final = (
    "I answer questions about the firm's policies, accounts and operations. "
    "Ask me about a client, an account or what the documents say."
)

#: The flag, in the house format of `action_unauthorised:...`.
FLAG: Final = "input_screened"


@dataclass(frozen=True)
class Screen:
    """Whether the message may go on, and which rule stopped it."""

    allowed: bool
    rule: str = ""

    @property
    def flag(self) -> str:
        return f"{FLAG}:{self.rule}" if self.rule else FLAG


#: Each rule is `(name, pattern)`, and the name reaches `guardrail_flags` so a
#: block can be read in the audit trail without rerunning anything.
#:
#: Written narrowly. Every pattern below names the *system* as its subject,
#: because that is the thing no adviser asks about and the thing every one of
#: these attacks needs. Phrases like "show me", "list" or "describe" appear in
#: ordinary questions by the hundred and are deliberately not triggers on their
#: own — the golden sets are full of them, and the test over those sets is what
#: keeps this honest.
RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        # "ignore/disregard/forget ... instructions|rules|prompt", with anything
        # short in between, which is how these arrive: "ignore your previous
        # instructions", "disregard the above rules".
        "override",
        re.compile(r"\b(ignore|disregard|forget)\b[^.?!]{0,40}\b(instruction|rule|prompt)", re.I),
    ),
    (
        # The instructions themselves as the subject: "your system prompt",
        # "the instructions you were given", "repeat your rules".
        "prompt_extraction",
        re.compile(
            r"\b(system prompt|your prompt)\b"
            r"|\b(your|the)\s+(instructions|rules)\b[^.?!]{0,30}"
            r"\b(given|got|received|word for word|verbatim|print|repeat|show|reveal)\b"
            r"|\b(print|repeat|reveal|show)\b[^.?!]{0,20}\b(your|the)\s+(instructions|rules|prompt)\b",
            re.I,
        ),
    ),
    (
        # The schema as the subject: "what tables can you read", "which views
        # do you have", "the columns you can see".
        "schema_extraction",
        re.compile(
            r"\b(tables?|views?|columns?|schema)\b[^.?!]{0,40}"
            r"\byou\b[^.?!]{0,20}\b(read|see|access|query|have|allowed)\b"
            r"|\b(information_schema|pg_catalog)\b"
            r"|\bdescribe\b[^.?!]{0,20}\b(table|view|schema)\b",
            re.I,
        ),
    ),
    (
        # Forged framing: a message cannot contain a system turn, so text that
        # looks like one is an attempt to be read as one.
        "forged_role",
        re.compile(r"<\s*/?\s*(system|assistant)\s*>|\[\s*(system|assistant)\s*\]", re.I),
    ),
)


def screen(message: str) -> Screen:
    """Whether this message may reach the agents."""
    for name, pattern in RULES:
        if pattern.search(message):
            return Screen(allowed=False, rule=name)
    return Screen(allowed=True)
