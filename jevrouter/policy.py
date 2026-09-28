"""The decision policy (docs/PLAN-accuracy-v2.md A2): Jev's route decision checked against what the agents can actually
do, as one pure, synchronous chain of rules that records a trace of what each rule did.

Rules run in the plan's order; the first rule that settles the step stops the chain, and every rule that looked at the
step adds a trace entry {rule, agent, why}, where `agent` is the step's agent after that rule. Two rules run outside:
blocked_dependency (a step that builds on a blocked one never reaches Jev) and ambiguous_term (an async lookup), both in
the pipeline with the same trace entry.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from . import create as cf
from . import gate
from .agents import create as create_agent
from .agents.tools import dates_question, units_question
from .config import BLOCK_AT, CONFIRM_AT, GUARDS, KEYLESS, SQL_AGENT, UNSUPPORTED_AT
from .files import FILE_AGENTS


@dataclass
class StepCtx:
    text: str                      # what the agent will receive (after prepare)
    ctx: str                       # dependency context ('' when none)
    tid: str
    offered: dict[str, str]        # agents offered to Jev for this run
    runnable: set[str]             # agents in the run's registry
    forced: str | None             # the @agent, only when this step is the bound step
    mode: str                      # quick | balanced | deep | research
    has_engine: bool
    web: bool                      # the run's engine can search the web
    attached: bool
    has_context: bool              # earlier chat turns exist
    depends_on: list[str]
    frame: dict | None             # previous turn's frame (A4), keyless follow-ups only


@dataclass
class Decision:
    agent: str
    pick: str
    reason: str
    note: str | None = None        # answer text decided by the policy (guards, can't, questions)
    note_ok: bool = False
    assumption: str | None = None  # one-line stated assumption shown before an LLM answer (A6)
    trace: list[dict] = field(default_factory=list)   # [{rule, agent, why}], in order applied
    caveats: list[str] = field(default_factory=list)  # limits the answer must state, for the merger (B6 time_sensitive)


RULES = ('blocked', 'blocked_dependency', 'unsupported', 'cant_do', 'forced', 'create_demote', 'file_intent',
         'keyless_contract', 'confirmed', 'attached_file', 'refers_back_file', 'advice', 'time_sensitive',
         'clarify_policy', 'missing_slot', 'mode_research', 'ambiguous_term')


def tool_question(agent: str, text: str) -> str | None:
    """The unit and date agents' follow-up question when their parser finds nothing to work with (gate.question covers
    the older agents), so "How much is a pound?" asks which meaning instead of failing."""
    return units_question(text) if agent == 'units' else dates_question(text) if agent == 'dates' else None


# Agents that answer from an LLM and so can state an assumption instead of asking (A6), and the keyless agents whose live
# APIs are real, so a "live data" signal never makes them unsupported (A3).
LLM_AGENTS = ('knowledge', 'research', 'report', 'chat', 'code')
LIVE_APIS = ('weather', 'time', 'currency')
STICKY_MARGIN = 0.15  # A4: the previous turn's agent is kept when Jev gives it this close to its top pick
# "How do I book a flight cheaply?" asks for steps, not for an action
HOW_TO = re.compile(r"^\s*(?:how\b|what(?:'s| is| are)\s+(?:the\s+)?(?:best|cheapest|easiest|fastest)\s+way)|\bhow\s+"
                    r"(?:to|do|does|can|could|would|should)\b", re.I)
# "What was Apple's stock price in 2010?" asks about the past, which needs no live data
NOW = re.compile(r'\b(?:now|today|tonight|currently|current|live|latest|right now|at the moment|this (?:week|month|year))\b',
                 re.I)


def past_only(text: str) -> bool:
    """True when the text is about a year before this one and nothing current."""
    this = date.today().year
    years = [int(y) for y in re.findall(r'\b((?:19|20)\d\d)\b', text)]
    return bool(years) and all(y < this for y in years) and not NOW.search(text)


def names_slot(agent: str, text: str) -> bool:
    """The text names a detail of its own for this (keyless) agent: a place, an amount, an expression."""
    p = gate.parse_for(agent, text)
    return bool(p and p.slots)


def keyless_question(agent: str, unused: list[str], text: str) -> str:
    """The question a keyless agent asks when its parser can't use everything the text gives."""
    if 'second place' in unused:
        places = gate.places_in(text)
        return (f'I can look up one place at a time. Which do you want first: {gate.join_or(places)}?' if len(places) > 1
                else 'Which one place do you mean?')
    if 'second currency' in unused:
        return 'I can convert to one currency at a time. Which currency do you want first?'
    if 'source time' in unused:
        return ("I can tell you the current time in a place, but converting a given time between places needs an LLM "
                'engine. Which place do you want the current time for?')
    return 'Could you write that with just the numbers and what to do with them?'


def apply(d: dict, s: StepCtx) -> Decision:
    """The final decision for one step from Jev's decision `d` (route_one's fields). `d` is not changed."""
    probabilities = d.get('probabilities') or {}
    signals = d.get('signals') or {}
    unsafe = d.get('unsafe', 0.0)
    out = Decision(d['agent'], d['pick'], d['reason'])
    text = s.text

    def step(rule: str, why: str):
        out.trace.append({'rule': rule, 'agent': out.agent, 'why': why})

    def settle(agent: str, reason: str, rule: str, why: str, note: str | None = None, ok: bool = False) -> Decision:
        out.agent, out.reason = agent, reason
        if note is not None:
            out.note, out.note_ok = note, ok
        step(rule, why)
        return out

    # 1. blocked: a blocked step stays blocked, forced or not
    if out.agent == 'blocked' or unsafe >= BLOCK_AT:
        reason = out.reason if out.agent == 'blocked' else f'Jev flagged it as unsafe ({unsafe:.0%})'
        return settle('blocked', reason, 'blocked', reason)
    forced = s.forced
    wants = gate.wants_file(text) or bool(cf.detect_format(text))
    # 6. create_demote, read first because cant_do judges the demoted pick: Jev leans to create for "write an email" or
    # "plan a trip"; without a format or a file asked for, the text answer is what's wanted, so the runner-up takes it
    # (and "send an email" still gets its "can't")
    if not forced and out.pick == 'create' and not wants:
        runner = next((a for a in probabilities if a not in ('create', *GUARDS) and a in s.offered), None)
        if runner:
            out.pick = runner
            if out.agent == 'create':
                out.agent, out.reason = runner, f"{out.reason}, but no file was asked for"
                step('create_demote', 'no file or format was asked for')
    pick = forced or out.pick
    # 4. cant_do, the fast path in front of unsupported: "Remind me at 5pm" gets an honest "I can't", not the time
    if cant := gate.cant_do(text, pick, 1.0 if forced else probabilities.get(pick, 0)):
        return settle('chat', f"can't {cant[0]}", 'cant_do', f"can't {cant[0]}", cant[1], True)
    # 3. unsupported: what no agent here can do (A3)
    if settled := unsupported(out, signals, s, pick, wants, settle, step):
        return settled
    # 5. forced: the bound step takes the @agent
    if forced:
        return settle(forced, f'you picked @{forced}', 'forced', f'you picked @{forced}')
    # 7. file_intent: "turn these release notes into a summary file" is a request for a file, so create makes it
    if 'create' in s.offered and out.agent not in (*GUARDS, 'create') and gate.wants_file(text):
        return settle('create', f"{out.reason}, but the request is for a file", 'file_intent', 'the request is for a file')
    # frame (A4): a keyless follow-up stays with the previous turn's agent when Jev nearly picked it
    if s.frame and (prev := s.frame.get('agent')) and prev != out.pick and prev in s.runnable and prev not in GUARDS:
        if (probabilities.get(prev, 0) >= probabilities.get(out.pick, 0) - STICKY_MARGIN
                and out.agent in (out.pick, 'clarify') and not (out.pick in KEYLESS and names_slot(out.pick, text))):
            out.agent, out.pick, out.reason = prev, prev, f'{out.reason}, but it follows on from the previous {prev} answer'
            step('frame', f'follows on from the previous {prev} answer')
    # 8. keyless_contract: a keyless pick is kept only when its parser can use everything the text gives (A5)
    # A detail named only by description counts even when the parser found nothing else ("Convert 250 GBP into the
    # currency of <somewhere>" gives the currency parser one currency): it is looked up or declined, never asked for
    if (out.agent in KEYLESS and (p := gate.parse_for(out.agent, text)) is not None and not p.full
            and (p.slots or any(u.startswith('described') for u in p.unused))):
        agent = out.agent
        if 'future date' in p.unused and (note := gate.future_note(text)):
            return settle('unsupported', 'the date is beyond what any source here covers', 'keyless_contract',
                          'future date', note, True)
        if any(u.startswith('described') for u in p.unused):
            if s.has_engine and 'knowledge' in s.runnable:
                out.agent, out.reason = 'knowledge', f'{out.reason}, but a detail is named only by description'
                step('keyless_contract', 'a detail is named only by description')
            else:
                return settle('unsupported', 'a detail is named only by description', 'keyless_contract',
                              'the description needs an engine to look up', gate.unsupported_text('described', text), True)
        else:
            # two places or two currencies are two lookups, which one agent call can't make: ask which first. A given
            # clock time to convert, or numbers the parser left out, an LLM agent can work with.
            llm_can = not {'second place', 'second currency'} & set(p.unused)
            runner = next((a for a in probabilities if a in LLM_AGENTS and a in s.runnable),
                          'knowledge' if 'knowledge' in s.runnable else None) if s.has_engine and llm_can else None
            if runner:
                out.agent, out.reason = runner, f"{out.reason}, but the {agent} parser can't use all of it"
                step('keyless_contract', f"the {agent} parser can't use: {', '.join(p.unused) or 'every number'}")
            else:
                return settle('clarify', f"the {agent} parser can't use all of it", 'keyless_contract',
                              f"the {agent} parser can't use: {', '.join(p.unused) or 'every number'}",
                              keyless_question(agent, p.unused, text), False)
    pick = out.pick
    # 9. confirmed: a keyless parser that finds every detail it needs overrules a clarify
    if (out.agent == 'clarify' and pick in KEYLESS and pick in s.runnable
            and probabilities.get(pick, 0) >= CONFIRM_AT and gate.confirmed(pick, text)):
        out.agent, out.reason = pick, f"{out.reason}, but the {pick} parser found all it needs"
        step('confirmed', f'the {pick} parser found all it needs')
    elif (out.agent == 'clarify' and pick in KEYLESS and pick in s.runnable
          and probabilities.get(pick, 0) >= CONFIRM_AT and gate.declines(pick, text)):
        # the parser knows what is asked and that it can't be done ("100 USD to Wakandan dollars"): say so, don't ask
        out.agent, out.reason = pick, f"{out.reason}, but the {pick} parser recognises what it can't do"
        step('confirmed', f"the {pick} parser recognises what it can't do")
    # attached_file: "the total in this spreadsheet" reads as vague to Jev, which sees only the text; the attached file
    # is the missing context, so a confident file-agent pick stands
    if (out.agent == 'clarify' and s.attached and pick in (*FILE_AGENTS, *SQL_AGENT) and pick in s.offered
            and probabilities.get(pick, 0) >= CONFIRM_AT):
        out.agent, out.reason = pick, f"{out.reason}, but a file is attached"
        step('attached_file', 'a file is attached')
    # refers_back_file: "put that in a PDF" means the chat's earlier answer, an earlier step or an attached file, which
    # Jev doesn't see, so a create pick stands; so does create as a close runner-up ("and as markdown please", where
    # Jev can't tell making a file from reading one)
    near = 'create' in s.offered and probabilities.get('create', 0) >= probabilities.get(pick, 0) - STICKY_MARGIN
    if (out.agent == 'clarify' and (pick == 'create' or near) and create_agent.asks_for_file(text)
            and (s.has_context or s.attached or s.depends_on)):
        out.agent, out.reason = 'create', f"{out.reason}, but it asks for a file of what came before"
        step('refers_back_file', 'it asks for a file of what came before')
    # 10. advice: "Should I learn Rust or Go?" is a question for a person's judgement, not a lookup or code (B6)
    if out.agent in (*LLM_AGENTS, 'clarify') and gate.advice(text):
        if s.has_engine and 'chat' in s.runnable:
            if out.agent != 'chat':
                out.agent, out.reason = 'chat', f'{out.reason}; it asks for advice'
            step('advice', 'it asks for advice')
        else:
            return settle('knowledge', 'it asks for advice', 'advice', 'advice needs an engine', gate.ADVICE_KEYLESS, False)
    # 11. time_sensitive: cut-off ranks and fees change every year; searched when a web engine is on, else hedged (B6)
    if out.agent in ('knowledge', 'report', 'chat') and gate.time_sensitive(text):
        if s.web and 'research' in s.runnable:
            out.agent, out.reason = 'research', f'{out.reason}; figures like these change, so the web is searched'
            step('time_sensitive', 'figures like these change, so the web is searched')
        else:
            out.caveats.append(gate.TIME_SENSITIVE_CAVEAT)
            step('time_sensitive', 'figures like these change; the answer says to check the official source')
    # 12. clarify_policy: Jev found it unclear, but it is sure which LLM agent fits: answer, stating the assumption (A6)
    if (out.agent == 'clarify' and d['agent'] == 'clarify' and pick in LLM_AGENTS and pick in s.runnable and s.has_engine
            and probabilities.get(pick, 0) >= 0.9):
        out.agent, out.reason = pick, f'{out.reason}, but {pick} is a clear fit'
        out.assumption = f'Assuming you mean {gate.option(pick, s.offered)} about "{text.strip()[:60]}".'
        step('clarify_policy', f'{pick} at {probabilities.get(pick, 0):.0%}: answered with a stated assumption')
    # 13. missing_slot: the agent can't act without a detail the text lacks
    try:
        ask = None if out.agent in GUARDS else gate.question(out.agent, text) or tool_question(out.agent, text)
        if (ask is None and out.agent == 'create' and not (s.has_context or s.attached or s.depends_on)):
            ask = gate.topic_question(text)  # "Make me a presentation": about what?
    except Exception:  # a parser bug must not fail the whole run; the step's agent reports its own error
        ask = None
    if ask:
        missing = out.agent
        out.agent, out.reason = 'clarify', f"missing detail for {missing}"
        out.note, out.note_ok = ask, False
    step('missing_slot', out.reason if ask else 'no detail missing')
    # 14. mode_research: research mode searches the web for what would be a knowledge or report answer
    if s.mode == 'research' and out.agent in ('knowledge', 'report') and s.web and 'research' in s.runnable:
        out.agent, out.reason = 'research', f"{out.reason}; research mode searches the web"
        step('mode_research', 'research mode searches the web')
    return out


def unsupported(out: Decision, signals: dict, s: StepCtx, pick: str, wants: bool, settle, step) -> Decision | None:
    """Rule 3 (A3): the step asks for an action, private data, live data with no web engine, or a date no source
    covers. Returns the settled decision, or None to go on."""
    text = s.text
    at = UNSUPPORTED_AT
    if signals.get('action', 0) >= at and not (pick == 'create' and wants) and not HOW_TO.search(text):
        return settle('unsupported', 'it asks for an action in the world', 'unsupported', f"action {signals['action']:.0%}",
                      gate.unsupported_text('action', text), True)
    if signals.get('personal', 0) >= at and pick not in (*FILE_AGENTS, *SQL_AGENT) and not s.attached:
        return settle('unsupported', 'it needs private information', 'unsupported',
                      f"personal {signals['personal']:.0%}", gate.unsupported_text('personal', text), True)
    # a year that hasn't happened is settled before live data could send the step to the web: no search finds it
    if pick not in ('create', 'code', 'run', 'url', *FILE_AGENTS, *SQL_AGENT):
        future = pick in ('weather', 'currency') or (gate.PREDICTION.search(text) and gate.future_year(text))
        if future and (note := gate.future_note(text)):
            return settle('unsupported', 'it asks about a date no source here covers', 'unsupported', 'future date',
                          note, True)
    if (signals.get('live', 0) >= at and pick not in (*LIVE_APIS, 'create', 'url', *FILE_AGENTS, *SQL_AGENT)
            and not past_only(text) and not s.forced):
        if s.web and 'research' in s.runnable:
            if out.agent != 'research':
                out.agent, out.reason = 'research', f"{out.reason}; it needs live data, so the web is searched"
            step('mode_research', f"live {signals['live']:.0%}: the web is searched")
            return out
        # advice or planning around figures that change yearly ("a plan for admission and the rank I need") is answered
        # with the time_sensitive caveat (rule 11), not refused; "what's the cut-off right now" still needs live data
        if gate.time_sensitive(text) and not NOW.search(text) and not gate.PREDICTION.search(text):
            step('unsupported', f"live {signals['live']:.0%}, but figures that change yearly are answered with a caveat")
            return None
        return settle('unsupported', 'it needs live data', 'unsupported', f"live {signals['live']:.0%}, no web engine",
                      gate.unsupported_text('live', text), True)
    return None
