"""Parsues i vogël i konfigurimeve nginx të repo-së për teste statike (pa nginx): server-a, listen, location-e (edhe të emërtuara)."""

import re
from dataclasses import dataclass


def strip_comments(text: str) -> str:
    return re.sub(r"(?m)^\s*#.*$", "", text)


def block_end(text: str, start: int) -> int:
    """Indeksi pas `}` që mbyll blokun e hapur para `start`; thonjëzat (p.sh. JSON te `return`) nuk numërohen."""
    depth, i, quote = 1, start, None
    while depth:
        c = text[i]
        if quote:
            quote = None if c == quote else quote
        elif c in ("'", '"'):
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i


@dataclass
class Location:
    spec: str
    body: str

    @property
    def proxied(self) -> bool:
        return "proxy_pass" in self.body

    def headers(self) -> list[str]:
        return re.findall(r"add_header\s+([A-Za-z-]+)\s", self.body)


@dataclass
class Server:
    port: int
    body: str
    locations: list[Location]

    def loc(self, spec_part: str) -> Location:
        found = [x for x in self.locations if spec_part in x.spec]
        assert len(found) == 1, (spec_part, [x.spec for x in self.locations])
        return found[0]

    def server_level(self) -> str:
        """Trupi i serverit pa bllokun e location-eve (direktivat e nivelit server)."""
        out, i = [], 0
        for m in re.finditer(r"(?m)^\s*location\s+[^{\n]+\{", self.body):
            if m.start() < i:
                continue
            out.append(self.body[i : m.start()])
            i = block_end(self.body, m.end())
        out.append(self.body[i:])
        return "".join(out)


def parse(text: str) -> tuple[str, list[Server]]:
    """(pjesa http-level, serverat)."""
    text = strip_comments(text)
    servers, http_parts, i = [], [], 0
    for m in re.finditer(r"(?m)^server\s*\{", text):
        if m.start() < i:
            continue
        http_parts.append(text[i : m.start()])
        end = block_end(text, m.end())
        body = text[m.end() : end - 1]
        locs = []
        for lm in re.finditer(r"(?m)^\s*location\s+([^{\n]+?)\s*\{", body):
            lend = block_end(body, lm.end())
            locs.append(Location(lm.group(1).strip(), body[lm.end() : lend - 1]))
        port = int(re.search(r"listen\s+(\d+)", body).group(1))
        servers.append(Server(port, body, locs))
        i = end
    http_parts.append(text[i:])
    return "".join(http_parts), servers
