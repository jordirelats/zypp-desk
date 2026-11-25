# Translating Zypp Desk

Zypp Desk uses **gettext**, the usual format for free software. You do not need to know Python.

## Add a new language

Catalan (`ca`), German (`de`) and French (`fr`) are already there: to improve one of them, edit its
file in `po/` directly.

1. Copy the template and name it after the language code, for example Italian:

   ```bash
   cp po/zypp-desk.pot po/it.po
   ```

2. Open `po/it.po` in a translation editor (Lokalize, Poedit, GTranslator) or any text editor, and
   fill in every `msgstr`. In the header, set `Language: it` and the plural rule
   (`Plural-Forms: nplurals=2; plural=(n != 1);` for Italian).
3. Check it and try it:

   ```bash
   msgfmt --check --statistics -o /dev/null po/it.po
   ./tools/build-mo.sh
   LANGUAGE=it ./zypp-desk
   ```

4. Send `po/it.po` as a pull request.

## Rules of thumb

- Keep the placeholders in braces exactly as they are: `{name}`, `{n}`, `{version}`… You may move
  them inside the sentence.
- Do not translate zypper commands and options (`zypper dup`, `--no-recommends`, `-n`).
- Comments that start with `TRANSLATORS:` above a string explain where it appears.
- `po/ca.po` (Catalan) is complete and can be used as a reference.

## Also translatable

- The menu entry: `data/io.github.jordirelats.ZyppDesk.desktop.in` (`GenericName[it]=`,
  `Comment[it]=`, `Keywords[it]=`).
- The password message: `data/io.github.jordirelats.ZyppDesk.policy.in` (`xml:lang="it"` lines).

## For developers

After changing any user-visible string, run `./tools/update-po.sh`. It refreshes
`po/zypp-desk.pot` and merges it into every `po/*.po`, and prints how much of each language is
translated.
