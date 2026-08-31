#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "pyobjc-framework-CoreData", "pyobjc-framework-Cocoa"]
# ///
"""Sync recipes between Mela and an Obsidian vault.

Mela keeps its library in a Core Data store that NSPersistentCloudKitContainer mirrors to iCloud. Reading it is straightforward. Writing has two routes: `.melarecipe` JSON that Mela imports, which cannot update a recipe it already holds, and — with `push --write` — a Core Data save against the store itself, which can. That save records the same persistent history Mela's own edits do, and the mirror exports it on Mela's next launch under Mela's entitlements.

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
MOMD = Path("/Applications/Mela.app/Contents/Resources/Mela.momd")
VAULT = Path.home() / "vault/Recipes"
OUTBOX = Path.home() / "Documents/Mela Outbox"
LOG_DIRNAME = "Cook Log"
BACKUPS = Path.home() / "Documents/Mela Backups"
AGENT = Path.home() / "Library/LaunchAgents/com.lenorakepler.melasync.plist"

CORE_DATA_EPOCH = 978307200  # 2001-01-01 UTC, in Unix seconds
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

SECTIONS = ["Ingredients", "Instructions", "Notes", "Nutrition"]

# A cook-log entry is a `### Made <date>` heading in the vault and a bold inline `**Made <date>:**` run in Mela — a heading is the right structure for a note and reads badly in Mela's spacing, and the two sides already differ, so each gets the form that suits it.
LOG_HEADING = re.compile(r"^###[ \t]*Made[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*:?[ \t]*$", re.M)
LOG_BOLD = re.compile(r"^\*\*Made[ \t]+(\d{4}-\d{2}-\d{2})[ \t]*:?\*\*[ \t]*", re.M)
EMBED = re.compile(r"^!\[\[([^\]|#]+?)\]\]$")
# What a legacy hand-written entry looks like, for `split-logs` to migrate.
LEGACY = re.compile(r"^(?:\*\*)?\s*(?:Made|Cooked|Baked)?\s*[:]?\s*(\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|\d{4}-\d{2}-\d{2})\s*[:.\-\u2013]\s*(?:\*\*)?\s*", re.I)


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

        # ZDATA is a tagged blob: a leading 0x01 means the image bytes follow inline, 0x02 that the next 36 characters name a file under _EXTERNAL_DATA, where Core Data spills the larger ones. Writing the blob out whole produces a file with a stray tag byte in front of the JPEG, or one containing nothing but a UUID — which is what "the images are corrupted" looks like.
        images: dict[int, list[Path | bytes]] = {}
        for row in db.execute("SELECT ZRECIPE pk, ZDATA data FROM ZRECIPEIMAGEOBJECT ORDER BY ZINDEX"):
            pk, blob = row["pk"], row["data"]
            if not pk or not blob:
                continue
            blob = bytes(blob)
            if blob[0] == 2:
                path = EXTERNAL / blob[1:37].decode(errors="replace")
                if path.is_file():
                    images.setdefault(pk, []).append(path)
            elif blob[0] == 1:
                images.setdefault(pk, []).append(blob[1:])

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


def matching_model_url():
    """Find the model version in Mela's bundle whose hashes match this store.

    Loading the wrong one either fails or, worse, migrates the store. Core Data records the hashes it was created with, so match on those rather than trusting `NSManagedObjectModel_CurrentVersionName` — the current version is only the right one until Mela ships a new one.
    """
    db = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    store_hashes = plistlib.loads(bytes(db.execute("SELECT Z_PLIST FROM Z_METADATA").fetchone()[0]))["NSStoreModelVersionHashes"]
    db.close()
    info = plistlib.loads((MOMD / "VersionInfo.plist").read_bytes())
    for name, hashes in info["NSManagedObjectModel_VersionHashes"].items():
        if hashes == store_hashes:
            return MOMD / f"{name}.mom"
    sys.exit("No model version in Mela.app matches this store — Mela has probably been updated. Refusing to guess.")


def write_to_mela(recipes, dry_run=False):
    """Update Mela's own store through Core Data, which is what makes edits sync.

    Mela's import cannot update an existing recipe, but its CloudKit mirror does not care who wrote a change: it reads Core Data's persistent history and exports anything newer than what it last sent. A save made here records the same ATRANSACTION and ACHANGE rows Mela's own edit does, with no process identity attached to either, so Mela picks it up on next launch and uploads it under its own entitlements.

    Raw SQL would not do — it changes the row while writing no history, so the mirror never learns and the edit is silently local.
    """
    import CoreData
    import Foundation

    if subprocess.run(["pgrep", "-x", "Mela"], capture_output=True).returncode == 0:
        sys.exit("Quit Mela first. It holds the store open, and a change it never observed can be lost when it next saves.")

    model = CoreData.NSManagedObjectModel.alloc().initWithContentsOfURL_(
        Foundation.NSURL.fileURLWithPath_(str(matching_model_url())))
    container = CoreData.NSPersistentContainer.alloc().initWithName_managedObjectModel_("Curcuma", model)
    desc = CoreData.NSPersistentStoreDescription.alloc().initWithURL_(
        Foundation.NSURL.fileURLWithPath_(str(STORE)))
    desc.setOption_forKey_(Foundation.NSNumber.numberWithBool_(True), CoreData.NSPersistentHistoryTrackingKey)
    # A silent migration of a live library is the one outcome worth crashing to avoid.
    desc.setShouldMigrateStoreAutomatically_(False)
    desc.setShouldInferMappingModelAutomatically_(False)
    container.setPersistentStoreDescriptions_([desc])

    failed = []
    container.loadPersistentStoresWithCompletionHandler_(lambda store, error: error and failed.append(error))
    if failed:
        sys.exit(f"Could not open Mela's store: {failed[0]}")
    ctx = container.viewContext()

    def fetch_one(entity, key, value):
        request = CoreData.NSFetchRequest.fetchRequestWithEntityName_(entity)
        request.setPredicate_(Foundation.NSPredicate.predicateWithFormat_(f"{key} == %@", value))
        found, _ = ctx.executeFetchRequest_error_(request, None)
        return found[0] if found else None

    updated = created = 0
    for recipe in recipes:
        row = fetch_one("RecipeObject", "id", recipe.id)
        if row is None:
            row = CoreData.NSEntityDescription.insertNewObjectForEntityForName_inManagedObjectContext_("RecipeObject", ctx)
            row.setValue_forKey_(recipe.id, "id")
            row.setValue_forKey_(Foundation.NSDate.date(), "date")
            created += 1
        else:
            updated += 1
        for key, value in (("title", recipe.title), ("text", recipe.text), ("link", recipe.link),
                           ("yield", recipe.yield_), ("prepTime", recipe.prep_time),
                           ("cookTime", recipe.cook_time), ("totalTime", recipe.total_time),
                           ("ingredients", recipe.ingredients), ("instructions", recipe.instructions),
                           ("notes", recipe.notes), ("nutrition", recipe.nutrition)):
            row.setValue_forKey_(value, key)
        if recipe.categories:
            tags = [fetch_one("RecipeTag", "title", name)
                    or CoreData.NSEntityDescription.insertNewObjectForEntityForName_inManagedObjectContext_("RecipeTag", ctx)
                    for name in recipe.categories]
            for tag, name in zip(tags, recipe.categories):
                tag.setValue_forKey_(name, "title")
            row.setValue_forKey_(Foundation.NSSet.setWithArray_(tags), "tags")

    if dry_run:
        ctx.rollback()
        print(f"  would update {updated} and create {created} (rolled back)")
        return updated, created

    ok, error = ctx.save_(None)
    if not ok:
        sys.exit(f"Save failed, nothing written: {error}")
    return updated, created


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
            # Drop the list marker, and a task checkbox with it: `- [x] 2 eggs` is a cooking state, not an ingredient called "[x] 2 eggs".
            lines.append(re.sub(r"^(?:[-*+]|\d+\.)\s+(?:\[[ xX/-]\]\s*)?", "", line))
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
        ("Notes", recipe.notes, lambda b: collapse_notes(b).strip()),
        ("Nutrition", recipe.nutrition, lambda b: b.strip()),
    ):
        if block.strip():
            parts.append(f"## {heading}\n\n{render(block).strip()}\n")

    return "\n".join(parts).strip() + "\n"


def log_dir(vault: Path) -> Path:
    return vault / LOG_DIRNAME


def expand_notes(text: str, vault: Path) -> str:
    """Turn the vault's Notes section into the flat string Mela stores.

    An embed becomes the log note's text, headed by the bold run Mela renders tidily; a `### Made` heading written inline becomes the same thing. Everything else — the storage tips and quoted reviews that make up most of these fields — is passed through untouched.
    """
    lines = []
    for line in text.split("\n"):
        embed = EMBED.match(line.strip())
        if not embed:
            lines.append(line)
            continue
        entry = log_dir(vault) / f"{embed.group(1).strip()}.md"
        if not entry.exists():
            lines.append(line)
            continue
        meta, body = parse_note(entry.read_text(encoding="utf-8"))
        date = meta.get("date")
        body = body.strip()
        lines.append(f"**Made {date}:** {body}" if date else body)
    joined = "\n".join(lines)
    # A heading owns the text under it, so pull that text up onto the bold run rather than leaving a stranded line.
    return LOG_HEADING.sub(lambda m: f"**Made {m.group(1)}:**", joined).replace(":**\n\n", ":** ").replace(":**\n", ":** ")


def collapse_notes(text: str) -> str:
    """The reverse, for a Notes field arriving from Mela: bold run back to a heading."""
    return LOG_BOLD.sub(lambda m: f"### Made {m.group(1)}\n\n", text)


def parse_note(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, text[match.end():]


def note_to_recipe(meta: dict, body: str, vault: Path | None = None) -> Recipe:
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
        notes=(expand_notes(sections.get("Notes", ""), vault) if vault else sections.get("Notes", "")).strip(),
        nutrition=sections.get("Nutrition", "").strip(),
        categories=list(meta.get("categories") or []),
        favorite=bool(meta.get("favorite")),
        want_to_cook=bool(meta.get("wantToCook")),
        date=float(meta.get("mela_date_raw") or 0.0),
    )


def digest(body: str) -> str:
    return hashlib.sha256(body.strip().encode()).hexdigest()[:16]


def image_suffix(head: bytes) -> str:
    """Mela stores whatever the source served — a third of these are not JPEG."""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"\x89PNG":
        return ".png"
    if head[4:12].startswith(b"ftyp"):
        return ".heic"
    if head[:4] == b"GIF8":
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ".img"


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
        # Two rows sharing an id are one recipe as far as everything downstream is concerned, and writing both means each overwrites the other on every run.
        if recipe.id in claimed.values():
            continue
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
            if not existing_meta.get("mela_hash") or digest(existing_body) != existing_meta["mela_hash"]:
                # Edited in Obsidian since the last sync — or missing the hash entirely, which says the same thing less precisely. Overwriting either would silently discard work.
                conflicts += 1
                if not args.force:
                    print(f"  conflict (edited in Obsidian, not overwritten): {path.name}")
                    continue

        image = None
        if args.images and recipe.images:
            attachments.mkdir(parents=True, exist_ok=True)
            source = recipe.images[0]
            data = source.read_bytes() if isinstance(source, Path) else source
            image = f"{safe_name(recipe.title)}{image_suffix(data[:16])}"
            (attachments / image).write_bytes(data)
            image = f"attachments/{image}"

        body = body_for(recipe, image)
        # Mela has learnt nothing since we last handed it this text, so keep the vault's own version of the body — embeds and all — and let only the frontmatter be refreshed below.
        preserved = existing_meta.get("mela_sent") == digest(body)
        if preserved:
            body = existing_body
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
        if existing_meta.get("mela_sent"):
            meta["mela_sent"] = existing_meta["mela_sent"]
        if image:
            meta["image"] = image

        # Mela records that a recipe is flagged, never when it was flagged, so a "want to cook" list can only be ordered by when the recipe was added. Stamp the first sync that sees the flag set and keep that date for as long as it stays set. Keyed on whether the stamp exists rather than on the flag changing, so a flag flipped from a card in Obsidian is stamped on the next pull just the same.
        for flag, stamp in (("favorite", "favorite_since"), ("wantToCook", "wantToCook_since")):
            if meta[flag]:
                meta[stamp] = existing_meta.get(stamp) or datetime.now().date().isoformat()
        if existing_body.strip() == body.strip() and dict(existing_meta) == meta:
            skipped += 1
            continue
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10**9)
        path.write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
        written += 1

    print(f"pull: {written} written, {skipped} unchanged, {conflicts} with local edits -> {vault}")


def push(args):
    """Stage notes as .melarecipe files for Mela to import.

    Mela's import is add-only, and `id` is the key it dedupes on: importing an id it already holds does nothing at all — no update, no duplicate, no warning. So an edit to a recipe Mela already has cannot be delivered as an edit. Staging it anyway would produce a file that imports "successfully" and changes nothing, which is the worst of the options.
    """
    args.outbox.mkdir(parents=True, exist_ok=True)
    known = {r.id for r in read_library()}
    staged, blocked, direct = [], [], []

    for path in sorted(args.vault.glob("*.md")):
        if args.only and args.only.lower() not in path.name.lower():
            continue
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        mela_id = str(meta.get("mela_id") or "")
        if mela_id and meta.get("mela_hash") == digest(body) and not args.all:
            continue
        if not mela_id and not args.new:
            continue

        recipe = note_to_recipe(meta, body, args.vault)
        recipe.title = recipe.title or path.stem
        if args.write:
            if not recipe.id:
                recipe.id = f"obsidian-{digest(path.stem + body)}"
            direct.append((path, recipe))
            continue
        if mela_id in known and not args.as_new:
            blocked.append(path)
            continue
        if not recipe.id or args.as_new:
            recipe.id = f"obsidian-{digest(path.stem + body)}"
        out = args.outbox / f"{safe_name(recipe.title)}.melarecipe"
        out.write_text(json.dumps(to_melarecipe(recipe), ensure_ascii=False, indent=2), encoding="utf-8")
        staged.append(out)

    if args.write:
        if not direct:
            print("push: nothing to write")
            return
        print(f"push --write: {len(direct)} recipe(s) into Mela's own store")
        for path, _ in direct[:10]:
            print(f"  {path.name}")
        if len(direct) > 10:
            print(f"  ... and {len(direct) - 10} more")
        if not args.dry_run:
            # A write to the live library is worth 70 MB of insurance every time, not only the first time someone remembers. One slot, overwritten — the useful copy is the one from immediately before this write, and a pile of them is just disk.
            snapshot = BACKUPS / "pre-write"
            shutil.rmtree(snapshot, ignore_errors=True)
            snapshot.mkdir(parents=True, exist_ok=True)
            for p in sorted(GROUP.glob("Curcuma.sqlite*")):
                if p.is_file():
                    shutil.copy2(p, snapshot / p.name)
            print(f"  backed the store up to {snapshot}")
        updated, created = write_to_mela([r for _, r in direct], dry_run=args.dry_run)
        if not args.dry_run:
            for path, recipe in direct:
                meta, body = parse_note(path.read_text(encoding="utf-8"))
                meta["mela_id"] = recipe.id
                meta["mela_hash"] = digest(body)
                # What Mela holds now. A pull that regenerates this same text has learnt nothing new, and must leave the note — and its embeds — alone.
                meta["mela_sent"] = digest(body_for(recipe, meta.get("image")))
                front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10**9)
                path.write_text(f"---\n{front}---\n\n{body.lstrip()}", encoding="utf-8")
            print(f"  updated {updated}, created {created}")
            print("  launch Mela and leave it frontmost; it exports the change on next run")
        return

    print(f"push: {len(staged)} staged -> {args.outbox}")
    for path in staged:
        print(f"  {path.name}")
    if blocked:
        print(f"\n  {len(blocked)} edited note(s) Mela will not accept, because it already holds that id:")
        for path in blocked[:10]:
            print(f"    {path.name}")
        if len(blocked) > 10:
            print(f"    ... and {len(blocked) - 10} more")
        print("  Mela's import cannot update an existing recipe. Either edit it in Mela, or")
        print("  re-run with --as-new to import a second copy under a fresh id and delete the old one there.")
    if staged and args.open:
        subprocess.run(["open", "-a", "Mela", *[str(p) for p in staged]], check=False)
        print("\n  handed to Mela; confirm each import, then re-run `pull` to pick up the new ids")
    elif staged:
        print("\n  run again with --open to hand them to Mela, or drag them onto its window")


def iso_date(text: str) -> str | None:
    """Normalise the date shapes actually present in the notes. A two-digit year is 20xx — these are cooking logs, not archaeology."""
    text = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", text)
    if not m:
        return None
    month, day, year = m.group(1), m.group(2), m.group(3)
    if year is None:
        return None  # A year-less entry cannot be dated; leave it for a human.
    year = int(year)
    year += 2000 if year < 100 else 0
    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def split_logs(args):
    """Lift dated entries out of each recipe's Notes into their own notes, embedded back in.

    Only lines that open with a date-shaped marker are touched. The storage tips, make-ahead instructions and quoted reviews that make up most of these fields have no such marker and are left exactly where they are.
    """
    logs = log_dir(args.vault)
    moved = skipped_undated = 0
    touched = []

    for path in sorted(args.vault.glob("*.md")):
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        if "## Notes" not in body:
            continue
        head, _, rest = body.partition("## Notes\n")
        notes, sep, tail = rest.partition("\n## ")

        out, entries = [], []
        for line in notes.split("\n"):
            m = LEGACY.match(line)
            if not m:
                out.append(line)
                continue
            date = iso_date(m.group(1))
            if not date:
                skipped_undated += 1
                out.append(line)
                continue
            entries.append((date, line[m.end():].strip()))
            out.append(f"!!ENTRY{len(entries) - 1}!!")

        if not entries:
            continue

        for i, (date, text) in enumerate(entries):
            name = f"{date} {safe_name(path.stem)}"
            out = [f"![[{name}]]" if line == f"!!ENTRY{i}!!" else line for line in out]
            if not args.dry_run:
                logs.mkdir(parents=True, exist_ok=True)
                front = yaml.safe_dump({"recipe": f"[[{path.stem}]]", "date": date, "tags": ["cook-log"]},
                                       sort_keys=False, allow_unicode=True, width=10**9)
                (logs / f"{name}.md").write_text(f"---\n{front}---\n\n{text}\n", encoding="utf-8")
            moved += 1

        touched.append((path.name, len(entries)))
        if not args.dry_run:
            new_body = head + "## Notes\n" + "\n".join(out) + (sep + tail if sep else "")
            meta["mela_hash"] = digest(new_body)
            # The body we started from is, by construction, what Mela holds. Without this the next pull rebuilds that text and the embeds are gone.
            meta["mela_sent"] = digest(body)
            front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10**9)
            path.write_text(f"---\n{front}---\n\n{new_body.lstrip()}", encoding="utf-8")

    for name, count in touched:
        print(f"  {count} entr{'ies' if count > 1 else 'y '}  {name}")
    print(f"split-logs: {moved} entr{'ies' if moved != 1 else 'y'} from {len(touched)} recipe(s) -> {logs}"
          f"{' (dry run)' if args.dry_run else ''}")
    if skipped_undated:
        print(f"  {skipped_undated} dated line(s) had no year and were left in place")


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
    p.add_argument("--as-new", dest="as_new", action="store_true", help="mint a fresh id so an edited recipe imports as a second copy")
    p.add_argument("--write", action="store_true", help="write into Mela's store directly, updating recipes in place (Mela must be quit)")
    p.add_argument("--dry-run", action="store_true", help="with --write, do the work and roll it back")
    p.add_argument("--only", help="restrict to notes whose filename contains this")
    p.add_argument("--open", action="store_true", help="hand the staged files to Mela")
    p.set_defaults(func=push)

    p = sub.add_parser("split-logs", help="lift dated Notes entries into their own cook-log notes")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=split_logs)

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
