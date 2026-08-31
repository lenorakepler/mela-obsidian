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

**Status:** decided and done for the header fields; `Description` still open.

`source`, `servings`, `prep_time`, `cook_time` and `total_time` are frontmatter properties only, and no longer rendered in the body. Duplication was ruled out — a value in two places is a value that can disagree with itself — and frontmatter was the only viable side, since Bases cannot see the body at all.

`mela_hash` now covers those properties as well as the body. It has to: hashing the body alone would mean changing the servings in Obsidian never counted as an edit, and `push` would skip it silently.

### Still open

`Description` — Mela's `text` field — sits as a bare paragraph at the top of the body where nothing can address it. It is a short standalone summary rather than part of the recipe's prose, so it is arguably structural metadata that belongs in frontmatter whatever Bases can render. Moving it would leave the body starting directly at the photo and `## Ingredients`, which may read better or worse; worth living with the current arrangement for a while before deciding.

If it does move, the third option from the original write-up becomes worth considering: render a readable header back from the properties, so the note still reads like a recipe without the rendered text becoming a second source of truth.

## Housekeeping

- **Delete the test recipes in Mela**: `ZZ Melasync Test Recipe (delete me)`, `ZZ Melasync Test Recipe B (delete me)`, and the `Melasync Test` category. Then `pull`, and remove their notes plus `Recipes/Cook Log/2026-08-30 ZZ Melasync Test Recipe (delete me).md` from the vault.
- **Old backups**: `~/Documents/Mela Backups/2026-08-30-203816` (72 MB, store only) is redundant now that `pre-write/` is refreshed before every write. `2026-08-30-201920` (982 MB) is worth keeping — it is the only copy that includes the photos.
- **Source-field tidying in Mela**, which would otherwise fragment a group-by: `Hettie` vs `Hetty McKinnon`, `Andy Barraghani` vs `Baraghani`, `Justine Dorion` vs `Doiron`, `Big Little Recpies`, a stray backtick in ``Milk Bar Life` ``, `Six Seasons` vs `Six Seasons - Joshua McFadden`, the three spellings of the Momofuku Milk Bar books, and one source that is just `4`.

## Ideas not started

- **Multi-value editing in Flex Cards.** A list property falls back to read-only, so `categories` cannot be edited from a card. Needs a real token control.
- **Writing while Mela runs.** Core Data supports multi-process access on a shared store and Mela's own extensions rely on it, so the "quit Mela first" rule may be stricter than necessary. Untested, and the failure mode is losing a write, so it stays strict until someone proves otherwise.
