"""Build small .HMI projects in the Editor's layout, for tests that need no real HMI."""

import struct

from nextion_tools.hmi import DIRECTORY_ENTRY, PAGE_HEADER, PAGE_TYPE, STRING_ATTRS

DATA_START = 0x1000


def component(name, type_, attrs=None, events=None, cid=None):
    """attrs: {name: int or str}; events: {event: [line, ...]}."""
    return {"name": name, "type": type_, "attrs": dict(attrs or {}), "events": dict(events or {}), "id": cid}


def page(name, components=(), events=None, block=None):
    return {"name": name, "components": list(components), "events": dict(events or {}), "block": block}


def _record(name, value):
    if name in STRING_ATTRS:
        raw = value.encode("utf-8") + b"\0"
    else:
        raw = int(value).to_bytes(4 if name not in ("type", "id", "vscope", "sendkey") else 1, "little")
    body = name.encode("latin1").ljust(16, b"\0") + raw
    return struct.pack("<I", len(body)) + body


def _marker_and_code(event, lines):
    marker = ("codes%s-%d" % (event, len(lines))).encode("latin1")
    out = struct.pack("<I", len(marker)) + marker
    for line in lines:
        raw = line.encode("utf-8")
        out += struct.pack("<I", len(raw)) + raw
    return out


def _component_bytes(c, cid):
    attrs = {"objname": c["name"], "type": c["type"], "id": cid if c["id"] is None else c["id"]}
    attrs.update(c["attrs"])
    out = b"".join(_record(k, v) for k, v in attrs.items())
    out += b"".join(_marker_and_code(e, lines) for e, lines in c["events"].items())
    return out + struct.pack("<I", 0)


def _page_block(p):
    comps = [component(p["name"], PAGE_TYPE, {"vscope": 0}, p["events"])] + p["components"]
    bodies = [_component_bytes(c, i) for i, c in enumerate(comps)]
    table = b""
    offset = 12 * len(bodies)
    for body in bodies:
        table += struct.pack("<III", offset, len(body), 0)
        offset += len(body)
    header = bytearray(PAGE_HEADER)
    total = PAGE_HEADER + len(table) + sum(len(b) for b in bodies)
    struct.pack_into("<III", header, 4, total, PAGE_HEADER, len(bodies))
    header[24:40] = p["name"].encode("latin1").ljust(16, b"\0")
    return bytes(header) + table + b"".join(bodies)


def build(pages, pictures=0, fonts=0, stale=(), program=None):
    """The bytes of a project. Pages are listed in main.HMI in the order given (that is their
    id), but stored under block names in reverse, so a reader that trusts block names is wrong.
    `stale` adds directory entries flagged stale, holding garbage. `program` is Program.s's
    lines."""
    blocks = {}
    if program is not None:
        blocks["Program.s"] = "\r\n".join(program).encode("utf-8") + b"\r\n"
    resources = ["%d.i" % i for i in range(pictures)] + ["%d.zi" % i for i in range(fonts)]
    for i, p in enumerate(pages):
        block_name = p["block"] or "%d.pa" % (len(pages) - 1 - i)
        resources.append(block_name)
        blocks[block_name] = _page_block(p)
    for name in resources:
        blocks.setdefault(name, b"\0" * 16)
    main = bytearray(96 + 16 * len(resources))
    struct.pack_into("<I", main, 28, len(resources))
    for i, name in enumerate(resources):
        main[96 + 16 * i + 8:96 + 16 * i + 16] = name.encode("latin1").ljust(8, b"\0")[:8]
    blocks["main.HMI"] = bytes(main)

    entries = [(name, data, 0) for name, data in blocks.items()]
    entries += [(name, b"garbage", 1) for name in stale]
    directory = struct.pack("<I", len(entries))
    payload = b""
    for name, data, flags in entries:
        directory += name.encode("latin1").ljust(16, b"\0") + struct.pack("<III", DATA_START + len(payload), len(data), flags)
        payload += data
    return directory.ljust(DATA_START, b"\0") + payload
