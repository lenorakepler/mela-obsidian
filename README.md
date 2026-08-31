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

## Keeping the bookkeeping out of your way

`mela_id`, `mela_hash`, and `mela_date_raw` are machinery, and the Properties panel invites a stray keystroke into them. `snippets/hide-sync-properties.css` hides all three from the properties editor and from Bases table and card views. It hides them from the UI only — the file still carries them, which is what this tool reads.

## Not finished

`BACKLOG.md` holds the one piece that is written but not working: recovering the calorie counts NYT publishes and Mela's importer did not capture, through Mela's own `Download Recipe` Shortcuts action. The code is committed; it needs a Shortcut built by hand, because a `.shortcut` file is signed and cannot be generated.

## Backing up first

```bash
./melasync.py backup                     # ~1 GB, to ~/Documents/Mela Backups/<timestamp>/
./melasync.py backup --no-images         # ~70 MB, the store alone
./melasync.py backup --to /Volumes/Ext   # somewhere with more room
```

Two copies, because they fail differently. The raw `Curcuma.sqlite` (plus its `-wal`, `-shm`, and the `_EXTERNAL_DATA` photos) is byte-exact and restores everything, but only into Mela. The `recipes/*.melarecipe` files are readable JSON that any recipe app can import, which is the copy that still means something if Mela is not there to restore into. Photos are not inlined in the JSON — Mela's format wants them base64-encoded, which would multiply the size for no gain when the originals are sitting in the same folder.

The copied store is opened and run through `PRAGMA integrity_check` before the backup is called done, and the command refuses to start if the volume has less than 1.2x the space it needs.

## Not clobbering your edits

Every note carries a `mela_hash` — a hash of the body as it stood at the last sync, together with the promoted header properties. That one field is what makes both directions safe to run repeatedly:

- A note whose body still matches its hash has not been touched in Obsidian. `pull` may overwrite it freely.
- A note whose body no longer matches has been edited. `pull` leaves it alone and says so; `push` treats it as the thing to send.
- A note carrying a `mela_id` but **no** `mela_hash` is in an unknown state, and is treated as edited rather than as fresh. Deleting the hash by accident should not be a way to lose the note's contents.

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

## Cook logs

A note in the recipe's Notes field that records *making* it — the date, what you changed, whether it worked — is a different kind of thing from a storage tip, and worth being able to read on its own. So each one becomes its own note under `Recipes/Cook Log/`, embedded back into the recipe:

```markdown
---
recipe: "[[Carrot Peanut Satay Ramen]]"
date: 2026-03-23
tags: [cook-log]
---

Everyone really loved this. Roasted double the carrots at 425 before the pan.
```

and in the recipe, under `## Notes`:

```markdown
![[2026-03-23 Carrot Peanut Satay Ramen]]

Storage: keeps 3 days refrigerated.
```

The two sides take the form that suits them. In the vault an entry is a `### Made 2026-03-23` heading, which folds, appears in the outline, and nests correctly under `## Notes`. Mela gets `**Made 2026-03-23:** ` inline, because a heading reads badly in its line spacing. `push` expands embeds and converts heading to bold; `pull` converts back.

`split-logs` migrates entries already written by hand. It only touches lines opening with a date-shaped marker, so storage tips, make-ahead instructions and quoted reviews stay exactly where they are. Run it with `--dry-run` first; an entry whose date has no year is reported and left alone, since there is nothing to date it by.

### How pull avoids flattening the embeds

Push sends Mela flat text, so a later pull regenerating the note from that text would replace every embed with the words it expanded to. Each note therefore records `mela_sent`, the hash of what Mela was last given. If a pull rebuilds exactly that, Mela has learnt nothing since the push, and the note is left alone — embeds intact. Only a genuine change on Mela's side is written back.

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

Mela stores ingredients and instructions as one string per field, newline-separated, with a leading `#` marking a group heading. Those become a bulleted list, a numbered list, and `###` headings; `push` converts them back, dropping any task checkbox it finds — `- [x] 2 eggs` is a cooking state, not an ingredient named `[x] 2 eggs`. `Source`, `Servings`, `Prep`, `Cook` and `Total` are frontmatter properties and appear nowhere in the body. Bases can only filter, sort and group on frontmatter, and a value kept in both places is a value that can disagree with itself. The Properties panel displays them while you read.

Those fields are covered by `mela_hash` along with the body. Hashing the body alone would mean changing the servings in Obsidian never registered as an edit, and `push` would skip it without saying so.

## Things worth knowing before you trust it

- **A note without a `mela_id` is never overwritten.** Hand-written notes have no hash to compare against, so they are treated as not ours: if a Mela recipe wants the same filename, it takes a suffixed one instead.
- **Nutrition is parsed into numbers** — `calories`, `protein_g`, `fat_g`, `saturated_fat_g`, `carbs_g`, `fiber_g`, `sugar_g`, `sodium_mg`, `cholesterol_mg` — so a view can filter and sort on them, while the prose stays in the body where it is read. Roughly 30% of a typical library has any of this, and calories far less than the macros: many sites publish grams without a total. The numbers come from many sites with different serving assumptions, so they answer "show me the high-protein ones" and should not be treated as a ledger.
- **Duplicate ids collapse.** If two recipes in Mela share an `id`, they become one note, and `pull` says how many did.
- **Repeated titles get a suffix.** A hash of the full id, not a prefix of it — recipe ids are often URLs, so the first characters are the same for everything from one site.
- **Images are opt-in.** `--images` copies the first photo per recipe into `attachments/`, named by what the bytes actually are. In one library of 1,234 recipes the photos were 990 JPEG, 84 HEIC, 35 PNG, 2 GIF and 1 WebP, so writing them all as `.jpg` gives you a folder of files Obsidian will not display.
- **`ZRECIPEIMAGEOBJECT.ZDATA` is a tagged blob**, not raw image bytes. A leading `0x01` means the image follows inline; `0x02` means the next 36 characters name a file under `.Curcuma_SUPPORT/_EXTERNAL_DATA`, where Core Data spills the larger ones. Note that `_EXTERNAL_DATA` also holds LZFSE-compressed CloudKit record archives that are not images at all, so the directory cannot be read as a photo folder.
- **Every photo is exported.** The first becomes the `image` property, which is what a card view uses as its cover; any others are embedded under a `## Photos` heading at the end of the note. `push` never sends images back. Mela's format wants them base64-encoded inline, which makes for enormous files — some of the exported `.melarecipe` files in a full library run to 26 MB.

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
