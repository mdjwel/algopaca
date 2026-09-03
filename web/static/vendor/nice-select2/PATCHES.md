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

## 4. `innerText` on hidden/collapsed `<select>` returns empty string

When `<select>` is inside a closed `<details>` accordion or hidden container, `option.innerText` evaluates to `""` in standard browser DOM layout engines. NiceSelect used `t.dataset.display ?? t.innerText`, which resulted in empty options `{ text: "" }` if initialized or updated while hidden.

Patch — in `#d(e)`:

```js
{text:t.dataset.display??t.innerText,value:t.value...}
// becomes
{text:t.dataset.display||(t.innerText||t.textContent).trim()||t.value,value:t.value...}
```

## 5. `#y()` must match `<option>` by `value` before falling back to text

When two `<option>` tags have identical display text (e.g. custom engines created from or sharing the name of a starter blueprint), matching by text caused `#y()` to match the wrong `<option>`. When processing the second (unselected) option, it found the first option and set `s.selected = false`, unselecting both options, resetting `select.value` to `""`, and firing a blank change event.

Patch — in `#y()`:
```js
let s=Array.from(e.options).find(e=>String(e.value).trim().toLowerCase()===String(t.data.value).trim().toLowerCase());
null==s&&(s=Array.from(e.options).find(e=>String(e.dataset.display||e.textContent).trim().toLowerCase()===String(t.data.text).trim().toLowerCase())),
```

## 6. `#m(e, t)` click target must resolve to `li.option`

When options contain nested spans (such as badges or metadata tags), `t.target` is the inner `<span>` instead of the `<li>`. NiceSelect added the `selected` class to `t.target`, which placed the class on the child `<span>` rather than `li.option`.

Patch — in `#m(e, t)`:
```js
const s=t.target.closest("li.option")||t.target;
```

## Notes

- `//# sourceMappingURL=nice-select2.js.map` at the end of the bundle now points
  at a map that does not match the patched source. The map is not shipped, so
  this is cosmetic.
- `CSS.escape` (patch 3) is available in all browsers this desk targets.
