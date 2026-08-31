# mela-obsidian

Sync recipes between [Mela](https://mela.recipes) and an Obsidian vault, as plain markdown notes.

## How it works, and why it works that way

Mela keeps its library in a Core Data store — `Curcuma.sqlite`, in the group container `66JC38RDUD.recipes.mela` — which `NSPersistentCloudKitContainer` mirrors to iCloud. Reading it is straightforward and gives you everything: the table columns map one-to-one onto the fields Mela [documents for its export format](https://mela.recipes/fileformat/).

Writing to it is a different matter. Editing rows behind Mela's back would leave the CloudKit mirror describing a library that no longer exists, and the failure would surface later, on another device. So this tool never writes to Mela's store. It reads a copy of it, and the return direction goes out as `.melarecipe` files — the JSON format Mela documents and imports.

That asymmetry is the whole design:

| Direction | How | Automatic? |
| --- | --- | --- |
| Mela → Obsidian | Read a snapshot of the Core Data store, write markdown | Yes — `pull`, on a timer if you want |
| Obsidian → Mela | Write `.melarecipe` JSON, hand it to Mela | Staged automatically, but Mela's import is a click |

## Commands

```bash
./melasync.py status                     # what differs between the two sides
./melasync.py pull                       # Mela -> Obsidian
./melasync.py pull --images              # also copy each recipe's first photo
./melasync.py push                       # Obsidian -> .melarecipe files in the outbox
./melasync.py push --open                # ...and hand them to Mela
./melasync.py install-agent              # pull every 15 minutes via launchd
```

The script has a [PEP 723](https://peps.python.org/pep-0723/) header, so `uv run melasync.py` installs its one dependency itself. `--vault` points at the folder of notes; it defaults to `~/vault/Recipes`.

## Not clobbering your edits

Every note carries a `mela_hash` — the hash of the body as it stood at the last sync. That one field is what makes both directions safe to run repeatedly:

- A note whose body still matches its hash has not been touched in Obsidian. `pull` may overwrite it freely.
- A note whose body no longer matches has been edited. `pull` leaves it alone and says so; `push` treats it as the thing to send.

So the two commands never fight over the same note, and neither silently discards work. `pull --force` overrides this if you want Mela to win.

Recipes are matched by their Mela `id`, not by filename, so renaming a note in Obsidian does not create a duplicate on the next pull.

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

- **Whether re-importing updates or duplicates is untested.** Mela identifies a recipe by `id`, and `push` preserves it, so an import *should* update the existing recipe. That is an inference from the file format documentation, not something this repo has verified — and verifying it means writing to a real library that syncs to iCloud. Test it with one throwaway recipe before pushing a batch.
- **Duplicate ids collapse.** If two recipes in Mela share an `id`, they become one note, and `pull` says how many did.
- **Repeated titles get a suffix.** A hash of the full id, not a prefix of it — recipe ids are often URLs, so the first characters are the same for everything from one site.
- **Images are opt-in.** `--images` copies the first photo per recipe into `attachments/`. Core Data keeps large images as separate files and small ones inline in the row; both are handled.
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
