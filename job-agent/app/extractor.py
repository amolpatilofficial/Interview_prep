"""Turns a live page into a machine-readable form schema.

A single JS pass walks every frame's DOM, tags each control with a
``data-jaa-id`` attribute (so Playwright can find it again), and resolves the
human-readable question next to it. Radio/checkbox groups collapse into one
logical field with options.
"""
from __future__ import annotations

import re
from typing import Any

EXTRACT_JS = r"""
(() => {
  const OUT = [];
  let counter = 0;
  const nextId = () => `jaa-${++counter}`;

  const CONTROL_SEL = 'input, select, textarea, [contenteditable="true"], [role="combobox"], [role="textbox"]';
  const SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (el) => (el ? clean(el.innerText || el.textContent || '') : '');

  function humanize(s) {
    return clean(String(s || '')
      .replace(/[_\-.\[\]]+/g, ' ')
      .replace(/([a-z0-9])([A-Z])/g, '$1 $2'))
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function isVisible(el) {
    if (el.type === 'file') return true;               // file inputs are usually hidden by design
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (parseFloat(st.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    // an ancestor might be collapsed
    let p = el.parentElement, hops = 0;
    while (p && hops < 12) {
      const ps = window.getComputedStyle(p);
      if (ps.display === 'none' || ps.visibility === 'hidden') return false;
      if (p.hasAttribute('aria-hidden') && p.getAttribute('aria-hidden') === 'true') return false;
      p = p.parentElement; hops++;
    }
    return true;
  }

  function countControls(node) {
    try { return node.querySelectorAll('input:not([type=hidden]), select, textarea').length; }
    catch (e) { return 99; }
  }

  function findLabel(el) {
    const byIds = el.getAttribute('aria-labelledby');
    if (byIds) {
      const t = byIds.split(/\s+/).map((id) => textOf(document.getElementById(id))).filter(Boolean).join(' ');
      if (t) return t;
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;

    if (el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id.replace(/"/g, '\\"');
      const lab = document.querySelector(`label[for="${esc}"]`);
      const t = textOf(lab);
      if (t) return t;
    }
    const wrapping = el.closest('label');
    if (wrapping) {
      const t = textOf(wrapping);
      if (t) return t;
    }
    // Walk up looking for a label/legend/heading in a container that holds
    // exactly this one control (so we don't steal a neighbour's label).
    let node = el.parentElement, hops = 0;
    while (node && hops < 6) {
      if (countControls(node) <= 1) {
        const cand = node.querySelector('label, legend, .label, [class*="label"], [class*="Label"], h1, h2, h3, h4, h5, h6');
        if (cand && !cand.contains(el)) {
          const t = textOf(cand);
          if (t && t.length < 400) return t;
        }
      }
      node = node.parentElement; hops++;
    }
    return clean(el.getAttribute('placeholder')) || humanize(el.getAttribute('name') || el.id || '');
  }

  function groupLabel(el, name) {
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      const t = textOf(lg);
      if (t) return t;
    }
    let node = el.parentElement, hops = 0;
    while (node && hops < 6) {
      const cand = node.querySelector('legend, label, .label, [class*="label"], [class*="Label"], h1,h2,h3,h4,h5,h6, p');
      if (cand && !cand.contains(el)) {
        const t = textOf(cand);
        if (t && t.length < 400) return t;
      }
      node = node.parentElement; hops++;
    }
    return humanize(name);
  }

  function isRequired(el, label) {
    if (el.required) return true;
    if (el.getAttribute('aria-required') === 'true') return true;
    if (/\*/.test(label || '')) return true;
    if (/\(required\)|\brequired\b/i.test(label || '')) return true;
    const wrap = el.closest('[class*="required"], [data-required="true"]');
    if (wrap) return true;
    return false;
  }

  function normalizeType(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return 'textarea';
    if (tag === 'select') return el.multiple ? 'multiselect' : 'select';
    if (el.getAttribute('contenteditable') === 'true') return 'richtext';
    if (tag !== 'input') {
      // role-based custom widget
      const role = (el.getAttribute('role') || '').toLowerCase();
      return role === 'combobox' ? 'custom_select' : 'text';
    }
    const t = (el.type || 'text').toLowerCase();
    if (['text', 'email', 'tel', 'url', 'number', 'date', 'password', 'search', 'month'].includes(t)) return t;
    if (t === 'file') return 'file';
    if (t === 'radio') return 'radio';
    if (t === 'checkbox') return 'checkbox';
    return 'text';
  }

  // ---- pass 1: collect every candidate control -----------------------------
  const controls = Array.from(document.querySelectorAll(CONTROL_SEL));
  const radioGroups = new Map();   // name -> {label, options[]}
  const checkboxGroups = new Map();

  for (const el of controls) {
    if (el.disabled || el.readOnly) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && SKIP_TYPES.has((el.type || '').toLowerCase())) continue;
    if (!isVisible(el)) continue;
    // Skip the inner text box of a custom combobox; the combobox wrapper wins.
    if (tag === 'input' && el.getAttribute('role') === 'combobox' && el.closest('[role="combobox"]') !== el) {
      // still allow it — react-select puts role=combobox on the input itself
    }

    const type = normalizeType(el);
    const id = nextId();
    el.setAttribute('data-jaa-id', id);
    const name = el.getAttribute('name') || '';
    const rawLabel = findLabel(el);

    if (type === 'radio' || (type === 'checkbox' && name && document.querySelectorAll(`input[type=checkbox][name="${(window.CSS&&CSS.escape)?CSS.escape(name):name}"]`).length > 1)) {
      const store = type === 'radio' ? radioGroups : checkboxGroups;
      const key = name || rawLabel;
      if (!store.has(key)) {
        store.set(key, { label: groupLabel(el, key), options: [], required: false, type });
      }
      const g = store.get(key);
      g.options.push({ jaa_id: id, value: el.value, label: rawLabel || el.value, checked: el.checked });
      g.required = g.required || isRequired(el, g.label);
      continue;
    }

    const rec = {
      jaa_id: id,
      type,
      tag,
      name,
      dom_id: el.id || '',
      label: rawLabel,
      required: isRequired(el, rawLabel),
      placeholder: el.getAttribute('placeholder') || '',
      maxlength: el.getAttribute('maxlength') ? parseInt(el.getAttribute('maxlength'), 10) : null,
      current_value: (type === 'checkbox') ? (el.checked ? 'checked' : '') : (el.value || textOf(el) || ''),
      options: [],
      accept: el.getAttribute('accept') || '',
    };

    if (type === 'select' || type === 'multiselect') {
      rec.options = Array.from(el.options || [])
        .map((o) => ({ value: o.value, label: clean(o.text) }))
        .filter((o) => o.label !== '');
    }
    if (type === 'custom_select') {
      // React-select / Workday style: try to read the listbox if it's in the DOM.
      const owns = el.getAttribute('aria-owns') || el.getAttribute('aria-controls');
      if (owns) {
        const list = document.getElementById(owns);
        if (list) {
          rec.options = Array.from(list.querySelectorAll('[role="option"], li'))
            .map((o) => ({ value: clean(textOf(o)), label: clean(textOf(o)) }))
            .filter((o) => o.label !== '')
            .slice(0, 60);
        }
      }
    }
    OUT.push(rec);
  }

  for (const [key, g] of [...radioGroups.entries(), ...checkboxGroups.entries()]) {
    OUT.push({
      jaa_id: `grp-${counter + 1}-${encodeURIComponent(key).slice(0, 40)}`,
      type: g.type === 'radio' ? 'radio' : 'checkbox_group',
      tag: 'group',
      name: key,
      dom_id: '',
      label: g.label,
      required: g.required,
      placeholder: '',
      maxlength: null,
      current_value: g.options.filter((o) => o.checked).map((o) => o.label).join(', '),
      options: g.options,
      accept: '',
    });
    counter++;
  }

  // ---- page context --------------------------------------------------------
  const meta = (sel, attr) => {
    const el = document.querySelector(sel);
    return el ? clean(el.getAttribute(attr) || '') : '';
  };
  const bodyText = clean(document.body ? document.body.innerText : '').slice(0, 4000);

  return {
    url: location.href,
    title: clean(document.title),
    og_title: meta('meta[property="og:title"]', 'content'),
    og_site: meta('meta[property="og:site_name"]', 'content'),
    h1: textOf(document.querySelector('h1')),
    body_text: bodyText,
    fields: OUT,
  };
})()
"""


APPLY_BUTTON_JS = r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], input[type="submit"]'));
  const wanted = /^(apply|apply now|apply for this job|easy apply|start application|apply to this job|submit application|i'?m interested)$/i;
  const loose  = /\bapply\b/i;
  const rect = (el) => el.getBoundingClientRect();
  const visible = (el) => {
    const s = getComputedStyle(el); const r = rect(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  let best = null, bestScore = -1;
  for (const el of nodes) {
    if (!visible(el)) continue;
    const t = clean(el.innerText || el.value || el.getAttribute('aria-label'));
    if (!t) continue;
    let score = -1;
    if (wanted.test(t)) score = 10;
    else if (loose.test(t) && t.length < 40) score = 4;
    if (score > bestScore) { bestScore = score; best = el; }
  }
  if (!best || bestScore < 4) return null;
  best.setAttribute('data-jaa-apply', '1');
  return clean(best.innerText || best.value || 'Apply');
})()
"""


SUBMIT_BUTTON_JS = r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a'));
  const strong = /^(submit application|submit your application|submit|send application|apply now|apply|finish|complete application)$/i;
  const weak = /\b(submit|apply|send)\b/i;
  const bad  = /\b(cancel|back|previous|save (for|as) later|clear|reset|search|sign in|log ?in|upload)\b/i;
  const visible = (el) => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  let best = null, bestScore = 0;
  for (const el of nodes) {
    if (!visible(el) || el.disabled) continue;
    const t = clean(el.innerText || el.value || el.getAttribute('aria-label'));
    if (!t || bad.test(t)) continue;
    let score = 0;
    if (strong.test(t)) score = 10;
    else if (weak.test(t) && t.length < 40) score = 5;
    if (el.type === 'submit') score += 2;
    if (score > bestScore) { bestScore = score; best = el; }
  }
  if (!best) return null;
  best.setAttribute('data-jaa-submit', '1');
  return clean(best.innerText || best.value || 'Submit');
})()
"""


_NOISE = re.compile(
    r"\s*(\*|\(required\)|\(optional\)|required|optional)\s*$", re.IGNORECASE
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def clean_question(label: str) -> str:
    q = (label or "").strip()
    for _ in range(3):
        q = _NOISE.sub("", q).strip()
    return q or "(unlabelled field)"


def normalize_question(label: str) -> str:
    """Key used by the answer bank. Stable across whitespace/punctuation/case."""
    q = clean_question(label).lower()
    q = _PUNCT.sub(" ", q)
    q = _WS.sub(" ", q).strip()
    return q


def token_set(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "of", "to", "in", "for", "your", "you", "please", "is",
        "are", "do", "does", "and", "or", "on", "at", "we", "us", "this", "that",
        "what", "have", "with", "will", "be", "any",
    }
    return {t for t in normalize_question(text).split() if t and t not in stop}


def similarity(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


IGNORE_PATTERNS = re.compile(
    r"\b(search|filter|keyword|query|newsletter|subscribe|coupon|promo|"
    r"password|confirm password|captcha|sort by)\b",
    re.IGNORECASE,
)


def is_noise_field(field: dict[str, Any]) -> bool:
    """Drop site-search boxes, newsletter signups, and password fields."""
    blob = f"{field.get('label','')} {field.get('name','')} {field.get('dom_id','')} {field.get('placeholder','')}"
    if field.get("type") == "password":
        return True
    if IGNORE_PATTERNS.search(blob):
        return True
    return False


async def extract_page(page) -> dict[str, Any]:
    """Run the extractor in the main frame plus every same-origin child frame."""
    frames_data: list[dict[str, Any]] = []
    for index, frame in enumerate(page.frames):
        try:
            data = await frame.evaluate(EXTRACT_JS)
        except Exception:
            continue
        if not data:
            continue
        for f in data.get("fields", []):
            f["frame"] = index
            f["question"] = clean_question(f.get("label", ""))
            f["normalized_question"] = normalize_question(f.get("label", ""))
        data["frame"] = index
        frames_data.append(data)

    fields: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for data in frames_data:
        for f in data.get("fields", []):
            if is_noise_field(f):
                continue
            key = (f["frame"], f["jaa_id"])
            if key in seen:
                continue
            seen.add(key)
            fields.append(f)

    main = frames_data[0] if frames_data else {}
    return {
        "url": page.url,
        "title": main.get("title", ""),
        "og_title": main.get("og_title", ""),
        "og_site": main.get("og_site", ""),
        "h1": main.get("h1", ""),
        "body_text": "\n\n".join(d.get("body_text", "") for d in frames_data)[:8000],
        "fields": fields,
        "frames": len(frames_data),
    }
