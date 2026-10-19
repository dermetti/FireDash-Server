/* Progressive enhancement only; the server always validates station authority. */
(function () {
  function initialize() {
    document.querySelectorAll('[data-station-access]').forEach(function (root) {
      if (root.dataset.initialized || !window.htmx) return;
      root.dataset.initialized = 'true';
      const input = root.querySelector('#station-combobox');
      const chosen = root.querySelector('#chosen-station-id');
      const results = root.querySelector('#station-search-results');
      const status = root.querySelector('#station-selection-status');
      const confirm = root.querySelector('#station-access-confirm');
      const activate = root.querySelector('#activate-station-access');
      root.querySelector('[data-station-enhanced]').hidden = false;
      root.querySelectorAll('[data-station-fallback]').forEach(function (el) { el.hidden = true; });
      root.querySelector('#station-select-fallback').disabled = true;
      chosen.disabled = false;
      activate.disabled = !chosen.value;
      let index = -1;
      function options() { return Array.from(results.querySelectorAll('[role="option"]')); }
      function highlight(next) {
        const rows = options();
        index = next;
        rows.forEach(function (row, i) {
          row.setAttribute('aria-selected', String(i === index));
          row.classList.toggle('active', i === index);
        });
        if (rows[index]) input.setAttribute('aria-activedescendant', rows[index].id);
        else input.removeAttribute('aria-activedescendant');
      }
      function close() {
        results.hidden = true;
        input.setAttribute('aria-expanded', 'false');
        highlight(-1);
      }
      function select(row) {
        if (chosen.value !== row.dataset.stationId) confirm.checked = false;
        chosen.value = row.dataset.stationId;
        input.value = row.dataset.stationLabel;
        status.textContent = 'Selected: ' + row.dataset.stationLabel;
        activate.disabled = false;
        close();
      }
      input.addEventListener('input', function () {
        chosen.value = '';
        confirm.checked = false;
        activate.disabled = true;
        status.textContent = 'No station selected.';
        close();
        results.querySelector('[role="listbox"]').replaceChildren();
      });
      input.addEventListener('keydown', function (event) {
        const rows = options();
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          if (!rows.length) return;
          results.hidden = false;
          input.setAttribute('aria-expanded', 'true');
          highlight(index < 0 ? (event.key === 'ArrowDown' ? 0 : rows.length - 1)
            : (index + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length);
        } else if (event.key === 'Enter') {
          event.preventDefault();
          if (!results.hidden && rows[index]) select(rows[index]);
        } else if (event.key === 'Escape') {
          if (input.getAttribute('aria-expanded') === 'true') {
            event.preventDefault();
            event.stopPropagation();
          }
          close();
        } else if (event.key === 'Tab') close();
      });
      results.addEventListener('click', function (event) {
        const row = event.target.closest('[role="option"]');
        if (row && results.contains(row)) { select(row); input.focus(); }
      });
      root.addEventListener('htmx:beforeSwap', function (event) {
        if (event.detail.target !== results) return;
        const query = event.detail.requestConfig.parameters.q;
        if (query !== input.value || chosen.value) event.detail.shouldSwap = false;
      });
      root.addEventListener('htmx:afterSwap', function (event) {
        if (event.target !== results) return;
        highlight(-1);
        results.hidden = false;
        input.setAttribute('aria-expanded', String(options().length > 0));
        status.textContent = options().length ? 'Choose a matching station.' : 'No matching active stations.';
      });
      chosen.form.addEventListener('submit', function (event) {
        if (!chosen.value) { event.preventDefault(); input.focus(); }
      });
    });
  }
  initialize();
  document.body.addEventListener('htmx:afterSwap', initialize);
})();
