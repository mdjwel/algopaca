# Local patches to nice-select2

`nice-select2.min.js` is **not** pristine upstream. It is nice-select2 **2.4.1**
from jsDelivr with three behavioural fixes applied by hand to the minified
bundle. Re-downloading the vendor file will silently revert all three and bring
back the bugs listed below.

If you upgrade nice-select2, re-apply these (or confirm upstream fixed them)
and update this file.

## 1. Programmatic focus must not toggle a dropdown shut

`focus(e)` treats any call as a toggle. `app.js` refocuses the custom UI when a
`<label for=…>` sends focus to the hidden native `<select>`, which closed a
dropdown the user had just opened.

Patch — inside `focus(e="")`, guard the close branch on a sentinel argument:

```js
:(r(this.dropdown,"open"),n(this.el))
// becomes
:"focus_event"!==e&&(r(this.dropdown,"open"),n(this.el))
```

Callers pass `el._niceSelect.focus("focus_event")` to mean "focus, never close".

## 2. `update()` must preserve the open state

Rebuilding the option list while the dropdown was open dropped the `open`
class, so a refresh mid-interaction collapsed the menu.

Patch — in the rebuild path, re-add the class instead of the bare call:

```js
this.#e(!1),e&&i(this.dropdown)
// becomes
this.#e(!1),e&&a(this.dropdown,"open")
```

## 3. Clicking the field's own `<label>` must not close the dropdown

The outside-click handler counted a `<label for=…>` pointing at this select as
"outside", so clicking the label closed the menu it had just opened.

Patch — in the document click handler `#v(e)`, return early for both the
dropdown itself and the label bound to this select:

```js
#v(e){if(this.dropdown.contains(e.target))return;
      if(this.el.id&&e.target.closest&&
         e.target.closest('label[for="'+CSS.escape(this.el.id)+'"]'))return;
      r(this.dropdown,"open"),n(this.el)…
```

## Notes

- `//# sourceMappingURL=nice-select2.js.map` at the end of the bundle now points
  at a map that does not match the patched source. The map is not shipped, so
  this is cosmetic.
- `CSS.escape` (patch 3) is available in all browsers this desk targets.
