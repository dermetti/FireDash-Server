(async function () {
  if (window.publicationFilterTestRunning) return;
  window.publicationFilterTestRunning = true;
  function check(value, message) { if (!value) throw Error(message); }
  function find(selector) { return document.querySelector(selector); }
  function waitFor(condition) {
    return new Promise((resolve, reject) => {
      const deadline = Date.now() + 3000;
      function checkReady() {
        if (condition()) return resolve();
        if (Date.now() >= deadline) return reject(Error('Timed out waiting for publication UI'));
        setTimeout(checkReady, 20);
      }
      checkReady();
    });
  }
  function rows() { return document.querySelectorAll('#publication-scope-results tbody tr').length; }
  function query() { return new URLSearchParams(location.search); }
  function key(value) { find('#station-combobox').dispatchEvent(new KeyboardEvent('keydown', {key:value,bubbles:true,cancelable:true})); }
  try {
    await waitFor(() => find('#station-combobox') && !find('#station-combobox').disabled);
    check(rows() === 2 && query().get('scope') === 'station', 'all-stations initial results');
    const scopeHeading = find('#publication-browser nav h2');
    const reset = find('#publication-reset');
    check(scopeHeading.textContent === 'Publication scope' && reset.textContent === 'Reset all filters', 'scope heading and global reset label');
    check(reset.parentElement === scopeHeading.parentElement && !reset.closest('form'), 'global reset belongs to scope heading row');
    const headingBounds = scopeHeading.getBoundingClientRect();
    const resetBounds = reset.getBoundingClientRect();
    check(resetBounds.left > headingBounds.right && resetBounds.top < headingBounds.bottom, 'global reset sits to the right of the heading');
    check(find('#scope-department').getBoundingClientRect().top >= headingBounds.bottom, 'scope buttons sit beneath heading');
    check(find('#publication-state-filter').getBoundingClientRect().top > find('#station-combobox').getBoundingClientRect().bottom, 'station and status stack at every viewport');
    check(getComputedStyle(find('#station-selection-status')).position === 'absolute', 'selection announcement stays visually hidden');
    const input = find('#station-combobox');
    input.value = 'North'; input.dispatchEvent(new Event('input', {bubbles:true}));
    await waitFor(() => find('#station-option-0') && find('#station-option-0').textContent.includes('North'));
    check(rows() === 2 && !query().has('station_id'), 'typing does not filter publications');
    check(input.getAttribute('aria-expanded') === 'true', 'live search expands listbox');
    key('ArrowDown'); check(input.getAttribute('aria-activedescendant') === 'station-option-0', 'keyboard highlight');
    key('Escape'); check(input.getAttribute('aria-expanded') === 'false', 'escape closes results');
    key('ArrowDown'); key('Enter');
    await waitFor(() => rows() === 1 && query().has('station_id') && find('[data-station-clear]') && !find('[data-station-clear]').hidden);
    check(find('#station-selection-status').textContent.includes('North (N)'), 'selected station confirmation');
    // A status change must transmit the selected UUID exactly once and omit search text.
    find('#publication-state-filter').value = 'scheduled';
    find('#publication-state-filter').dispatchEvent(new Event('change', {bubbles:true}));
    await waitFor(() => query().get('state') === 'scheduled' && rows() === 1);
    check(query().getAll('station_id').length === 1 && !query().has('q'), 'coherent filter URL');
    find('[data-station-clear]').click();
    await waitFor(() => !query().has('station_id') && find('#station-selection-status').textContent.includes('Showing all stations'));
    check(query().get('state') === 'scheduled' && rows() === 1, 'clear keeps status');
    check(find('#station-combobox').value === '' && find('#station-combobox').placeholder === 'Search station name or short code', 'reset restores empty input and placeholder');
    check(find('[data-station-clear]').hidden, 'station reset hides without selection');
    history.back();
    await waitFor(() => query().has('station_id') && !find('#station-combobox').disabled);
    // Cached HTMX DOM must initialize again; searches must fetch after restoration.
    const restored = find('#station-combobox');
    restored.value = 'UI'; restored.dispatchEvent(new Event('input', {bubbles:true}));
    await waitFor(() => find('#station-option-0') && find('#station-option-0').textContent.includes('UI Station'));
    check(query().has('station_id'), 'search keeps current filter');
    find('#station-option-0').click();
    await waitFor(() => find('#station-selection-status').textContent.includes('UI Station (UIS)') && rows() === 0);
    find('#scope-all').click();
    await waitFor(() => query().get('scope') === 'all' && !find('#station-combobox'));
    check(query().get('state') === 'scheduled' && !query().has('station_id') && rows() === 1, 'scope navigation preserves status only');
    find('#scope-department').click();
    await waitFor(() => query().get('scope') === 'department' && rows() === 0);
    check(find('#scope-department').getAttribute('aria-current') === 'page', 'visible active scope');
    find('#publication-state-filter').value = '';
    find('#publication-state-filter').dispatchEvent(new Event('change', {bubbles:true}));
    await waitFor(() => !query().get('state') && rows() === 1);
    find('#scope-all').click();
    await waitFor(() => query().get('scope') === 'all' && rows() === 3);
    find('#publication-state-filter').value = 'scheduled';
    find('#publication-state-filter').dispatchEvent(new Event('change', {bubbles:true}));
    await waitFor(() => query().get('state') === 'scheduled' && rows() === 1);
    find('#publication-reset').click();
    await waitFor(() => !query().has('scope') && rows() === 1);
    check(find('#scope-department').getAttribute('aria-current') === 'page', 'reset restores department');
    check(!query().has('state') && !query().has('station_id') && !query().has('page') && find('#publication-state-filter').value === '', 'reset clears all filter URL state');
    history.back();
    await waitFor(() => query().get('scope') === 'all' && find('#publication-state-filter').value === 'scheduled');
    history.forward();
    await waitFor(() => !query().has('scope') && find('#scope-department').getAttribute('aria-current') === 'page');
    for (const control of document.querySelectorAll('#publication-browser nav a, #publication-filter-form select, #publication-reset')) {
      const bounds = control.getBoundingClientRect();
      check(bounds.left >= 0 && bounds.right <= innerWidth, 'filter controls fit viewport');
    }
    find('#result').textContent = 'PASS';
  } catch (error) { find('#result').textContent = 'FAIL: ' + error.message; }
})();
