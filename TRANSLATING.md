# Translating Zypp Desk

Zypp Desk uses **gettext**, the usual format for free software. You do not need to know Python.

## Add a new language

1. Copy the template and name it after the language code, for example French:

   ```bash
   cp po/zypp-desk.pot po/fr.po
   ```

2. Open `po/fr.po` in a translation editor (Lokalize, Poedit, GTranslator) or any text editor, and
   fill in every `msgstr`. In the header, set `Language: fr` and the plural rule
   (`Plural-Forms: nplurals=2; plural=(n > 1);` for French).
3. Check it and try it:

   ```bash
   msgfmt --check --statistics -o /dev/null po/fr.po
   ./tools/build-mo.sh
   LANGUAGE=fr ./zypp-desk
   ```

4. Send `po/fr.po` as a pull request.

## Rules of thumb

- Keep the placeholders in braces exactly as they are: `{name}`, `{n}`, `{version}`… You may move
  them inside the sentence.
- Do not translate zypper commands and options (`zypper dup`, `--no-recommends`, `-n`).
- Comments that start with `TRANSLATORS:` above a string explain where it appears.
- `po/ca.po` (Catalan) is complete and can be used as a reference.

## Also translatable

- The menu entry: `data/io.github.jordirelats.ZyppDesk.desktop.in` (`GenericName[fr]=`,
  `Comment[fr]=`, `Keywords[fr]=`).
- The password message: `data/io.github.jordirelats.ZyppDesk.policy.in` (`xml:lang="fr"` lines).

## For developers

After changing any user-visible string, run `./tools/update-po.sh`. It refreshes
`po/zypp-desk.pot` and merges it into every `po/*.po`, and prints how much of each language is
translated.
