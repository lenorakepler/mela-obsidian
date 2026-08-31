# Backlog

## Recover missing NYT calorie counts

**Status:** code written and committed, blocked on one Shortcut that has to be built by hand.

### The problem

259 of 314 recipes saved from `cooking.nytimes.com` have a nutrition field with no calorie count in it. They carry `**Fat** 14 grams`, `**Protein** 34 grams` and the rest, but no total. NYT publishes calories on all of them; Mela's importer did not capture it for a long stretch, so the data is simply absent from the library. It is not a parsing failure — `parse_nutrition` finds every number that is present.

Across the whole library that is why `calories` sits at 132 recipes while the macros are on ~360.

### Why a Shortcut

Recovering the numbers means re-reading the pages. Mela ships a Shortcuts action, **Download Recipe** (`GetRecipeFromURLIntent`, in `recipes.mela.appkit.Intents`), which takes a URL and returns a parsed recipe. Using it means Mela's own parser and Mela's own site access do the fetching, through the subscription already signed in to the app. Nothing here scrapes a paywalled site or works around one; this end only supplies URLs and reads what comes back.

`/usr/bin/shortcuts` can run a shortcut headlessly, so the loop can be driven from a script — but a `.shortcut` file is signed, which is why this one has to be assembled in the app rather than generated.

### What is already done

- `melasync.py refetch-nutrition` — selects recipes on a host whose nutrition lacks a given field, normalises and de-duplicates their URLs, runs the Shortcut once per URL, parses the result, and writes the recovered value back through the Core Data path (so it syncs to every device the way any other edit does).
- URL normalisation strips query strings and fragments. 192 of these URLs carry `smid=ck-recipe-iOS-share` or `unlocked_article_code=…` from the iOS share sheet, and stripping them also collapses 3 pairs that were the same recipe saved twice under different tracking parameters. **256 unique URLs** remain.
- `parse_nutrition` already reads every shape NYT uses, including the run-together `Calories (kcal) 490 Fat (g) 33 …` form.

### Steps to finish

1. **Build the Shortcut.** In Shortcuts.app, create one named exactly `Mela Fetch`.

   - Add the **Download Recipe** action (search "Mela").
   - Set its URL field to the **Shortcut Input** magic variable. Click the field and look in the variable picker; on macOS this is available to any shortcut run from the command line, and the checkboxes in the details pane are only about Quick Actions and the menu bar.
   - Add **Stop and Output**, returning the recipe's nutrition.

   *If `Shortcut Input` is not offered:* have the shortcut read the URL from a file instead — **Get File** at `/tmp/mela-fetch-url.txt`, then **Get Text from Input**, then **Download Recipe** with that text. The runner then needs two lines changed, to write each URL to that path rather than pass it on stdin.

2. **Find out what Download Recipe actually returns.** With the action in place, click the value field on **Stop and Output** and see whether the picker offers properties of the recipe — Nutrition, Title, Ingredients — or only one opaque recipe variable. If properties are offered, return Nutrition. If not, return the recipe itself; `parse_nutrition` reads loose text and may well find the numbers in a dump. This is the one genuinely unknown step.

3. **Test a single URL before the batch:**

   ```bash
   ./melasync.py refetch-nutrition --limit 1 --dry-run
   ```

   Expected: one line reading `calories=<number>` and a title. If it reports `no calories returned`, step 2 needs a different output.

4. **Run the batch.** Quit Mela first — the write path refuses while it is running.

   ```bash
   ./melasync.py refetch-nutrition          # all 256
   ./melasync.py refetch-nutrition --replace  # take Mela's whole fresh nutrition block instead of appending the calorie line
   ```

   Default behaviour appends `**Calories** N` to the existing text rather than replacing it, so nothing already captured is lost.

5. **Launch Mela** and leave it frontmost so the mirror exports the changes, then `./melasync.py pull` to bring the numbers into the vault as `calories`.

### Worth knowing

- 256 sequential fetches will not be quick, and each one is a real page load by Mela. `--limit` exists for working through it in batches.
- Some of these recipes may genuinely have no calorie count on the page any more. A run that recovers most of them is the realistic outcome.
- The same command works for any host and any field: `--host seriouseats.com --field protein`.

## Decide which fields belong in frontmatter rather than the body

**Status:** open question, no work done.

Right now the split is roughly "what you read goes in the body, what the sync needs goes in frontmatter". That was a reasonable first cut but it is not obviously the right line, for two separate reasons.

The practical one: **Bases can only see frontmatter.** A field in the body cannot be a column, a filter, a sort key, or a group-by. That is already why `image`, `source`, and the parsed nutrition numbers were promoted, and each promotion was driven by wanting a view rather than by any judgement about where the field belonged.

The better reason: some of these are **genuinely structural metadata and should be properties regardless of what Bases can do**. `Description` — Mela's `text` field — is the clearest case. It is a short standalone summary of the recipe, not part of the recipe's prose, and it currently sits as a bare paragraph at the top of the body where nothing can address it. `Servings`, `Prep`, `Cook` and `Total` are arguably the same: they are values, they are the sort of thing you filter on ("under 30 minutes"), and they are rendered in the body only because that is where they read nicely.

### What makes this more than a formatting choice

Two mechanisms currently assume the body carries the content:

- **`mela_hash` is a hash of the body.** It is how `pull` tells an edited note from an untouched one and how `push` decides what to send. A field that moves to frontmatter stops being covered by it, so editing that field would no longer register as an edit and `push` would silently skip it. Either the hash has to cover the relevant frontmatter too, or promoted fields need their own comparison.
- **`note_to_recipe` parses `Source:`, `Servings:`, `Prep:`, `Cook:` and `Total:` back out of the body's preamble lines** on the way to Mela. If those move, that parsing moves with them, and the two must not disagree — a value in both places with different contents is the failure mode to design out.

### Options, roughly in increasing order of disruption

1. **Duplicate, as now.** `source` is in both; the body renders it and frontmatter carries it. Simple, and the duplication is invisible because only one side is authoritative for the sync. Does not scale — every duplicated field is a chance for the two to drift.
2. **Promote and stop rendering.** Move `Description` and the times into frontmatter alone, and let Obsidian's Properties panel display them. Cleanest data model; costs the readable header at the top of a note, which matters when the note is what you cook from.
3. **Promote and render from frontmatter.** Keep the reading experience by rendering the header from properties — a `Templater`-style inline block, or a CSS/Dataview rendering. Best of both, most machinery, and the rendered text must never become a second source of truth.

Worth deciding deliberately rather than one field at a time when a view happens to need it, which is how the first three got promoted.

## Housekeeping

- **Delete the test recipes in Mela**: `ZZ Melasync Test Recipe (delete me)`, `ZZ Melasync Test Recipe B (delete me)`, and the `Melasync Test` category. Then `pull`, and remove their notes plus `Recipes/Cook Log/2026-08-30 ZZ Melasync Test Recipe (delete me).md` from the vault.
- **Old backups**: `~/Documents/Mela Backups/2026-08-30-203816` (72 MB, store only) is redundant now that `pre-write/` is refreshed before every write. `2026-08-30-201920` (982 MB) is worth keeping — it is the only copy that includes the photos.
- **Source-field tidying in Mela**, which would otherwise fragment a group-by: `Hettie` vs `Hetty McKinnon`, `Andy Barraghani` vs `Baraghani`, `Justine Dorion` vs `Doiron`, `Big Little Recpies`, a stray backtick in ``Milk Bar Life` ``, `Six Seasons` vs `Six Seasons - Joshua McFadden`, the three spellings of the Momofuku Milk Bar books, and one source that is just `4`.

## Ideas not started

- **Multi-value editing in Flex Cards.** A list property falls back to read-only, so `categories` cannot be edited from a card. Needs a real token control.
- **Writing while Mela runs.** Core Data supports multi-process access on a shared store and Mela's own extensions rely on it, so the "quit Mela first" rule may be stricter than necessary. Untested, and the failure mode is losing a write, so it stays strict until someone proves otherwise.
