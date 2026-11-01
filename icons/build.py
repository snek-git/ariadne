"""The ariadne icon theme: pages and folders drawn in NieR's pictogram grammar.

`uv run --no-project python icons/build.py [OUT]` writes the theme to OUT (default
`~/.local/share/icons/Ariadne`) and a preview sheet of every kind to `icons/preview.svg`.

Grayscale only, never palette colours: the fork tints every icon to one ink, so black is full
ink, gray about half, and the empty page lets the field show through. Glyphs sit on a 16 unit
grid. Their grammar is the game's: a head dot over a body of diamonds, chevrons, arcs and an
open ring, one stroke weight, square ends.
"""

import sys
from pathlib import Path

LINE = 'fill="none" stroke="black" stroke-width="1.75" stroke-linecap="square"'
DOT = '<circle cx="8" cy="2.5" r="1.75"/>'
# The page and folder outline: a hairline, as the game's frames (2px at the 64px grid size).
FRAME = 'stroke="black" stroke-width="0.5" stroke-linejoin="miter"'


def diamond(cx, cy, r, filled=True):
    d = f"M{cx} {cy - r} L{cx + r} {cy} L{cx} {cy + r} L{cx - r} {cy} Z"
    return f'<path d="{d}"/>' if filled else f'<path d="{d}" {LINE} stroke-linejoin="miter"/>'


# Each kind's glyph in a 16 by 16 box.
GLYPHS = {
    "blank": "",
    # three lines of text under the head dot
    "text": DOT + '<rect x="2" y="6" width="12" height="1.75"/>'
    '<rect x="2" y="9.5" width="12" height="1.75"/><rect x="2" y="13" width="7" height="1.75"/>',
    # the Skills mark: a bar between two brackets
    "code": DOT + '<rect x="7.1" y="6" width="1.8" height="9"/>'
    f'<path d="M4.5 6.5 L2 10.5 L4.5 14.5 M11.5 6.5 L14 10.5 L11.5 14.5" {LINE}/>',
    # an open world: a ring with its equator
    "html": DOT + f'<circle cx="8" cy="10.5" r="4.25" {LINE}/>'
    f'<path d="M3.75 10.5 H12.25" {LINE}/>' + diamond(8, 10.5, 1.4),
    # the sun is the head dot, set off to the side over a ridge
    "image": '<circle cx="3.5" cy="3.5" r="1.75"/><path d="M1.5 15 L6 7.5 L9 11.5 L11 9 L14.5 15 Z"/>',
    "video": DOT + '<path d="M5 6 L13 10.5 L5 15 Z"/>',
    # a diamond source and two arcs of sound
    "audio": DOT + diamond(3.5, 10.5, 1.75)
    + '<path d="M7 7 Q9.5 10.5 7 14 M10.5 5.5 Q14 10.5 10.5 15.5" fill="none" stroke="black" '
    'stroke-width="1.75"/>',
    # the Map mark's cluster: four things bundled
    "archive": DOT + diamond(8, 7.25, 1.9) + diamond(4.75, 10.5, 1.9) + diamond(11.25, 10.5, 1.9)
    + diamond(8, 13.75, 1.9),
    # a seal: a diamond inside a diamond
    "doc": DOT + diamond(8, 10.5, 4.25, filled=False) + diamond(8, 10.5, 1.6),
    "model": DOT + '<path d="M8 6 L12.5 8.5 L12.5 13 L8 15.5 L3.5 13 L3.5 8.5 Z" fill="none" '
    'stroke="black" stroke-width="1.5" stroke-linejoin="miter"/>'
    '<path d="M8 10.75 L12.5 8.5 L12.5 13 L8 15.5 Z" fill="gray"/>'
    '<path d="M3.5 8.5 L8 10.75 L12.5 8.5 M8 10.75 V15.5" fill="none" stroke="black" stroke-width="1.5"/>',
    # still arriving: the head dot broken into a dashed fall, a chevron, the floor
    "partial": '<rect x="7.1" y="1" width="1.8" height="1.75"/><rect x="7.1" y="4" width="1.8" height="1.75"/>'
    '<path d="M3 7.5 H13 L8 12.5 Z"/><rect x="2" y="13.5" width="12" height="1.75"/>',
    # the System ring, given a bit
    "key": DOT + f'<circle cx="5" cy="10.5" r="2.75" {LINE}/>'
    '<rect x="7.75" y="9.6" width="7" height="1.8"/><rect x="12.5" y="11.4" width="1.8" height="2.4"/>',
    # the fusion shop's chip: a die with its pins
    "binary": f'<rect x="4" y="4" width="8" height="8" {LINE} stroke-linejoin="miter"/>'
    '<rect x="6.5" y="6.5" width="3" height="3"/>'
    + "".join(
        f'<rect x="{a}" y="{b}" width="{w}" height="{h}"/>'
        for p in (5.5, 9)
        for a, b, w, h in ((p, 1, 1.5, 2), (p, 13, 1.5, 2), (1, p, 2, 1.5), (13, p, 2, 1.5))
    ),
    "home": DOT + f'<path d="M2.5 10.5 L8 6 L13.5 10.5" {LINE}/>'
    '<path d="M4 11 H12 V15.5 H9.25 V12.5 H6.75 V15.5 H4 Z"/>',
    "desktop": f'<rect x="2" y="4" width="12" height="8" {LINE} stroke-linejoin="miter"/>'
    + diamond(8, 8, 1.75) + '<rect x="5" y="13.5" width="6" height="1.75"/>',
    "trash": DOT + '<rect x="2" y="5.25" width="12" height="1.75"/>'
    '<path d="M3.5 8 H12.5 L11.5 15.5 H4.5 Z"/>',
}

# Every name the fork may look up, and the glyph it gets.
FILES = {
    "blank": "text-x-generic-template inode-x-empty",
    "text": "text-x-generic text-plain text-markdown text-x-markdown text-x-log text-csv "
    "x-office-spreadsheet application-vnd.oasis.opendocument.text",
    "code": "text-x-script text-x-python text-x-script.python application-x-shellscript "
    "text-x-csrc text-x-chdr text-x-c++src text-x-rust text-x-go application-json "
    "application-javascript text-javascript text-x-makefile application-toml text-x-toml "
    "application-x-yaml text-css application-xml text-x-kotlin text-x-java",
    "html": "text-html application-xhtml+xml",
    "image": "image-x-generic image-png image-jpeg image-gif image-webp image-svg+xml image-bmp "
    "image-tiff image-x-xcf",
    "video": "video-x-generic video-mp4 video-webm video-x-matroska video-quicktime",
    "audio": "audio-x-generic audio-mpeg audio-flac audio-x-wav audio-ogg audio-x-vorbis+ogg "
    "audio-mp4",
    "archive": "package-x-generic application-zip application-vnd.rar application-x-rar "
    "application-x-7z-compressed application-x-tar application-x-compressed-tar application-gzip "
    "application-x-xz application-zstd application-x-bzip2 application-vnd.debian.binary-package "
    "application-x-iso9660-image application-x-cd-image",
    "doc": "x-office-document application-pdf application-epub+zip "
    "application-vnd.openxmlformats-officedocument.wordprocessingml.document",
    "model": "model-x-generic model-stl model-3mf model-obj application-vnd.ms-3mfdocument "
    "application-sla",
    "partial": "application-x-partial-download",
    "key": "text-x-ssl-private-key application-pgp-keys application-x-pem-key "
    "application-x-x509-ca-cert application-pkcs12",
    "binary": "application-octet-stream application-x-executable application-x-generic "
    "application-x-sharedlib application-x-object application-x-desktop",
}
PLACES = {
    "blank": "folder inode-directory folder-templates folder-publicshare folder-remote",
    "partial": "folder-download",
    "text": "folder-documents",
    "image": "folder-pictures",
    "audio": "folder-music",
    "video": "folder-videos",
    "code": "folder-development",
    "home": "user-home",
    "desktop": "user-desktop",
    "trash": "user-trash user-trash-full",
}


# The small marks for the sidebar, as the game's item marks before a name: bare glyphs at 16px,
# one unit a pixel so every bar lands whole. Symbolic icons are one colour (the sidebar inks
# them dark at rest and light on the current place), so cut-outs are holes, never white.
STROKE = 'fill="none" stroke="black" stroke-width="2" stroke-linejoin="miter"'
HEAD = '<circle cx="8" cy="2" r="1.5"/>'


def mark(cx, cy, r):
    return f'<path d="M{cx} {cy - r} L{cx + r} {cy} L{cx} {cy + r} L{cx - r} {cy} Z"/>'


SYMBOLIC = {
    # things arriving: the head dot falls as a chevron into a tray
    "mail-unread": HEAD + '<path d="M4 5 H12 L8 9 Z"/><path d="M1 9 H3 V13 H13 V9 H15 V15 H1 Z"/>',
    # the System ring, open at the top for the head dot, with one hand
    "document-open-recent": HEAD + '<path d="M5 4.4 A5.25 5.25 0 1 0 11 4.4" ' + STROKE + "/>"
    '<rect x="7" y="6" width="2" height="4"/><rect x="7" y="9" width="4" height="2"/>',
    "user-home": HEAD + '<path d="M2 10.5 L8 5.5 L14 10.5" ' + STROKE + "/>"
    '<path d="M3.5 11.5 H12.5 V16 H9.5 V13 H6.5 V16 H3.5 Z"/>',
    "folder-documents": HEAD + '<rect x="2" y="6" width="12" height="2"/>'
    '<rect x="2" y="10" width="12" height="2"/><rect x="2" y="14" width="7" height="2"/>',
    "folder-download": '<rect x="7" y="0" width="2" height="2"/><rect x="7" y="3" width="2" height="2"/>'
    '<path d="M3 6 H13 L8 11 Z"/><rect x="2" y="13" width="12" height="2"/>',
    "folder-music": HEAD + mark(3, 10, 2)
    + '<path d="M6.5 6 Q9 10 6.5 14 M10 4.5 Q13.5 10 10 15.5" fill="none" stroke="black" stroke-width="1.75"/>',
    "folder-pictures": '<circle cx="3.5" cy="3" r="1.75"/><path d="M1 15 L6 7 L9 11 L11 9 L15 15 Z"/>',
    "folder-videos": HEAD + '<path d="M4 5 L13 10 L4 15 Z"/>',
    "folder-development": HEAD + '<rect x="7" y="5" width="2" height="10"/>'
    '<path d="M4.5 5.5 L2 10 L4.5 14.5 M11.5 5.5 L14 10 L11.5 14.5" ' + STROKE + ' stroke-linecap="square"/>',
    "user-desktop": '<rect x="2" y="2" width="12" height="9" ' + STROKE + "/>"
    + mark(8, 6.5, 2) + '<rect x="5" y="13" width="6" height="2"/>',
    # a folder as the big one: a tab plate, its dot triad cut out
    "folder": '<path fill-rule="evenodd" d="M1 3 H6 L7.5 4.5 H15 V14 H1 Z '
    'M10 10 V11 H11 V10 Z M12 10 V11 H13 V10 Z M11 11 V12 H12 V11 Z"/>',
    # the big page: a 2px frame, the corner cut, a solid foot
    "text-x-generic": '<path fill-rule="evenodd" d="M2 0 H10 L14 4 V16 H2 Z M4 2 V11 H12 V5 L9 2 Z"/>',
    # an empty bin is a frame, a full one solid
    "user-trash": HEAD + '<rect x="1" y="5" width="14" height="2"/>'
    '<path fill-rule="evenodd" d="M3 8 H13 V16 H3 Z M5 8 V14 H11 V8 Z"/>',
    "user-trash-full": HEAD + '<rect x="1" y="5" width="14" height="2"/><rect x="3" y="8" width="10" height="8"/>',
    # the Quests cluster, joined
    "network-workgroup": '<path d="M8 3 L3 12 M8 3 L13 12 M3 12 H13" fill="none" stroke="black" '
    'stroke-width="1.25"/>' + mark(8, 3, 2.5) + mark(3, 12, 2.5) + mark(13, 12, 2.5),
    "media-eject": '<path d="M2 9 L8 3 L14 9 Z"/><rect x="2" y="11" width="12" height="2"/>',
}
SYMBOLIC_ALIASES = {
    "folder": "inode-directory folder-templates folder-publicshare folder-remote",
}


def place(kind, x, y, scale, knock=False):
    """The kind glyph at x, y; `knock` draws it in white, as if cut out of a dark body."""
    body = GLYPHS[kind]
    if knock:
        body = body.replace('stroke="black"', 'stroke="white"').replace('fill="gray"', 'fill="white"')
        body = f'<g fill="white">{body}</g>'
    return f'<g transform="translate({x} {y}) scale({scale})">{body}</g>'


def page(kind):
    """A page with its corner cut at 45 degrees, empty so the field shows through, and a solid
    strip along its foot with the list bullet knocked out of it."""
    out = f'<path d="M3.5 1.5 H10.5 L13.5 4.5 V14.5 H3.5 Z" fill="none" {FRAME}/>'
    out += f'<path d="M10.5 1.5 V4.5 H13.5" fill="none" {FRAME}/>'
    out += '<rect x="3.5" y="12" width="10" height="2.5"/>'
    out += '<rect x="4.75" y="12.75" width="1" height="1" fill="white"/>'
    if kind != "blank":
        out += place(kind, 4.5, 3.5, 0.5)
    return out


def folder(kind):
    """A gray plate with a tab and the rail's dot triad; a place carries its glyph cut out."""
    out = f'<path d="M1.5 3.5 H6 L7.5 5 H14.5 V13.5 H1.5 Z" {FRAME} fill="gray"/>'
    out += '<rect x="11" y="10.5" width="1" height="1"/><rect x="13" y="10.5" width="1" height="1"/>'
    out += '<rect x="12" y="11.5" width="1" height="1"/>'
    if kind != "blank":
        out += place(kind, 3.5, 6, 0.4375, knock=True)
    return out


INDEX = """[Icon Theme]
Name=Ariadne
Comment=Pages and folders in NieR's pictogram grammar, for the ariadne fork of COSMIC Files
Inherits=hicolor
Directories=scalable/places,scalable/mimetypes,symbolic

[scalable/places]
Size=64
MinSize=8
MaxSize=512
Type=Scalable
Context=Places

[scalable/mimetypes]
Size=64
MinSize=8
MaxSize=512
Type=Scalable
Context=MimeTypes

[symbolic]
Size=16
MinSize=8
MaxSize=512
Type=Scalable
Context=Places
"""


def svg(body, w=16, h=16, scale=4):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w * scale}" height="{h * scale}">{body}</svg>\n'
    )


def build(out):
    for sub in ("scalable/places", "scalable/mimetypes", "symbolic"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for name, body in SYMBOLIC.items():
        for alias in [name, *SYMBOLIC_ALIASES.get(name, "").split()]:
            (out / "symbolic" / f"{alias}-symbolic.svg").write_text(svg(body, scale=1))
    (out / "index.theme").write_text(INDEX)
    for kind, names in FILES.items():
        for name in names.split():
            (out / "scalable/mimetypes" / f"{name}.svg").write_text(svg(page(kind)))
    for kind, names in PLACES.items():
        for name in names.split():
            (out / "scalable/places" / f"{name}.svg").write_text(svg(folder(kind)))
    # every kind once, files then places, for looking at the set by itself
    cells = [page(k) for k in FILES] + [folder(k) for k in PLACES]
    tiles = "".join(
        f'<g transform="translate({(i % 8) * 20 + 2} {(i // 8) * 20 + 2})">{c}</g>'
        for i, c in enumerate(cells)
    )
    rows = (len(cells) + 7) // 8
    marks = "".join(
        f'<g transform="translate({(i % 8) * 20 + 2} {rows * 20 + (i // 8) * 20 + 2})">{b}</g>'
        for i, b in enumerate(SYMBOLIC.values())
    )
    rows += (len(SYMBOLIC) + 7) // 8
    Path(__file__).with_name("preview.svg").write_text(svg(tiles + marks, 160, rows * 20))


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".local/share/icons/Ariadne")
