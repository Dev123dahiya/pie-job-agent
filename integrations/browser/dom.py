"""Fixed, read-only page scripts. These constants are the ONLY JavaScript PIE runs in a page;
nothing from Claude, the user or a tool argument is ever evaluated.

EXTRACT_FORM (argument: a field-id prefix for the frame, or {prefix, host} to read a form
inside an element's open shadow root, or {prefix, root} to read only inside one element, e.g.
LinkedIn's Easy Apply panel) describes the page's form controls
and tags each with a ``data-pie-field`` attribute so PIE can address it later. It returns
labels, names, ids, autocomplete, placeholder, required-ness, options, the currently selected
option label of dropdowns and whether a value is present - never typed values themselves.

Challenge signals are taken from interactive UI only (``ui_text``: text next to inputs,
buttons, dialogs and forms), never from the whole page, so a job description that merely
mentions "2FA" or "captcha" does not stop an application.
"""

EXTRACT_FORM = r"""
(arg) => {
  // arg: the field-id prefix, or {prefix, host}: read the form inside host's (open) shadow root.
  const prefix = typeof arg === 'string' ? arg : arg.prefix;
  // arg.root: read only inside this element (e.g. the Easy Apply panel), wherever it is.
  const hostEl = typeof arg === 'string' || !arg.host ? null : document.querySelector(arg.host);
  const doc = (typeof arg !== 'string' && arg.root) || (hostEl && hostEl.shadowRoot) || document;
  const clean = (t, n = 300) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const visible = (el) => {
    if ((el.getAttribute('type') || '').toLowerCase() === 'file') return true;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const byId = (id) => (!id ? null : doc.getElementById ? doc.getElementById(id)
    : doc.querySelector(`#${CSS.escape(id)}`));
  const labelOf = (el) => {
    let t = '';
    if (el.id) {
      const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) t = l.innerText;
    }
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t && el.getAttribute('aria-labelledby')) {
      t = el.getAttribute('aria-labelledby').split(/\s+/).map((i) => byId(i)?.innerText || '').join(' ');
    }
    if (!t) t = el.getAttribute('aria-label') || '';
    return clean(t);
  };
  const container = (el) => el.closest('fieldset, [role=radiogroup], .field, .form-group, li, .question') || el.parentElement;
  const around = (el) => clean(container(el)?.innerText || '', 400);
  const errorNear = (el) => {
    const c = container(el);
    if (!c) return '';
    const e = c.querySelector('.error, .field-error, [role=alert], .invalid-feedback, [class*="error"]');
    return e ? clean(e.innerText, 200) : '';
  };
  // Text of the smallest ancestor (up to 4 levels, <= 600 chars) around an interactive element.
  const localText = (el) => {
    let best = '';
    let node = el.parentElement;
    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
      const t = node.innerText || '';
      if (t.length > 600) break;
      best = t;
    }
    return clean(best, 600);
  };
  // Custom dropdowns (react-select style): the value shown next to the combobox input.
  const comboRoot = (el) => el.closest('[class*="select__control"], [class*="-control"], [data-combobox-root], [class*="select-shell"]') || el.parentElement?.parentElement || el.parentElement;
  const comboValue = (el) => {
    if (el.tagName.toLowerCase() !== 'input' && !el.querySelector('input')) {
      return clean(el.getAttribute('data-value-label') || el.innerText, 200);
    }
    const root = comboRoot(el);
    const v = root ? root.querySelector('[class*="single-value"], [class*="singleValue"], [data-value-label]') : null;
    return v ? clean(v.getAttribute('data-value-label') || v.innerText, 200) : '';
  };
  const isRequired = (el, label) =>
    el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label);

  const fields = [];
  const groups = {};
  const uiText = [];
  let n = 0;
  const pushCombo = (el, id) => {
    const label = labelOf(el);
    const selected = comboValue(el);
    const isInput = el.tagName.toLowerCase() === 'input';
    fields.push({
      id, kind: 'combobox', tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
      name: el.name || el.getAttribute('name') || '', html_id: el.id || '', label,
      placeholder: clean(el.getAttribute('placeholder') || comboRoot(el)?.querySelector('[class*="placeholder"]')?.innerText),
      autocomplete: el.getAttribute('autocomplete') || '', required: isRequired(el, label), options: [],
      surrounding: around(el), error: errorNear(el), role: el.getAttribute('role') || '', accept: '',
      invalid: el.getAttribute('aria-invalid') === 'true', has_value: selected !== '',
      selected_label: selected, aria_expanded: el.getAttribute('aria-expanded') || '',
      aria_controls: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '',
      searchable: isInput && !el.readOnly, file_names: [],
    });
  };

  for (const el of doc.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (el.disabled || !visible(el)) continue;
    uiText.push(localText(el));
    const grouped = (type === 'radio' || type === 'checkbox') && el.name &&
      doc.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1;
    if (grouped) {
      let g = groups[el.name];
      if (!g) {
        const c = container(el);
        const legend = c?.querySelector('legend');
        const question = clean(legend?.innerText || c?.getAttribute('aria-label') ||
          (c?.innerText || '').split('\n')[0]);
        g = groups[el.name] = {
          id: `${prefix}${n++}`, kind: type === 'radio' ? 'radio' : 'checkbox_group',
          tag, type, name: el.name, html_id: '', label: question, placeholder: '',
          autocomplete: '', required: false, options: [], has_value: false, selected_label: '',
          surrounding: around(el), error: errorNear(el), role: '', accept: '', invalid: false,
        };
        fields.push(g);
      }
      el.setAttribute('data-pie-field', g.id);
      el.setAttribute('data-pie-option', String(g.options.length));
      g.options.push({ label: labelOf(el), value: el.value });
      g.required = g.required || isRequired(el, g.label);
      if (el.checked) { g.has_value = true; g.selected_label = labelOf(el); }
      continue;
    }
    const id = `${prefix}${n++}`;
    el.setAttribute('data-pie-field', id);
    const role = el.getAttribute('role') || '';
    if (role === 'combobox') { pushCombo(el, id); continue; }
    const label = labelOf(el);
    let kind = 'text';
    if (tag === 'select') kind = 'select';
    else if (tag === 'textarea') kind = 'textarea';
    else if (type === 'file') kind = 'file';
    else if (type === 'checkbox') kind = 'checkbox';
    else if (type === 'password') kind = 'password';
    else if (['email', 'tel', 'url', 'number', 'date'].includes(type)) kind = type;
    const field = {
      id, kind, tag, type, name: el.name || '', html_id: el.id || '', label,
      placeholder: clean(el.getAttribute('placeholder')), autocomplete: el.getAttribute('autocomplete') || '',
      required: isRequired(el, label), options: [], surrounding: around(el), error: errorNear(el),
      role, accept: el.getAttribute('accept') || '', selected_label: '',
      invalid: el.getAttribute('aria-invalid') === 'true' || (el.value !== '' && el.validity && !el.validity.valid),
      has_value: kind === 'file' ? el.files.length > 0 : kind === 'checkbox' ? el.checked : el.value !== '',
      file_names: kind === 'file' ? Array.from(el.files).map((f) => f.name) : [],
    };
    if (tag === 'select') {
      field.options = Array.from(el.options).map((o) => ({ label: clean(o.text), value: o.value }));
      field.has_value = el.value !== '';
      field.selected_label = el.value !== '' ? clean(el.options[el.selectedIndex]?.text) : '';
    }
    fields.push(field);
  }
  // Custom dropdowns that are not inputs (buttons/divs opening a listbox).
  for (const el of doc.querySelectorAll('[role=combobox]:not(input), [aria-haspopup="listbox"]:not(input):not(select)')) {
    if (el.closest('[data-pie-field]') || el.querySelector('[data-pie-field]') || !visible(el)) continue;
    const id = `${prefix}${n++}`;
    el.setAttribute('data-pie-field', id);
    uiText.push(localText(el));
    pushCombo(el, id);
  }

  const buttons = [];
  let b = 0;
  const THIRD_PARTY = /linkedin|indeed|google|facebook|apple|github|seek/i;
  for (const el of doc.querySelectorAll('button, input[type=submit], [role=button]')) {
    if (el.disabled || !visible(el)) continue;
    const text = clean(el.innerText || el.value || el.getAttribute('aria-label'));
    uiText.push(text);
    const submitType = (el.getAttribute('type') || '').toLowerCase() === 'submit' && !!el.closest('form');
    if (!(submitType || /^(submit|apply|send)( my| your)?( application)?$/i.test(text))) continue;
    if (THIRD_PARTY.test(text)) continue; // "Apply with LinkedIn" etc. are never used
    const id = `${prefix}s${b++}`;
    el.setAttribute('data-pie-submit', id);
    buttons.push({ id, label: text });
  }
  const applyLinks = [];
  let a = 0;
  for (const el of doc.querySelectorAll('a, button')) {
    const text = clean(el.innerText, 60);
    if (!/^apply( now| for this (job|position|role))?$/i.test(text) || !visible(el)) continue;
    const id = `${prefix}a${a++}`;
    el.setAttribute('data-pie-apply', id);
    applyLinks.push({ id, label: text });
  }
  for (const d of doc.querySelectorAll('[role=dialog], [aria-modal="true"]')) {
    if (visible(d)) uiText.push(clean(d.innerText, 600));
  }

  const q = (s) => doc.querySelectorAll(s).length;
  const body = document.body ? document.body.innerText : '';
  return {
    fields,
    submit_buttons: buttons,
    apply_buttons: applyLinks,
    signals: {
      iframes: Array.from(document.querySelectorAll('iframe')).map((f) => f.src || '').slice(0, 40),
      captcha_elements: q('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #challenge-form, #px-captcha, iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="arkoselabs"]'),
      password_inputs: Array.from(document.querySelectorAll('input[type=password]')).filter(visible).length,
      otp_inputs: Array.from(document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="otp" i], input[id*="otp" i], input[name*="verification_code" i]')).filter(visible).length,
      ui_text: clean(Array.from(new Set(uiText)).join(' \n '), 8000),
      text: clean(body, 20000),
      text_length: body.length,
    },
  };
}
"""

# Current label shown by a custom dropdown (react-select style). Constant, read-only.
COMBOBOX_VALUE = r"""
(el) => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  if (el.tagName.toLowerCase() !== 'input' && !el.querySelector('input')) {
    return clean(el.getAttribute('data-value-label') || el.innerText);
  }
  const root = el.closest('[class*="select__control"], [class*="-control"], [data-combobox-root], [class*="select-shell"]') || el.parentElement?.parentElement || el.parentElement;
  const v = root ? root.querySelector('[class*="single-value"], [class*="singleValue"], [data-value-label]') : null;
  return v ? clean(v.getAttribute('data-value-label') || v.innerText) : '';
}
"""

# From the "Apply to ..." heading, the nearest enclosing element that also holds a step button
# (Next / Review / Submit application): the Easy Apply panel. Crosses shadow-root boundaries.
FIND_PANEL = r"""
(heading) => {
  const STEP = /^(next|review|submit application)$/i;
  const ARIA = /continue to next step|review your application|submit application/i;
  const hasStep = (el) => [...el.querySelectorAll('button')].some((b) =>
    STEP.test((b.innerText || '').trim()) || ARIA.test(b.getAttribute('aria-label') || ''));
  let el = heading;
  for (let i = 0; i < 15 && el; i++) {
    el = el.parentElement || (el.getRootNode() && el.getRootNode().host) || null;
    if (el && hasStep(el)) return el;
  }
  return heading.parentElement;
}
"""
