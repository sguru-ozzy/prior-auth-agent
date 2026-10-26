limits => {
  const visible = e => !!e && !!e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden' && !e.closest('[inert],[aria-hidden="true"]');
  const text = e => (e?.innerText || e?.textContent || '').replace(/\s+/g, ' ').trim();
  const accessible = e => e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => text(document.getElementById(id))).join(' ') ||
    [...(e.labels || [])].map(text).join(' ') ||
    (e.matches('button,a,[role=button],[role=option],[role=tab]') ? text(e) : '') ||
    (e.tagName === 'INPUT' && ['submit','button'].includes(e.type) ? e.value : '') ||
    e.getAttribute('placeholder') || '';
  const nodes = [...document.querySelectorAll(
    'input:not([type=hidden]),textarea,select,button,a[href],[role=button],[role=option],'+
    '[role=tab],[role=checkbox],[role=radio],[role=combobox],[role=textbox]'
  )].filter(visible);
  document.querySelectorAll('[data-paa-ref]').forEach(e => e.removeAttribute('data-paa-ref'));
  nodes.forEach((e, i) => e.setAttribute('data-paa-ref', 'u' + i));
  const heading = e => text(e?.querySelector('legend,h1,h2,h3,h4,[role=heading]'));
  const pageHeading = text([...document.querySelectorAll('h1,[role=heading]')].find(visible));
  const controls = nodes.slice(0, limits.max_controls).map((e, i) => {
    const tag = e.tagName.toLowerCase();
    const kind = (e.getAttribute('type') || tag).toLowerCase();
    const role = e.getAttribute('role') || (tag === 'select' ? 'combobox' :
      ['radio','checkbox'].includes(kind) ? kind : tag === 'a' ? 'link' :
      tag === 'button' || ['button','submit'].includes(kind) ? 'button' : 'textbox');
    const group = e.closest('fieldset,[role=radiogroup],[role=group],[role=listbox]');
    const section = e.closest('section,article,[role=region],form');
    const form = e.form || e.closest('form');
    return {ref:'u'+i, label:accessible(e), evidence:text(e.closest('[role=option],label,li') || e),
      region_text:(e.closest('fieldset,section,[role=region],form')?.innerText || ''),
      group:group ? accessible(group) || heading(group) : '',
      context:[section?.getAttribute('aria-label') || heading(section),pageHeading].filter(Boolean).join(' | '),
      role,kind, value:kind === 'password' ? '' : String(e.value || ''),
      checked:!!e.checked || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true' || e.getAttribute('aria-pressed') === 'true',
      invalid:e.getAttribute('aria-invalid') === 'true' || !!(e.willValidate && !e.validity.valid),
      enabled:!e.disabled && e.getAttribute('aria-disabled') !== 'true',
      required:!!e.required || e.getAttribute('aria-required') === 'true' ||
        !!group?.querySelector('[required],[aria-required=true]'),
      href:tag === 'a' ? e.href : '',form_action:form?.action || '',
      form_method:form ? form.method.toUpperCase() : '',
      options:tag === 'select' ? [...e.options].map(o => ({label:text(o),value:o.value,disabled:o.disabled})) : []};
  });
  const pairs = [];
  const add = (label,value) => {if(label && value) pairs.push({label,value});};
  document.querySelectorAll('dt').forEach(e => {if(visible(e) && e.nextElementSibling?.matches('dd'))
    add(text(e),text(e.nextElementSibling));});
  document.querySelectorAll('tr,[role=row]').forEach(e => {
    const cells = [...e.querySelectorAll(':scope > th,:scope > td,:scope > [role=cell],:scope > [role=rowheader]')];
    if(visible(e) && cells.length === 2) add(text(cells[0]),text(cells[1]));
  });
  document.querySelectorAll('label,[data-label]').forEach(e => {
    if(!visible(e)) return;
    const field = e.control;
    if(field && !['password','hidden'].includes(field.type)) {
      const value = field.tagName === 'SELECT' ? text(field.selectedOptions[0]) : field.value;
      if(!['radio','checkbox'].includes(field.type)) add(text(e),String(value || ''));
    } else if(e.nextElementSibling && !e.nextElementSibling.querySelector('input,select,textarea')) {
      add(text(e),text(e.nextElementSibling));
    }
  });
  document.querySelectorAll('p,li,div,span').forEach(e => {
    if(!visible(e) || e.children.length > 2 || e.querySelector('input,select,textarea,button,a')) return;
    const children = [...e.children];
    if(children.length === 2 && !children.some(c => c.children.length)) add(text(children[0]),text(children[1]));
    if(!e.children.length) {
      const match = text(e).match(/^([^:]{1,100}):\s+(.+)$/);
      if(match) add(match[1],match[2]);
    }
  });
  const tables = [...document.querySelectorAll('table,[role=table],[role=grid]')].filter(visible).map(t => {
    const headers = [...t.querySelectorAll('thead th,[role=columnheader]')].map(text);
    if(!headers.length) headers.push(...[...t.querySelectorAll('tr:first-child th')].map(text));
    const rows = [...t.querySelectorAll('tbody tr,[role=row]')].filter(visible).map(r => ({
      cells:[...r.querySelectorAll('td,[role=cell]')].map(text),
      controls:[...r.querySelectorAll('[data-paa-ref]')].map(e => e.getAttribute('data-paa-ref')),
      text:text(r)})).filter(r => r.cells.length);
    return {headers,rows};
  });
  // Card/list layouts expose label-value pairs as cells and retain observed link references.
  const cards = [...document.querySelectorAll('article,[role=listitem]')].filter(e => visible(e) &&
    !e.closest('table,[role=table]') && e.querySelector('a[href],button'));
  if(cards.length) tables.push({headers:[],rows:cards.map(r => {
    const cardPairs = [...r.querySelectorAll('dt')].filter(e => e.nextElementSibling?.matches('dd'))
      .map(e => ({label:text(e),value:text(e.nextElementSibling)}));
    r.querySelectorAll('p,div,span').forEach(e => {
      if(e.children.length === 2 && [...e.children].every(c => !c.children.length))
        cardPairs.push({label:text(e.children[0]),value:text(e.children[1])});
      if(!e.children.length) {const m=text(e).match(/^([^:]{1,100}):\s+(.+)$/);
        if(m) cardPairs.push({label:m[1],value:m[2]});}
    });
    return {cells:[],pairs:cardPairs,text:text(r),
      controls:[...r.querySelectorAll('[data-paa-ref]')].map(e => e.getAttribute('data-paa-ref'))};
  })});
  const body = document.body?.innerText || '';
  return {url:location.href,title:document.title,text:body.slice(0,limits.max_text_length),
    alerts:[...document.querySelectorAll('[role=alert]')].filter(visible).map(text),controls,pairs,tables,
    busy:!![...document.querySelectorAll('[aria-busy=true],[role=progressbar]')].find(visible),
    truncated:nodes.length > limits.max_controls || body.length > limits.max_text_length};
}
