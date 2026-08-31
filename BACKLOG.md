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

## De-duplicate recipes saved from the same URL

**23 redundant records across 21 URLs.** Mela dedupes on `id`, and for a web recipe the id is the URL — so the same recipe saved from the desktop and from the phone becomes two entries, because the iOS share sheet appends `smid=ck-recipe-iOS-share` and the unlocked-article link appends `unlocked_article_code=…`. Mela sees two different ids and keeps both.

Worst offenders are NYT (7 URLs) and Punch (3); one recipe exists three times. A further 18 titles repeat without matching URLs, which is a different question — some are genuinely different recipes with the same name.

`clean_url()` already strips query strings and fragments; it was written for the calorie refetch. What is missing is the merge: decide which record to keep (the one with photos, the one with cook-log notes, the older `date`), fold anything the others have that it does not, and delete the rest. Deletion means writing through the Core Data path with Mela quit, and it is the one operation in this repo that destroys data rather than adding it — so it wants a dry run that prints exactly what would be merged and dropped, and it should never pick a survivor without showing the choice.

## Pull recipes out of epubs directly

Cookbook recipes are currently transcribed by hand or by OCR, which is where the truncated and unsplit ones come from — `Tempura Scallion Bottoms` lost half its method to a page break, and `Spinach and Tofu Wontons` has its entire method as one 2,266-character line because OCR dropped the step numbering.

**None of that is necessary: an epub is a zip of XHTML.** The text is already structured, with real heading and list markup, so a reader can lift the title, the ingredient list, and the numbered steps as separate fields — no OCR, nothing to re-split, no page boundaries to lose. A skill could take an epub and a recipe name, or walk the whole book, and emit `.melarecipe` JSON or notes directly.

**The books are already on disk.** `~/Dropbox/Calibre Library` holds 476 books, 293 of them epub, and it covers most of what the Evernote titles cite — Lucky Peach, Momofuku, NOPI, Start Simple, Dirt Candy, Le Pigeon, Ad Hoc at Home, Kristen Kish Cooking, Superiority Burger, Ottolenghi Simple. Missing from that library, at least: Hetty McKinnon's *Family* (40 notes, the single most-cited), Diana Henry, Morimoto, Frankie Gaw, Nik Sharma. Three further Calibre libraries exist under `Dropbox/Literature`, `Dropbox/Docs/eBooks` and `Dropbox/From lenorakepler directory` whose `metadata.db` would not open — likely Dropbox placeholders rather than empty, so the gaps may close once they are downloaded.

That makes this the other half of the Evernote job: the roughly 350 notes whose titles cite a cookbook are not fetchable from the web, but they are recoverable from these files.

The hard parts are that every publisher marks recipes up differently, and that ingredient groups (`### AIOLI`) are usually a styled paragraph rather than a real heading. Both are per-book problems, which suits a skill that can look at the file and adapt better than a fixed parser would.

### Related: instruction blobs already in the library

21 recipes have an instruction line of 900+ characters. Sentence count separates the two causes: ~20+ sentences on one line is OCR that lost its step boundaries, while `Chinese Leaf Salad` is 1,662 characters and ~7 sentences, which is just a wordy step. Nine recipes also show `towel-it's` and `center-don't`, em dashes flattened to hyphens, which corroborates the OCR origin.

Splitting these is not automatable: the numbering is gone, so a script would have to break on every sentence and turn one step into six. A skill that proposes a split for review is the realistic shape.

Also worth noting: `Shockingly Easy No-Knead Focaccia` is in the library twice with identical instructions.

## The Evernote recipe cache

**2,750 notes in `~/vault/Old/Evernote/Food`, plus 129 in `Old/Evernote/Food Ideas  Eaten`** — more than double the Mela library, imported from Evernote in every state of repair. Most were web clippings, so they arrived with the whole page: navigation, subscribe prompts, social buttons, icons, and the images that went with all of it.

What a sample of them looks like:

| | Notes |
| --- | --- |
| Total | 2,750 |
| Contain images | 1,739 |
| Have a recognisable `## Ingredients` / `## Directions` heading | 575 |
| Contain nav or marketing text (sign in, subscribe, share this, advertisement) | 413 |
| Bare link-only lines, the shape a stripped nav bar leaves | 726 |
| Over 12,000 characters | 250 |

**They are already sorted, which is worth more than any of the statistics below.** The folder is 21 subcategories that map straight onto Mela's categories — `Main - GrainsBowls` (629), `Salads` (337), `Dessert` (310), `SaucesDressings` (225), `Side - Non-Vegetable` (184), `Appetizers` (157) and so on. Two of those folders are not recipes and can be set aside up front: `Not Recipes` (32) and `TOC` (128), the latter being cookbook tables of contents. That leaves **2,588 recipes, pre-categorised**.

Images live in a `_resources` directory inside each subcategory, holding about 12,000 files and no markdown. They are not all photographs: 5,636 are SVGs averaging 616 bytes, which is site iconography, against 3,535 JPEGs averaging 49 KB, which is food. Size alone separates them.

**Nearly all of these are recipes.** Only 575 use an `## Ingredients` heading, but that measures formatting, not content: sampling the rest turns up `INGREDIENTS` in bare capitals, a `Serves 4` line followed straight by the ingredients, and web clips where the recipe is buried under the site's navigation. A handful are indexes or lists. So this is an extraction and cleanup problem across roughly 2,700 recipes, not a question of which ones are recipes.

The notes also carry their Evernote tags, and two of them are useful for ordering the work:

| Tag | Notes | Already in Mela |
| --- | --- | --- |
| `Transferred` | 57 | 43 (75%) |
| `Made` | 386 | 29 (7%) |

`Transferred` is reliable — matching on titles normalised for the site suffixes Evernote appended (`- BA`, `TASTE`, `| Serious Eats`), 43 of 57 are confirmed present, and the misses are junk titles like `Untitled Note` rather than failures of the tag. Use it to exclude.

**`Made` is where to start.** 386 notes marked as actually cooked, and only 29 of them are in Mela — so roughly 357 recipes she has made and kept are sitting unimported. Not because the rest are doubtful, but because these are the ones already known to be worth the trouble, which makes them the right batch to shake the extraction out on before running it across all 2,700.

### Re-fetch rather than parse

Most of these notes record where they came from, and a live URL is worth more than the clipping wrapped around it — re-downloading gives clean structured text with no page chrome to strip.

| | Notes |
| --- | --- |
| `source` URL in frontmatter | 1,458 |
| URL only in the body | 415 |
| No URL at all | 715 |

The hosts are mainstream and well-parsed: NYT 235, Bon Appétit 222, Food52 200, Serious Eats 138, Smitten Kitchen 69, Epicurious 62.

This is the same mechanism as the NYT calorie item above — Mela's `Download Recipe` Shortcuts action takes a URL and returns a parsed recipe. Build that Shortcut once and it serves both jobs, and these arrive in Mela the way any web recipe does.

Three things to get right:

- **A body URL is not a source URL.** In one sampled clipping the first link in the body was the blog's homepage from its navigation bar, not the recipe. Taking the first URL found would fetch the wrong page and look like it worked. The 1,458 with frontmatter are safe; the 415 are not, without a look.
- **Some hosts are gone.** `cookinglight.com` (47) and `myrecipes.com` (32) are defunct or redirected, so around 80 will return a homepage rather than a 404 — success-shaped failure. Compare the fetched title against the note's before accepting anything.
- **A title often names its source, but usually a book.** Of the 1,130 without a frontmatter URL, 393 carry a name after a dash — and the common ones are `Hetty McKinnon (Family)` 40, `Lucky Peach` 15, `Momofuku` 13, `NOPI (Ottolenghi)` 14, `Diana Henry` 12. Those are citations, not addresses, so they are not fetchable; they belong in `source` as text, which is the `source_type: text` case the sync already handles. A minority name a website — `smitten kitchen` and the like — where the title is enough to find the page or reconstruct a slug URL.
- **Some notes are photographs, not text.** 84 hold under 60 words of prose and one or more page photos — `Halloumi, Kale, and Mint Gozleme - Hetty McKinnon (Family)` is literally `![[IMG_7600.jpeg]]![[IMG_7601.jpeg]]` and nothing else. A further 252 have images and only 60–200 words, which look like transcriptions that stalled part-way. These are photographs of physical cookbooks that have no epub and no URL, so neither of the routes above reaches them; they need the page read. The images are `.jpeg`, converted from HEIC somewhere in the Evernote export, and sit in the `_resources` directory beside the note. `Family` is the most-cited book in the whole collection at 40 notes and is exactly this case.
- **Keep the clipping until the fetch is verified.** For the 715 with no URL, and for anything that fails, parsing the existing text is still the fallback.

A workable order:

1. **Recognise the shape.** Not "is this a recipe" but "where does the recipe start". The formats seen so far: a `## Ingredients` heading (575), bare `INGREDIENTS` capitals, a yield line followed directly by ingredients, and a full page clip with the recipe somewhere inside it. A quantity-dense run of short lines is the signal that survives all four.
2. **Strip the page.** The clipped clutter is regular enough to be worth a pass of its own: link-only lines, known marketing phrases, icon images. `verbatim_clean`'s host-profile approach in the job-search repo is the same problem solved once already.
3. **Extract.** Title, ingredients, steps into the same shape `pull` writes, so they can join the vault as ordinary recipe notes.
4. **De-duplicate against Mela's 1,235.** Some fraction of these were later re-saved into Mela properly, and importing them again would undo the work in the section above. Match on normalised source URL first, title second.

Only step 2 is really mechanical. The rest wants judgement per note, which suits a skill working in batches with review, rather than one script run over 2,750 files.

Worth deciding up front how much of this is wanted at all. A cache of clippings that has sat untouched since the Evernote export may be more valuable as a searchable archive than as 2,750 more recipe notes.

## Safety nets

Two independent ones, worth knowing before deciding how carefully to tread.

**The Mela side** is covered by `backup`, and by the single `pre-write/` snapshot that `push --write` refreshes before every write. That protects the recipe library and its photos.

**The vault side** is covered by Obsidian Sync, which keeps version history and deleted-file history server-side. A note overwritten or deleted locally can be restored from it, timestamps included.

Neither is a reason to write more freely. Work as though there is no backup and be glad of one on the day it is needed — a restore is still an interruption and still work. What the safety nets change is only what to say afterwards: do not call something unrecoverable without having looked.

## Housekeeping

- **Delete the test recipes in Mela**: `ZZ Melasync Test Recipe (delete me)`, `ZZ Melasync Test Recipe B (delete me)`, and the `Melasync Test` category. Then `pull`, and remove their notes plus `Recipes/Cook Log/2026-08-30 ZZ Melasync Test Recipe (delete me).md` from the vault.
- **Old backups**: `~/Documents/Mela Backups/2026-08-30-203816` (72 MB, store only) is redundant now that `pre-write/` is refreshed before every write. `2026-08-30-201920` (982 MB) is worth keeping — it is the only copy that includes the photos.
- **Source-field tidying in Mela**, which would otherwise fragment a group-by: `Hettie` vs `Hetty McKinnon`, `Andy Barraghani` vs `Baraghani`, `Justine Dorion` vs `Doiron`, `Big Little Recpies`, a stray backtick in ``Milk Bar Life` ``, `Six Seasons` vs `Six Seasons - Joshua McFadden`, the three spellings of the Momofuku Milk Bar books, and one source that is just `4`.

## Substantial recipe layout wants a plugin, not CSS

`snippets/recipe-note.css` gets full width and a floated cover photo. The two-column layout was written, tried, and removed — that is where CSS stopped being the right tool, and the reason turned out to be concrete rather than aesthetic.

**Floating `.cm-line` breaks the caret.** CodeMirror positions the cursor from line geometry, so floating lines to build columns in live preview puts the caret in the wrong place. Nothing to tune around: the layout and the editor want the same property to mean different things.

The obstacle is not selector syntax. In reading view a section can be named — `:has(h2[data-heading="Ingredients"])` — but CodeMirror exposes no equivalent, so live preview can only count headings by position, and hand-reordering a note's sections breaks it. Nor can CSS group siblings: ingredients and instructions are flat sibling blocks with a variable number belonging to each, which is why they are floated rather than placed in a grid. Independent scrolling per column, a step-at-a-time mode, quantity scaling, or checking off ingredients are all past the point where more `:has()` helps.

The way through is what [Recipe View](https://github.com/lachholden/obsidian-recipe-view) does: register a view type, parse the markdown, and build the DOM. Total control over layout, nothing fighting CodeMirror, and the same approach already taken for the Bases cards in [obsidian-flex-cards](https://github.com/lenorakepler/obsidian-flex-cards) — where registering a view was likewise the answer to a layout the built-in one could not express.

Worth doing only when the CSS version proves genuinely limiting. It renders read-only, so editing still happens in a normal view.

## Ideas not started

- **Multi-value editing in Flex Cards.** A list property falls back to read-only, so `categories` cannot be edited from a card. Needs a real token control.
- **Writing while Mela runs.** Core Data supports multi-process access on a shared store and Mela's own extensions rely on it, so the "quit Mela first" rule may be stricter than necessary. Untested, and the failure mode is losing a write, so it stays strict until someone proves otherwise.
