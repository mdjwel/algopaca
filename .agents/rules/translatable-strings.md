---
trigger: always_on
---

# Translatable Strings & Language Files Policy

When writing, generating, or refactoring code that includes UI text, labels, notices, or any user-facing strings in this project:

1. **Skip Language / Translation Files Update**:
   - **DO NOT** edit, modify, or add new keys to translation files in `web/static/lang/` (such as `en.json`, `bn.json`, `es.json`, `fr.json`, `hi.json`) or any other localization dictionaries.
   - Never attempt to auto-translate strings or insert new translation entries.
   - The user will handle string translations manually whenever they want.
   - Only modify language files if the user explicitly asks to update or translate them.

2. **Use Translatable Strings with English Text Only**:
   - Always hook new user-facing strings into the project's i18n conventions:
     - **HTML / Templates**: Use `data-i18n="key_name"` (or `data-i18n-placeholder`, `data-i18n-title`, `data-i18n-aria-label`) and provide the default text in **English only** inside the element or attribute.
     - **JavaScript**: Use `t('key_name', 'Default English text')` or `t('key_name', 'Default text with {param}', { param: value })` where the fallback/default string is in **English only**.
   - All translation keys, default texts, and fallback labels MUST be written in English.
