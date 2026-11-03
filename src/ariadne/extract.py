"""A file as the parts that get embedded: its name always, then its whole text in chunks or its picture."""
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

from . import config

TILE = 480
PIXEL_LIMIT = Image.MAX_IMAGE_PIXELS
Image.MAX_IMAGE_PIXELS = None  # checked per format in parts(): JPEGs decode scaled down


def ext(p: Path) -> str:
    return p.suffix.lower().lstrip(".")


# Types that sit together: a .mov recording belongs with the .mp4 files, a CV in .pdf with the .docx
# ones. Any other type is a family of its own.
FAMILIES = {
    "image": {"png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "heif", "avif", "tiff"},
    "video": {"mp4", "mov", "mkv", "webm", "avi", "m4v"},
    "audio": {"mp3", "wav", "flac", "ogg", "m4a", "opus"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "xz"},
    "doc": {"pdf", "doc", "docx", "odt", "rtf"},
}
FAMILY = {e: f for f, es in FAMILIES.items() for e in es}


def family(p: Path) -> str:
    """'image' for a .heic, 'doc' for a .pdf; any other type is its own extension."""
    return FAMILY.get(ext(p), ext(p))


def name_text(p: Path) -> str:
    """'HD_2250_Albinauric_Mask.3mf' -> 'HD 2250 Albinauric Mask (3mf file)'. The folder stays out:
    routing must judge a file by itself, not by where it already sits."""
    stem = re.sub(r"[_\-.]+", " ", p.stem).strip() or p.name
    e = ext(p)
    return f"{stem} ({e} file)" if e else stem


# Default names cameras, phones and apps give, one or many ('images'): they say nothing about what the file is.
DEFAULT_NAME = (r"(?:img|vid|pxl|dsc[nf]?|mvimg|pano|screenshot(?: from)?|screen ?shot|screen ?recording|photo|image|"
                r"pic|picture|video|audio|recording|voice|untitled|unnamed|noname|scan|document|file|download|new|"
                r"copy|export|output|result|whatsapp (?:image|video|audio)|telegram|signal|pasted image|snapshot|"
                r"capture)s?")


def generic(p: Path) -> bool:
    """'1.stl', '20261031_215145.heic', 'IMG_2041.jpg': a name with no topic word in it. Leave-one-out
    over 139 such files: the vote placed them right 26% of the time, against 80% for other names."""
    stem = re.sub(r"\s*\(\d+\)$", "", p.stem.lower().strip())
    return bool(re.fullmatch(r"[\d\W_]+", stem)
                or re.fullmatch(rf"(?:{DEFAULT_NAME})(?:[\W_]*\d[\d\W_a-z]{{0,3}})*[\d\W_]*", stem))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ("script", "style")

    def handle_endtag(self, tag):
        self.skip -= tag in ("script", "style") and self.skip > 0

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def read_text(p: Path) -> str:
    e = ext(p)
    if e in config.PDF_EXT:
        r = subprocess.run(["pdftotext", "-q", str(p), "-"], capture_output=True, timeout=120)
        return r.stdout.decode(errors="replace")
    s = p.read_text(errors="replace")
    if e in ("html", "htm"):
        h = _Text()
        h.feed(s)
        s = " ".join(h.out)
    return s


def chunks(s: str) -> list[str]:
    s = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", s)).strip()
    out, i = [], 0
    while i < len(s):
        c = s[i:i + config.CHUNK]
        if len(c.strip()) > 20:
            out.append(c)
        i += config.CHUNK - config.OVERLAP
    return out


def kind(p: Path) -> str:
    e = ext(p)
    if e in config.TEXT_EXT or e in config.PDF_EXT:
        return "text"
    if e in config.IMAGE_EXT:
        return "image"
    return "name"


def parts(p: Path) -> list[tuple[str, object]]:
    """[(part kind, payload)]: ('name', str) first, then ('chunk', str)... or ('image', Image)."""
    out: list[tuple[str, object]] = [("name", name_text(p))]
    k = kind(p)
    try:
        if k == "text":
            out += [("chunk", c) for c in chunks(read_text(p))]
        elif k == "image":
            im = Image.open(p)
            if im.format == "JPEG":
                im.draft("RGB", (TILE, TILE))  # decode at 1/2..1/8 scale: a 30000 px JPEG costs 3750 px
            elif im.width * im.height > PIXEL_LIMIT:
                raise ValueError(f"{im.width}x{im.height} is too big to decode")
            im.seek(0)
            out.append(("image", im.convert("RGB")))
    except Exception as err:  # unreadable content: the name still gets indexed
        print(f"content skipped for {p}: {err}", flush=True)
    return out
