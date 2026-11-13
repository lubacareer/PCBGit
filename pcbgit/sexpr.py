"""Shared, non-evaluating KiCad S-expression parser."""
import re

_TOKEN = re.compile(r'\s+|;[^\n]*|[()]|"(?:\\.|[^"\\])*"|[^\s();"]+')



def _unquote(token: str) -> str:
    """Decode KiCad quoted strings as UTF-8, including byte escapes."""
    raw = token[1:-1]
    output = bytearray()
    index = 0
    simple = {'"': 34, '\\': 92, 'a': 7, 'b': 8, 'f': 12,
              'n': 10, 'r': 13, 't': 9, 'v': 11}
    while index < len(raw):
        ch = raw[index]
        if ch != '\\':
            output.extend(ch.encode('utf-8'))
            index += 1
            continue
        index += 1
        if index >= len(raw):
            raise ValueError("unterminated quoted string escape")
        esc = raw[index]
        if esc in simple:
            output.append(simple[esc])
            index += 1
        elif esc == 'x':
            index += 1
            end = index
            while end < len(raw) and end - index < 2 and raw[end] in '0123456789abcdefABCDEF':
                end += 1
            output.append(int(raw[index:end], 16) if end > index else ord('x'))
            index = end
        elif esc in '01234567':
            end = index
            while end < len(raw) and end - index < 3 and raw[end] in '01234567':
                end += 1
            output.append(int(raw[index:end], 8) & 0xff)
            index = end
        else:
            # KiCad retains an unrecognized backslash and processes the next character.
            output.append(ord('\\'))
    try:
        return output.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError("quoted string contains invalid UTF-8 escapes") from exc



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
            stack[-1].append(_unquote(token) if token.startswith('"') else token)
    if pos != len(text) or stack or len(roots) != 1:
        raise ValueError("incomplete S-expression")
    return roots[0]

