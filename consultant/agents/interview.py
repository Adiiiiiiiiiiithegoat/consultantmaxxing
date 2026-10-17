"""Case-mode interview rules, deterministic: what a real interview looks like from the
candidate's side (checked against the casebook's train interviews, see PROGRESS.md).

- The interviewer often prompts instead of answering: "focus on revenue first", "what could
  cause that?". A real candidate answers the prompt with a structure or hypotheses before
  asking anything else; guided cases only hand over their data that way.
- Search-engine queries ("Acme fuel cost per block hour FY2024-26") are for the web, not the
  client: in case mode they are never sent to the interviewer.
- A question already asked (or close to it) is not asked again.
"""

import re

_STOP = set("the a an of and or to in on for is are what how does do its it with by vs be at "
            "from this that which their as was were has have did can will would per any our "
            "we you your i me my there these those about into than then".split())

# a sentence that hands the turn to the candidate: a question, or an instruction to analyse
# (often after an acknowledgement: "Sure, go ahead.", "Correct, proceed with volume.")
_ACK = (r"(?:(?:sure|yes|no|ok|okay|correct|right|good|great|fine|alright|that's (?:correct|"
        r"right|fine|fair))\W+)*")
_DIRECTIVE = re.compile(
    rf"^\W*{_ACK}(?:please|kindly|let's|let us|go ahead|proceed|focus|start|begin|now\b|think|"
    r"walk me|tell me|take|move on|continue|consider|look at|analy[sz]e|estimate|calculate|"
    r"identify|suggest|explain|list|try|you can|you may|you should|can you|could you|"
    r"why don't you)\b"
    r"|\b(?:go ahead|please proceed|let's (?:focus|look|move|explore|analy[sz]e|discuss|"
    r"start|go)|focus on|can you|could you|how (?:will|would) you|what would you|"
    r"i'd like you to|you (?:can|may|should) (?:now|proceed|go|focus|look|start))\b", re.I)
# how a candidate opens a turn that isn't a search query
_SPOKEN = re.compile(r"^\W*(?:i|i'?d|i'?m|we|let|could|can|would|should|please|so|ok|okay|"
                     r"first|firstly|now|next|then|also|additionally|"
                     r"great|thanks|thank|understood|that|this|my|our|yes|no|sure|right|good|"
                     r"got|before|to|if|given|since|based|is|are|do|does|did|has|have|how|what|"
                     r"why|which|when|where|who)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+|\n+", text.strip()) if s.strip()]


def prompts_candidate(reply: str) -> bool:
    """True if the interviewer's reply asks the candidate something or tells them what to
    analyse next (so the next turn should answer it, not fire another data question)."""
    sents = _sentences(reply.replace("’", "'").replace("‘", "'"))
    return any(s.endswith("?") or _DIRECTIVE.search(s) for s in sents)


def search_style(question: str) -> bool:
    """A web-search query rather than something said to a client: one short phrase, no
    question mark, not phrased as speech (e.g. 'Acme average fare by route 2024-2026')."""
    q = question.strip().replace("’", "'")
    return (bool(q) and not q.endswith("?") and len(_sentences(q)) == 1
            and not _SPOKEN.match(q) and len(q.split()) <= 25)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}


def repeats(question: str, asked: list[str], threshold: float = 0.35) -> bool:
    """True if `question` is (close to) one already asked: content-word overlap (Jaccard) >=
    threshold. 0.35 flags 1 of 291 real candidate turns (train interviews outside the eval
    cases) and 9 of the 72 questions of the first Claude run."""
    q = _words(question)
    return bool(q) and any(len(q & (a := _words(x))) / len(q | a) >= threshold for x in asked)
