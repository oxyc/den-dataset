# Plot translations

Untranslated, bge-m3 places a title whose Wikipedia plot is German nearer other German plots than nearer
titles with its story (oxyc/den-dataset#89). `translate.py` translates every plot that is not in English
with Helsinki-NLP's Opus-MT models, into an append-only cache the embed stage reads as the
`plot_translations` artifact. The embed stage uses a translation only when its `source_sha256` is the hash of
the plot it would embed otherwise, so a stale translation is ignored rather than embedded.

The translator is kept out of `pipeline/`, which is stdlib-only: it needs torch and transformers, pinned in
`requirements.txt`. `extract` and `plan` need neither.

## Languages

`models.json` pins one model per language, by commit. A plot language with no model there is left
untranslated and listed as `untranslated` in the run's summary. Portuguese has no `opus-mt-pt-en`, so it uses
the multi-source `opus-mt-ROMANCE-en`, whose only target is English. To add a language, add its model with the
commit from
`https://huggingface.co/api/models/Helsinki-NLP/opus-mt-<lang>-en` (`sha`).

The models are Apache-2.0, except `opus-mt-ru-en` (CC BY 4.0). Wikipedia's plots are CC BY-SA 4.0, and so are
their translations.

## Run it locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r tools/translate/requirements.txt
python3 tools/translate/translate.py extract --enriched out/enriched --out out/plots.jsonl
.venv/bin/python tools/translate/translate.py run --plots out/plots.jsonl --cache out/plot-translations.jsonl
```

`run` appends one row per title as it finishes, so a killed run continues where it stopped, and a re-run after
a re-fetch translates only the plots that changed. `--lang de` runs one language; `--device cpu` forces the
CPU. On an M-series Mac's GPU it managed about 950 chars/s; the whole corpus is ~19M characters.

## Run it in GitHub Actions

`.github/workflows/translate-plots.yml`, dispatched by hand, reads `plots.jsonl` and the cache from the
`plot-translations` prerelease, translates what the cache lacks in parallel CPU jobs of at most `max_chars`
source characters each, and uploads the merged cache back as `plot-translations.jsonl`. A job stops itself
after 4h20m and uploads what it did, so a second dispatch finishes the rest.

```sh
# once: the release, with the notes on the licence
gh release create plot-translations --prerelease --latest=false --title "Plot translations" \
    --notes "English machine translations (Helsinki-NLP Opus-MT) of Wikipedia plot sections. CC BY-SA 4.0."
# each time the plots change
gh release upload plot-translations out/plots.jsonl --clobber
gh workflow run translate-plots.yml
# afterwards, for the embed stage
gh release download plot-translations --pattern plot-translations.jsonl --dir out
```
