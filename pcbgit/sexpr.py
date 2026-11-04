"""Shared, non-evaluating KiCad S-expression parser."""
import re

_TOKEN = re.compile(r'\s+|;[^\n]*|[()]|"(?:\\.|[^"\\])*"|[^\s();"]+')


def _parse(text: str) -> list:
    """Parse KiCad's S-expressions without evaluating input or expanding symbols."""
    stack: list[list] = []
    roots: list[list] = []
    pos = 0
    for match in _TOKEN.finditer(text):
        if match.start() != pos:
            raise ValueError("invalid S-expression token")
        pos = match.end()
        token = match.group()
        if token.isspace() or token.startswith(";"):
            continue
        if token == "(":
            if len(stack) >= 128:
                raise ValueError("S-expression nesting exceeds 128")
            node: list = []
            if stack:
                stack[-1].append(node)
            else:
                roots.append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("unexpected closing parenthesis")
            stack.pop()
        elif not stack:
            raise ValueError("token outside S-expression")
        else:
            stack[-1].append(token[1:-1] if token.startswith('"') else token)
    if pos != len(text) or stack or len(roots) != 1:
        raise ValueError("incomplete S-expression")
    return roots[0]

