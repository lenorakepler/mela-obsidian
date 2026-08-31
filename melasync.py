#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Sync recipes between Mela and an Obsidian vault.

Mela keeps its library in a Core Data store that NSPersistentCloudKitContainer mirrors to iCloud. Reading it is safe and complete; writing to it behind Mela's back would desynchronise that mirror, so this only ever reads. The write direction goes out as `.melarecipe` JSON — the format Mela documents and imports — and Mela pulls it in.

Each note records the hash of the body it was last synced with, which is what makes the two directions safe to run repeatedly: a note whose body no longer matches its hash has been edited in Obsidian, so a pull leaves it alone and a push sends it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

GROUP = Path.home() / "Library/Group Containers/66JC38RDUD.recipes.mela/Data"
STORE = GROUP / "Curcuma.sqlite"
EXTERNAL = GROUP / ".Curcuma_SUPPORT/_EXTERNAL_DATA"
VAULT = Path.home() / "vault/Recipes"
OUTBOX = Path.home() / "Documents/Mela Outbox"
BACKUPS = Path.home() / "Documents/Mela Backups"
AGENT = Path.home() / "Library/LaunchAgents/com.lenorakepler.melasync.plist"

CORE_DATA_EPOCH = 978307200  # 2001-01-01 UTC, in Unix seconds
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

SECTIONS = ["Ingredients", "Instructions", "Notes", "Nutrition"]


@dataclass
class Recipe:
    id: str
    title: str
    text: str = ""
    link: str = ""
    yield_: str = ""
    prep_time: str = ""
    cook_time: str = ""
    total_time: str = ""
    ingredients: str = ""
    instructions: str = ""
    notes: str = ""
    nutrition: str = ""
    categories: list[str] = field(default_factory=list)
    favorite: bool = False
    want_to_cook: bool = False
    date: float = 0.0
    images: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------- Mela side


def read_library() -> list[Recipe]:
    """Read every recipe from a snapshot of Mela's store.

    Copied first, and opened read-only even then: the live file is normally mid-WAL and may be open by Mela. The -wal and -shm files have to come along or recent edits are invisible.
    """
    if not STORE.exists():
        sys.exit(f"No Mela library at {STORE}. Is Mela installed and has it synced?")
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "Curcuma.sqlite"
        for suffix in ("", "-wal", "-shm"):
            source = STORE.with_name(STORE.name + suffix)
            if source.exists():
                shutil.copy2(source, snapshot.with_name(snapshot.name + suffix))
        db = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row

        tags: dict[int, list[str]] = {}
        for row in db.execute(
            "SELECT j.Z_4RECIPES pk, t.ZTITLE title FROM Z_4TAGS j"
            " JOIN ZRECIPETAG t ON t.Z_PK = j.Z_5TAGS ORDER BY t.ZTITLE"
        ):
            tags.setdefault(row["pk"], []).append(row["title"])

        # Core Data spills a large blob to a file under _EXTERNAL_DATA and leaves the UUID filename in the column, but keeps a small one inline. Both shapes are in this table.
        images: dict[int, list[Path | bytes]] = {}
        for row in db.execute("SELECT ZRECIPE pk, ZDATA data FROM ZRECIPEIMAGEOBJECT ORDER BY ZINDEX"):
            blob, pk = row["data"], row["pk"]
            if not pk or not blob:
                continue
            if isinstance(blob, bytes) and re.fullmatch(rb"[0-9A-Fa-f-]{36}", blob):
                path = EXTERNAL / blob.decode()
                if path.is_file():
                    images.setdefault(pk, []).append(path)
            else:
                images.setdefault(pk, []).append(bytes(blob))

        out = []
        for row in db.execute("SELECT * FROM ZRECIPEOBJECT"):
            pk = row["Z_PK"]
            out.append(Recipe(
                id=row["ZID"] or f"pk-{pk}",
                title=(row["ZTITLE"] or "Untitled").strip(),
                text=row["ZTEXT"] or "",
                link=row["ZLINK"] or "",
                yield_=row["ZYIELD"] or "",
                prep_time=row["ZPREPTIME"] or "",
                cook_time=row["ZCOOKTIME"] or "",
                total_time=row["ZTOTALTIME"] or "",
                ingredients=row["ZINGREDIENTS"] or "",
                instructions=row["ZINSTRUCTIONS"] or "",
                notes=row["ZNOTES"] or "",
                nutrition=row["ZNUTRITION"] or "",
                categories=tags.get(pk, []),
                favorite=bool(row["ZFAVORITE"]),
                want_to_cook=bool(row["ZWANTTOCOOK"]),
                date=row["ZDATE"] or 0.0,
                images=images.get(pk, []),
            ))
        db.close()
        return out


def to_melarecipe(recipe: Recipe) -> dict:
    return {
        "id": recipe.id,
        "title": recipe.title,
        "text": recipe.text,
        "images": [],
        "categories": recipe.categories,
        "yield": recipe.yield_,
        "prepTime": recipe.prep_time,
        "cookTime": recipe.cook_time,
        "totalTime": recipe.total_time,
        "ingredients": recipe.ingredients,
        "instructions": recipe.instructions,
        "notes": recipe.notes,
        "nutrition": recipe.nutrition,
        "link": recipe.link,
        "favorite": recipe.favorite,
        "wantToCook": recipe.want_to_cook,
        "date": recipe.date,
    }


# ------------------------------------------------------------ Markdown side


def bullets(block: str) -> str:
    """Mela's ingredient string: one per line, `#` marking a group header."""
    lines = []
    for line in block.split("\n"):
        line = line.rstrip()
        if not line:
            lines.append("")
        elif line.startswith("#"):
            lines.append(f"### {line.lstrip('#').strip()}")
        else:
            lines.append(f"- {line}")
    return "\n".join(lines)


def unbullets(block: str) -> str:
    lines = []
    for line in block.split("\n"):
        line = line.rstrip()
        if line.startswith("#"):
            lines.append("#" + re.sub(r"^#+\s*", "", line))
        else:
            lines.append(re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line))
    return "\n".join(lines).strip()


def numbered(block: str) -> str:
    out, n = [], 0
    for line in block.split("\n"):
        line = line.rstrip()
        if not line:
            out.append("")
        elif line.startswith("#"):
            out.append(f"### {line.lstrip('#').strip()}")
        else:
            n += 1
            out.append(f"{n}. {line}")
    return "\n".join(out)


def body_for(recipe: Recipe, image: str | None) -> str:
    parts = []
    if image:
        parts.append(f"![[{image}]]\n")
    if recipe.text.strip() and recipe.text.strip() != recipe.title:
        parts.append(recipe.text.strip() + "\n")

    # Two trailing spaces is a markdown hard break, which keeps these on separate lines without a list.
    meta = []
    if recipe.link:
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", recipe.link).split("/")[0])
        meta.append(f"Source: [{host}]({recipe.link})" if recipe.link.startswith("http") else f"Source: {recipe.link}")
    for label, value in (("Servings", recipe.yield_), ("Prep", recipe.prep_time),
                         ("Cook", recipe.cook_time), ("Total", recipe.total_time)):
        if value:
            meta.append(f"{label}: {value}")
    if meta:
        parts.append("  \n".join(meta) + "  \n")

    for heading, block, render in (
        ("Ingredients", recipe.ingredients, bullets),
        ("Instructions", recipe.instructions, numbered),
        ("Notes", recipe.notes, lambda b: b.strip()),
        ("Nutrition", recipe.nutrition, lambda b: b.strip()),
    ):
        if block.strip():
            parts.append(f"## {heading}\n\n{render(block).strip()}\n")

    return "\n".join(parts).strip() + "\n"


def parse_note(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end():]


def note_to_recipe(meta: dict, body: str) -> Recipe:
    """Read a note back into recipe fields, mirroring `body_for`."""
    sections: dict[str, str] = {}
    current, buffer = None, []
    for line in body.split("\n"):
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading and heading.group(1) in SECTIONS:
            if current:
                sections[current] = "\n".join(buffer).strip()
            current, buffer = heading.group(1), []
        else:
            buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    preamble = body.split("\n## ")[0]

    def line_value(label):
        m = re.search(rf"^{label}:\s*(.+?)\s*$", preamble, re.M)
        return m.group(1).strip() if m else ""

    link = line_value("Source")
    if md := re.match(r"\[.*?\]\((.*?)\)", link):
        link = md.group(1)

    description = "\n".join(
        line for line in preamble.split("\n")
        if line.strip() and not re.match(r"^(Source|Servings|Prep|Cook|Total):", line) and not line.startswith("![[")
    ).strip()

    return Recipe(
        id=meta.get("mela_id") or "",
        title=meta.get("title") or "",
        text=description,
        link=link,
        yield_=line_value("Servings"),
        prep_time=line_value("Prep"),
        cook_time=line_value("Cook"),
        total_time=line_value("Total"),
        ingredients=unbullets(sections.get("Ingredients", "")),
        instructions=unbullets(sections.get("Instructions", "")),
        notes=sections.get("Notes", "").strip(),
        nutrition=sections.get("Nutrition", "").strip(),
        categories=list(meta.get("categories") or []),
        favorite=bool(meta.get("favorite")),
        want_to_cook=bool(meta.get("wantToCook")),
        date=float(meta.get("mela_date_raw") or 0.0),
    )


def digest(body: str) -> str:
    return hashlib.sha256(body.strip().encode()).hexdigest()[:16]


def safe_name(title: str) -> str:
    name = ILLEGAL.sub("-", title).strip(" .")
    return (name or "Untitled")[:120]


# --------------------------------------------------------------- Operations


def note_index(vault: Path) -> dict[str, Path]:
    """Map mela_id to the note holding it, so a renamed note is still found."""
    index = {}
    for path in vault.glob("*.md"):
        meta, _ = parse_note(path.read_text(encoding="utf-8"))
        if meta.get("mela_id"):
            index[str(meta["mela_id"])] = path
    return index


def pull(args):
    vault, attachments = args.vault, args.vault / "attachments"
    vault.mkdir(parents=True, exist_ok=True)
    index = note_index(vault)
    written = skipped = conflicts = 0
    claimed: dict[Path, str] = {}
    library = read_library()

    duplicates = len(library) - len({r.id for r in library})
    if duplicates:
        print(f"  note: {duplicates} recipe(s) in Mela share an id with another and collapse into one note")

    for recipe in library:
        path = index.get(recipe.id)
        if path is None:
            base = vault / f"{safe_name(recipe.title)}.md"
            # A file at this path belongs to us only if it carries this recipe's id. A note with no `mela_id` was written by hand and has never been synced, so it is not ours to overwrite — take a different name and leave it alone.
            occupied = claimed.get(base, recipe.id) != recipe.id
            if not occupied and base.exists():
                occupied = parse_note(base.read_text(encoding="utf-8"))[0].get("mela_id") != recipe.id
            # Eighteen titles repeat in this library, and ids are often URLs whose first characters are the same host — so disambiguate on a hash of the whole id, not a prefix of it.
            path = vault / f"{safe_name(recipe.title)} ({digest(recipe.id)}).md" if occupied else base
        claimed[path] = recipe.id

        existing_meta, existing_body = ({}, "")
        if path.exists():
            existing_meta, existing_body = parse_note(path.read_text(encoding="utf-8"))
            if existing_meta.get("mela_hash") and digest(existing_body) != existing_meta["mela_hash"]:
                # Edited in Obsidian since the last sync. Overwriting would silently discard that edit.
                conflicts += 1
                if not args.force:
                    print(f"  conflict (edited in Obsidian, not overwritten): {path.name}")
                    continue

        image = None
        if args.images and recipe.images:
            attachments.mkdir(parents=True, exist_ok=True)
            image = f"{safe_name(recipe.title)}.jpg"
            source = recipe.images[0]
            if isinstance(source, Path):
                shutil.copyfile(source, attachments / image)
            else:
                (attachments / image).write_bytes(source)
            image = f"attachments/{image}"

        body = body_for(recipe, image)
        meta = {
            "title": recipe.title,
            "cssclasses": ["recipe"],
            "categories": recipe.categories,
            "favorite": recipe.favorite,
            "wantToCook": recipe.want_to_cook,
            "mela_id": recipe.id,
            "mela_date": datetime.fromtimestamp(recipe.date + CORE_DATA_EPOCH, timezone.utc).date().isoformat(),
            "mela_date_raw": recipe.date,
            "mela_hash": digest(body),
        }
        if existing_body.strip() == body.strip() and existing_meta.get("mela_hash") == meta["mela_hash"]:
            skipped += 1
            continue
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10**9)
        path.write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
        written += 1

    print(f"pull: {written} written, {skipped} unchanged, {conflicts} with local edits -> {vault}")


def push(args):
    args.outbox.mkdir(parents=True, exist_ok=True)
    staged = []
    for path in sorted(args.vault.glob("*.md")):
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        if not meta.get("mela_id") and not args.new:
            continue
        if meta.get("mela_hash") == digest(body) and not args.all:
            continue
        recipe = note_to_recipe(meta, body)
        recipe.title = recipe.title or path.stem
        if not recipe.id:
            recipe.id = f"obsidian-{digest(path.stem + body)}"
        out = args.outbox / f"{safe_name(recipe.title)}.melarecipe"
        out.write_text(json.dumps(to_melarecipe(recipe), ensure_ascii=False, indent=2), encoding="utf-8")
        staged.append(out)

    print(f"push: {len(staged)} staged -> {args.outbox}")
    for path in staged:
        print(f"  {path.name}")
    if staged and args.open:
        subprocess.run(["open", "-a", "Mela", *[str(p) for p in staged]], check=False)
        print("  handed to Mela; confirm the import, then re-run `pull` to pick up its ids")
    elif staged:
        print("  run again with --open to hand them to Mela, or drag them onto its window")


def status(args):
    library = {r.id: r for r in read_library()}
    notes = {}
    edited = orphan = 0
    for path in sorted(args.vault.glob("*.md")):
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        mela_id = meta.get("mela_id")
        if not mela_id:
            orphan += 1
            print(f"  only in Obsidian: {path.name}")
            continue
        notes[str(mela_id)] = path
        if meta.get("mela_hash") != digest(body):
            edited += 1
            print(f"  edited in Obsidian: {path.name}")
    missing = [r for i, r in library.items() if i not in notes]
    for recipe in missing[:20]:
        print(f"  only in Mela: {recipe.title}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more only in Mela")
    print(f"status: {len(library)} in Mela, {len(notes) + orphan} notes, {edited} edited since sync, {len(missing)} not yet pulled")


def backup(args):
    """Copy the library twice over: byte-exact, and as portable JSON.

    The raw copy restores everything including photos, but only into Mela. The `.melarecipe` files are readable and importable by other apps, which is the copy that still means something if Mela is not around to restore into.
    """
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    target = args.to / stamp
    store_bytes = sum(p.stat().st_size for p in GROUP.glob("Curcuma.sqlite*") if p.is_file())
    image_bytes = sum(p.stat().st_size for p in EXTERNAL.rglob("*") if p.is_file()) if (args.images and EXTERNAL.is_dir()) else 0
    needed = store_bytes + image_bytes

    free = shutil.disk_usage(args.to.parent if args.to.exists() else Path.home()).free
    print(f"backup: {needed / 1e9:.2f} GB to copy, {free / 1e9:.1f} GB free")
    if free < needed * 1.2:
        sys.exit("Not enough free space — pass --to on another volume, or --no-images to skip the photos.")

    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(GROUP.glob("Curcuma.sqlite*")):
        if path.is_file():
            shutil.copy2(path, target / path.name)
    if image_bytes:
        shutil.copytree(EXTERNAL, target / "_EXTERNAL_DATA", dirs_exist_ok=True)

    check = sqlite3.connect(f"file:{target / 'Curcuma.sqlite'}?mode=ro", uri=True)
    verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    count = check.execute("SELECT COUNT(*) FROM ZRECIPEOBJECT").fetchone()[0]
    check.close()
    if verdict != "ok":
        sys.exit(f"Copied store failed its integrity check: {verdict}")

    exported = target / "recipes"
    exported.mkdir(exist_ok=True)
    for recipe in read_library():
        name = f"{safe_name(recipe.title)} ({digest(recipe.id)}).melarecipe"
        (exported / name).write_text(json.dumps(to_melarecipe(recipe), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  store: {store_bytes / 1e6:.0f} MB, integrity ok, {count} recipes")
    if image_bytes:
        print(f"  photos: {image_bytes / 1e6:.0f} MB")
    print(f"  json:  {len(list(exported.iterdir()))} .melarecipe files (no photos inlined)")
    print(f"  -> {target}")


def install_agent(args):
    plist = {
        "Label": "com.lenorakepler.melasync",
        "ProgramArguments": [str(Path(__file__).resolve()), "pull", "--vault", str(args.vault)]
                            + (["--images"] if args.images else []),
        "StartInterval": args.interval,
        "RunAtLoad": True,
        "StandardOutPath": str(Path.home() / "Library/Logs/melasync.log"),
        "StandardErrorPath": str(Path.home() / "Library/Logs/melasync.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    AGENT.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "unload", str(AGENT)], capture_output=True)
    subprocess.run(["launchctl", "load", str(AGENT)], check=True)
    print(f"installed {AGENT}, pulling every {args.interval}s")
    print("Mela's store lives in a group container, so grant Full Disk Access to /bin/launchctl's job if the log shows permission errors")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", type=Path, default=VAULT, help=f"folder of recipe notes (default {VAULT})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pull", help="Mela -> Obsidian")
    p.add_argument("--images", action="store_true", help="also copy the first photo of each recipe")
    p.add_argument("--force", action="store_true", help="overwrite notes edited in Obsidian")
    p.set_defaults(func=pull)

    p = sub.add_parser("push", help="Obsidian -> .melarecipe files for Mela to import")
    p.add_argument("--outbox", type=Path, default=OUTBOX)
    p.add_argument("--all", action="store_true", help="stage every note, not only edited ones")
    p.add_argument("--new", action="store_true", help="include notes that have no mela_id yet")
    p.add_argument("--open", action="store_true", help="hand the staged files to Mela")
    p.set_defaults(func=push)

    p = sub.add_parser("status", help="what differs between the two sides")
    p.set_defaults(func=status)

    p = sub.add_parser("backup", help="copy the Mela library, raw and as portable JSON")
    p.add_argument("--to", type=Path, default=BACKUPS)
    p.add_argument("--no-images", dest="images", action="store_false", help="skip the 900 MB of photos")
    p.set_defaults(func=backup)

    p = sub.add_parser("install-agent", help="run `pull` periodically via launchd")
    p.add_argument("--interval", type=int, default=900)
    p.add_argument("--images", action="store_true")
    p.set_defaults(func=install_agent)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
