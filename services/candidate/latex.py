"""LaTeX source utilities.

Treated as source code, not prose (skills/04). Two properties drive the design:

* **Offsets must stay exact.** Comments are masked with spaces rather than removed,
  so every character index into the masked text is also valid in the original. The
  M3 patcher addresses spans by offset, and an off-by-N here would corrupt a resume.
* **The template is untouchable.** The preamble is identified and hashed so any
  later edit that disturbs macros or formatting can be detected.
"""

from __future__ import annotations

import re

#: Commands whose brace argument is a section heading.
SECTION_COMMANDS = ("section", "subsection", "subsubsection", "part", "chapter")

#: Commands that introduce a bullet in common resume templates.
ITEM_COMMANDS = ("item", "resumeItem", "resumeSubItem", "cvitem", "cvline")

#: Commands whose arguments together form a role/education heading. Jake Gutierrez,
#: Deedy and moderncv templates all use one of these shapes.
HEADING_COMMANDS = ("resumeSubheading", "resumeProjectHeading", "cventry", "subheading")

_ESCAPES = {
    r"\&": "&",
    r"\%": "%",
    r"\$": "$",
    r"\#": "#",
    r"\_": "_",
    r"\{": "{",
    r"\}": "}",
    r"\textasciitilde": "~",
    r"\textbackslash": "\\",
    r"\ldots": "...",
    r"\dots": "...",
    r"~": " ",
    r"--": "-",
}

#: ``\\`` is an explicit line break in LaTeX, and in a skills block it is the only
#: thing separating "Languages: Python, Go" from "Frameworks: FastAPI". Flattening
#: it to a space merges the groups and silently loses skills at the seams, so it is
#: preserved as a real newline via a sentinel that survives whitespace collapsing.
_LINEBREAK = "\x00"

_COMMAND_WITH_ARG = re.compile(
    r"\\(?:textbf|textit|texttt|emph|underline|textsc|textrm|href|url|mbox|hbox|text)\s*"
)
_BARE_COMMAND = re.compile(r"\\[a-zA-Z@]+\*?")
_WHITESPACE = re.compile(r"\s+")


def mask_comments(source: str) -> str:
    """Replace LaTeX comments with spaces, preserving length and line structure.

    A comment runs from an unescaped ``%`` to end of line. Returning a same-length
    string means offsets computed against the mask are valid in the original.
    """
    chars = list(source)
    index, length = 0, len(chars)
    while index < length:
        char = chars[index]
        if char == "\\":
            # Skip the escaped character, whatever it is.
            index += 2
            continue
        if char == "%":
            while index < length and chars[index] != "\n":
                chars[index] = " "
                index += 1
            continue
        index += 1
    return "".join(chars)


def read_braced_group(text: str, open_index: int) -> tuple[str, int]:
    """Read a balanced ``{...}`` group starting at ``open_index``.

    Returns the inner content and the index just past the closing brace. Raises
    ``ValueError`` if the group never closes, which is the caller's signal that the
    source is malformed or the group spans further than the slice provided.
    """
    if open_index >= len(text) or text[open_index] != "{":
        raise ValueError(f"expected '{{' at offset {open_index}")

    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index], index + 1
        index += 1
    raise ValueError(f"unbalanced brace group starting at offset {open_index}")


def brace_balance(text: str) -> int:
    """Net brace depth of ``text``, ignoring escaped braces."""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return depth


def to_plain_text(fragment: str) -> str:
    """Render a LaTeX fragment as readable text for display and evidence quotes.

    Deliberately lossy but conservative: it unwraps formatting commands, keeps
    their content, and drops commands it does not know. It never invents words.
    """
    # Protect explicit line breaks before anything else can eat them.
    text = fragment.replace("\\\\", _LINEBREAK)

    # \href{url}{label} and \url{u}: keep the human-readable part.
    text = re.sub(r"\\href\s*\{[^}]*\}\s*\{([^}]*)\}", r"\1", text)
    text = re.sub(r"\\url\s*\{([^}]*)\}", r"\1", text)

    # Unwrap formatting commands, innermost first.
    for _ in range(6):
        replaced = _COMMAND_WITH_ARG.sub("", text)
        if replaced == text:
            break
        text = replaced

    # Braced groups become their contents once the command is gone.
    text = text.replace("{", " ").replace("}", " ")

    for pattern, replacement in _ESCAPES.items():
        text = text.replace(pattern, replacement)

    text = _BARE_COMMAND.sub(" ", text)
    text = text.replace("$", "").replace("&", " ")

    # Collapse all runs of whitespace, then restore explicit breaks as newlines and
    # drop any empty lines the removed commands left behind.
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip(" \t-|,") for line in text.split(_LINEBREAK)]
    return "\n".join(line for line in lines if line).strip(" \t-|,\n")


def find_preamble(source: str) -> tuple[str, str]:
    """Split source into ``(preamble, body)`` at ``\\begin{document}``.

    A fragment with no document environment is treated as all body: it has no
    template to protect.
    """
    masked = mask_comments(source)
    match = re.search(r"\\begin\s*\{document\}", masked)
    if match is None:
        return "", source
    return source[: match.end()], source[match.end() :]


def command_argument(masked: str, command: str, start: int) -> tuple[str, int] | None:
    """If ``masked`` has ``\\command{...}`` at ``start``, return its argument.

    Returns ``(argument, end_index)`` or None. Operates on the masked text so a
    commented-out command is never matched.
    """
    pattern = re.compile(rf"\\{re.escape(command)}\*?\s*")
    match = pattern.match(masked, start)
    if match is None:
        return None
    brace = masked.find("{", match.end())
    if brace == -1 or masked[match.end() : brace].strip():
        return None
    try:
        content, end = read_braced_group(masked, brace)
    except ValueError:
        return None
    return content, end
