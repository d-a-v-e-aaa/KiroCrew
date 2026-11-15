"""The ssh self-target floor's reading of substitution bodies: each one bash runs is judged.

A ``$( )`` or backtick body is a command line bash runs before the outer command, and
the static splice in ``argv_floor_ssh`` reads only its first command (``$(echo hi; ssh
<this host> id)`` splices to ``hi``). So each body bash runs -- unquoted, inside double
quotes, in an unquoted heredoc, nested, or inside a ``bash -c`` / ``sh -c`` / ``eval`` /
here-string payload -- is judged here as a command line of its own, with
``_is_ssh_to_self``. Quote state, comments and heredoc spans come from the shared shell
reader. This reading only adds refusals on top of the line's own verdict.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the ssh floor in
``argv_floor_ssh`` and the frame walk in ``argv_floor_self``, both below it.
"""

from __future__ import annotations

import re

from .argv_floor_self import _quoted_assignment_scripts
from .argv_floor_ssh import (
    _EMPTY_EXPANSION_RE,
    _is_ssh_to_self,
    _may_name_ssh_verb,
    _resolve_line_assignments,
    _splice_static_substitutions,
    _without_quoted_heredoc_text,
)
from .shell_commands import read_commands
from .shell_normalizer import (
    _backtick_closer,
    _iter_shell_chars,
    _matching_close_paren,
    _nested_shell_payloads,
    _opens_comment,
    _self_tokens,
)


def _live_substitutions(text: str) -> "list[tuple[int, int, str]]":
    """``(start, end, body)`` of each outermost ``$( )`` / backtick substitution bash
    runs in *text*, in text order; ``start:end`` spans the opener to the closer.

    Unquoted or inside double quotes; never in single quotes, a comment, after an
    escape, as ``$$(`` or in a quoted heredoc. Heredoc bodies are blanked before the
    scan (as ``_double_quoted_substitution_bodies`` does), and the substitutions of an
    unquoted one are read the way ``shell_commands._body_substitutions`` reads them.
    """
    subs: list[tuple[int, int, str]] = []
    scan = text
    if "<<" in text:
        for doc in [doc for cmd in read_commands(text, heredocs=True) for doc in cmd.heredocs]:
            blank = re.sub("[^\n]", " ", text[doc.start : doc.end])
            scan = scan[: doc.start] + blank + scan[doc.end :]
            if not doc.quoted:
                subs += [
                    (doc.start + s, doc.start + e, body)
                    for s, e, body in _heredoc_substitutions(doc.body)
                ]
    i, state = 0, 0
    while i < len(scan):
        dollars = 0
        for step in _iter_shell_chars(scan[i:], state):
            at = i + step.offset
            dollars = dollars + 1 if step.state != 1 and step.text == "$" else 0
            if step.state == 0 and step.char == "#" and _opens_comment(scan[i:], step.offset):
                newline = scan.find("\n", at)
                i, state = (len(scan) if newline < 0 else newline), 0
                break
            if dollars % 2 and scan.startswith("(", at + 1) and not scan.startswith("((", at + 1):
                end, proven = _matching_close_paren(scan[at + 2 :], 0)
                stop = at + 2 + end if proven else len(scan)
                subs.append((at, stop, text[at + 2 : stop - proven]))
                i, state = stop, step.state
                break
            if step.state != 1 and step.text == "`":
                closer = _backtick_closer(scan, at + 1)
                stop = closer + 1 if closer >= 0 else len(scan)
                subs.append((at, stop, text[at + 1 : stop - (closer >= 0)]))
                i, state = stop, step.state
                break
        else:
            break
    return sorted(subs)


def _heredoc_substitutions(body: str) -> "list[tuple[int, int, str]]":
    """:func:`_live_substitutions` for an unquoted heredoc body, where only a backslash
    escapes (``shell_commands._body_substitutions``, returning spans and text)."""
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            close, proven = _matching_close_paren(body[i + 2 :], 0)
            out.append((i, i + 2 + close, body[i + 2 : i + 2 + close - proven]))
            i += 2 + close
        elif body[i] == "`":
            closer = _backtick_closer(body, i + 1)
            end = len(body) if closer < 0 else closer
            out.append((i, end + 1, body[i + 1 : end]))
            i = end + 1
        else:
            i += 1
    return out


def _line_may_name_ssh_verb(text_lower: str) -> bool:
    """The verb gate on the whole line, read both as written and statically spliced:
    the splice erases a list's later commands, and builds a verb glued from parts."""
    raw = _EMPTY_EXPANSION_RE.sub("", text_lower)
    spliced = _splice_static_substitutions(raw, escaped_too=True)
    return any(_may_name_ssh_verb(_resolve_line_assignments(t)) for t in (raw, spliced))


def _substitution_reaches_self(text_lower: str) -> bool:
    """True if a substitution body that bash runs in *text_lower* reaches this host,
    judged as a command line of its own.

    Each body is judged one level at a time: a nested body that the static splice can
    never resolve (one holding ``$``, a backtick or a paren) is replaced by one word,
    and is judged on its own when its turn comes. So each level is read once, and the
    work grows linearly with the nesting depth.
    """
    if not _line_may_name_ssh_verb(text_lower):
        return False
    seen: set[str] = set()
    work = [(text_lower, False)]  # (text, judge it): the caller judged the line itself
    while work:
        text, judge = work.pop()
        live = "$(" in text or "`" in text
        subs = _live_substitutions(text) if live else []
        view = text
        for start, end, body in reversed(subs):
            if any(c in body for c in "$`()"):
                view = view[:start] + "_" + view[end:]
        if judge and _is_ssh_to_self(view):
            return True
        if live:
            # A payload with no substitution opener holds no body; the line's own
            # frame walk already judges the payload itself.
            fresh = [p for p in _nested_shell_payloads(_self_tokens(text)) if p not in seen]
            seen.update(fresh)
            work += [(p, False) for p in fresh]
        for _start, _end, body in subs:
            body = _without_quoted_heredoc_text(body)
            if body not in seen:
                seen.add(body)
                work.append((body, True))
    return False


def _reaches_self_over_ssh(text_lower: str) -> bool:
    """:func:`_is_ssh_to_self` on *text_lower*, on each quoted assignment script in it,
    read whole (the separator mask would turn a script's own separators into data),
    and on each substitution body bash runs in it."""
    scripts = _quoted_assignment_scripts(text_lower)
    return (
        _is_ssh_to_self(text_lower, own_line=True)
        or any(map(_is_ssh_to_self, scripts))
        or _substitution_reaches_self(text_lower)
    )
