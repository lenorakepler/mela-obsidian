# mela-obsidian

Sync recipes between [Mela](https://mela.recipes) and an Obsidian vault, as plain markdown notes.

## How it works, and why it works that way

Mela keeps its library in a Core Data store — `Curcuma.sqlite`, in the group container `66JC38RDUD.recipes.mela` — which `NSPersistentCloudKitContainer` mirrors to iCloud. Reading it is straightforward and gives you everything: the table columns map one-to-one onto the fields Mela [documents for its export format](https://mela.recipes/fileformat/).

Writing to it is a different matter. Editing rows behind Mela's back would leave the CloudKit mirror describing a library that no longer exists, and the failure would surface later, on another device. So this tool never writes to Mela's store. It reads a copy of it, and the return direction goes out as `.melarecipe` files — the JSON format Mela documents and imports.

That asymmetry is the whole design:

| Direction | How | Automatic? |
| --- | --- | --- |
| Mela → Obsidian | Read a snapshot of the Core Data store, write markdown | Yes — `pull`, on a timer if you want |
| Obsidian → Mela | Write `.melarecipe` JSON, hand it to Mela | New recipes only — see below |

## Commands

```bash
./melasync.py backup                     # copy the library before anything else
./melasync.py status                     # what differs between the two sides
./melasync.py pull                       # Mela -> Obsidian
./melasync.py pull --images              # also copy each recipe's first photo
./melasync.py push                       # Obsidian -> .melarecipe files in the outbox
./melasync.py push --open                # ...and hand them to Mela
./melasync.py install-agent              # pull every 15 minutes via launchd
```

The script has a [PEP 723](https://peps.python.org/pep-0723/) header, so `uv run melasync.py` installs its one dependency itself. `--vault` points at the folder of notes; it defaults to `~/vault/Recipes`.

## Backing up first

```bash
./melasync.py backup                     # ~1 GB, to ~/Documents/Mela Backups/<timestamp>/
./melasync.py backup --no-images         # ~70 MB, the store alone
./melasync.py backup --to /Volumes/Ext   # somewhere with more room
```

Two copies, because they fail differently. The raw `Curcuma.sqlite` (plus its `-wal`, `-shm`, and the `_EXTERNAL_DATA` photos) is byte-exact and restores everything, but only into Mela. The `recipes/*.melarecipe` files are readable JSON that any recipe app can import, which is the copy that still means something if Mela is not there to restore into. Photos are not inlined in the JSON — Mela's format wants them base64-encoded, which would multiply the size for no gain when the originals are sitting in the same folder.

The copied store is opened and run through `PRAGMA integrity_check` before the backup is called done, and the command refuses to start if the volume has less than 1.2x the space it needs.

## Not clobbering your edits

Every note carries a `mela_hash` — the hash of the body as it stood at the last sync. That one field is what makes both directions safe to run repeatedly:

- A note whose body still matches its hash has not been touched in Obsidian. `pull` may overwrite it freely.
- A note whose body no longer matches has been edited. `pull` leaves it alone and says so; `push` treats it as the thing to send.

So the two commands never fight over the same note, and neither silently discards work. `pull --force` overrides this if you want Mela to win.

Recipes are matched by their Mela `id`, not by filename, so renaming a note in Obsidian does not create a duplicate on the next pull.

## What Mela's import will and will not do

Tested against Mela 2.6.1 with throwaway recipes, because the file-format documentation does not say:

- Importing a `.melarecipe` whose `id` **Mela already holds does nothing at all.** Not an update, not a duplicate, and not an error — the import reports success and the library is unchanged.
- Importing one with an `id` Mela has **not** seen adds it, even when the title, link, and every other field match an existing recipe exactly.

So `id` is the key Mela dedupes on, and its import is add-only. An edit made in Obsidian to a recipe Mela already has **cannot be delivered as an edit**. `push` therefore refuses to stage those and says which they are, rather than writing a file that imports "successfully" and changes nothing.

Two ways out of that, neither pretty:

- Make the edit in Mela instead, and `pull` it back.
- `push --as-new` mints a fresh id so the edited version imports as a *second* recipe, which you then reconcile by deleting the old one in Mela.

Recipes that Mela has never seen — a note you wrote in Obsidian — import normally with `push --new`.

## The note format

Written to match the shape [Recipe View](https://github.com/lachholden/obsidian-recipe-view) and an ordinary reader both handle:

```markdown
---
title: Creamy Baked Polenta with Herbs and Green Onions
cssclasses:
- recipe
categories:
- Carb
- Side
favorite: false
wantToCook: false
mela_id: epicurious.com/recipes/food/views/creamy-baked-polenta-104946
mela_date: '2024-02-03'
mela_date_raw: 728684132.062597
mela_hash: 22340747c8570aa4
---

Source: [epicurious.com](https://www.epicurious.com/recipes/food/views/...)
Servings: Makes 6 servings

## Ingredients

- 6 cups water
- 1 1/2 cups polenta (coarse cornmeal) or yellow cornmeal

## Instructions

1. Preheat oven to 350°F. Pour 6 cups water into a 13x9x2-inch glass baking dish.
```

Mela stores ingredients and instructions as one string per field, newline-separated, with a leading `#` marking a group heading. Those become a bulleted list, a numbered list, and `###` headings; `push` converts them back. `Source`, `Servings`, `Prep`, `Cook`, and `Total` live in the body rather than the frontmatter, because that is where you want to read them — `push` parses them back out of those lines.

## Things worth knowing before you trust it

- **A note without a `mela_id` is never overwritten.** Hand-written notes have no hash to compare against, so they are treated as not ours: if a Mela recipe wants the same filename, it takes a suffixed one instead.
- **Duplicate ids collapse.** If two recipes in Mela share an `id`, they become one note, and `pull` says how many did.
- **Repeated titles get a suffix.** A hash of the full id, not a prefix of it — recipe ids are often URLs, so the first characters are the same for everything from one site.
- **Images are opt-in.** `--images` copies the first photo per recipe into `attachments/`, named by what the bytes actually are. In one library of 1,234 recipes the photos were 990 JPEG, 84 HEIC, 35 PNG, 2 GIF and 1 WebP, so writing them all as `.jpg` gives you a folder of files Obsidian will not display.
- **`ZRECIPEIMAGEOBJECT.ZDATA` is a tagged blob**, not raw image bytes. A leading `0x01` means the image follows inline; `0x02` means the next 36 characters name a file under `.Curcuma_SUPPORT/_EXTERNAL_DATA`, where Core Data spills the larger ones. Note that `_EXTERNAL_DATA` also holds LZFSE-compressed CloudKit record archives that are not images at all, so the directory cannot be read as a photo folder.
- **Only the first image is exported**, and `push` never sends images back. Mela's format wants them base64-encoded inline, which makes for enormous files — some of the exported `.melarecipe` files in a full library run to 26 MB.

## Reading Mela's library yourself

The store is at:

```
~/Library/Group Containers/66JC38RDUD.recipes.mela/Data/Curcuma.sqlite
```

`ZRECIPEOBJECT` is the recipe table, `ZRECIPETAG` the categories, joined through `Z_4TAGS`, and `ZRECIPEIMAGEOBJECT` the photos. Dates are Core Data's — seconds since 2001-01-01 UTC. Copy the file, plus its `-wal` and `-shm`, before opening it; the live one is normally mid-write.

`.melarecipes` is a zip of `.melarecipe` JSON files, though on disk a Mela export may appear as an ordinary folder that Finder shows as one file.

## Prior art

- [Mela's file format documentation](https://mela.recipes/fileformat/) — the schema this maps onto, published by Mela's developer.
- [markmals/mela-decoder-js](https://github.com/markmals/mela-decoder-js) — reads and writes `.melarecipes` archives from JavaScript.
- [Recipe View](https://github.com/lachholden/obsidian-recipe-view) by lachholden — renders these notes as a recipe card in Obsidian.
- [MoveMyRecipes](https://movemyrecipes.com/mela) — a browser converter between Mela and other recipe apps.

## License

MIT
