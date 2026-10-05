# App localization

The interface supports English and Simplified Chinese. The language selector is
device-local and supports following the system language.

- `webapp/src/i18n.ts` loads the catalogs and resolves the preference.
- `webapp/src/locales/personal.*.json` contains the personal interface messages.
- `webapp/src/locales/ui.*.json` contains the remaining pages, shared controls,
  notifications and native desktop messages.
- `webapp/src/uiI18n.ts` provides `tr`, `useUILanguage` and `uiLocale`. Components
  using `tr` subscribe with `useUILanguage`; memoized translated data must include
  the returned language in its dependencies. Format dates with `uiLocale()`.
- The desktop shell reads the same Chinese catalog. The webview synchronizes its
  resolved language through `set_desktop_language`; `language.json` in the app
  configuration directory preserves it for native startup and tray operation.

Translate display labels only. Keep option values, protocol enums, storage keys,
request fields, CSS classes and conversation identities unchanged. User names,
notes, conversations and model output remain in their original language. Use
complete sentences with interpolation; use plural forms instead of concatenating
English word endings.

`Localization.test.tsx` checks catalog parity, interpolation, untranslated JSX,
filter values, settings and live language switching. Chat tests also verify that
date groups and user text survive a language change. `nativeLanguage.test.ts`
checks the webview-to-native bridge, and Rust tests cover native translations.

Validation: `npm --prefix webapp run lint`, `npm --prefix webapp test`,
`npm --prefix webapp run build`, and `cargo test --lib` in `webapp/src-tauri`.
