(async function () {
  function check(value, message) { if (!value) throw Error(message); }
  function find(selector) { return document.querySelector(selector); }
  function waitFor(condition) {
    return new Promise((resolve, reject) => {
      const deadline = Date.now() + 4000;
      function poll() {
        if (condition()) return resolve();
        if (Date.now() >= deadline) return reject(Error('Timed out waiting for move dialog'));
        setTimeout(poll, 20);
      }
      poll();
    });
  }
  function link(label) {
    return Array.from(document.querySelectorAll('#move-destination-browser a'))
      .find(el => el.textContent.trim().startsWith(label));
  }
  try {
    await waitFor(() => document.readyState === 'complete');
    const opener = find('[data-move-test-entry]');
    find('#entry-' + opener.dataset.moveTestEntry + ' a[hx-get*="/move/"]').click();
    await waitFor(() => find('#file-explorer-form-modal.show') && find('.modal-backdrop.show'));
    const modal = find('#file-explorer-form-modal');
    const form = modal.querySelector('form');
    const header = modal.querySelector('.modal-header');
    const footer = modal.querySelector('.modal-footer');
    const submit = footer.querySelector('[type="submit"]');
    const backdrop = find('.modal-backdrop');
    const instance = bootstrap.Modal.getInstance(modal);
    let lifecycleEvents = 0;
    for (const event of ['show.bs.modal', 'shown.bs.modal', 'hide.bs.modal', 'hidden.bs.modal']) {
      modal.addEventListener(event, () => lifecycleEvents++);
    }
    // Wait for the initial opening transition before measuring later navigation.
    await waitFor(() => !instance._isTransitioning);
    lifecycleEvents = 0;
    function mounted() {
      check(find('#file-explorer-form-modal') === modal && modal.classList.contains('show'), 'same open modal');
      check(modal.querySelector('form') === form && modal.querySelector('.modal-header') === header, 'form and header stay mounted');
      check(modal.querySelector('.modal-footer') === footer && footer.querySelector('[type="submit"]') === submit, 'footer and Move here stay mounted');
      check(find('.modal-backdrop') === backdrop && document.querySelectorAll('.modal-backdrop').length === 1, 'same single backdrop');
      check(bootstrap.Modal.getInstance(modal) === instance && lifecycleEvents === 0, 'modal never reopened');
      check(form.querySelectorAll('[name="destination"]').length === 1, 'exactly one submitted destination');
    }
    async function navigate(label, condition) {
      const old = find('#move-destination-browser');
      const target = link(label);
      check(target, 'navigation link exists: ' + label);
      target.click();
      await waitFor(() => find('#move-destination-browser') !== old && condition());
      mounted();
    }
    await navigate('Target', () => form.elements.destination.value === opener.dataset.moveTestTarget);
    await navigate('Next', () => find('#move-destination-browser').textContent.includes('Page 2 of 2'));
    check(form.elements.destination.value === opener.dataset.moveTestTarget, 'pagination keeps destination');
    await navigate('Previous', () => find('#move-destination-browser').textContent.includes('Page 1 of 2'));
    await navigate('Child 00', () => form.elements.destination.value !== opener.dataset.moveTestTarget);
    await navigate('Up one level', () => form.elements.destination.value === opener.dataset.moveTestTarget);
    await navigate('Files', () => form.elements.destination.value === '');
    await navigate('Target', () => form.elements.destination.value === opener.dataset.moveTestTarget);
    let completed = false;
    document.body.addEventListener('htmx:beforeOnLoad', event => {
      if (event.detail.xhr.status === 204 && event.detail.xhr.getResponseHeader('HX-Redirect')) {
        // Keep the result marker visible after the real server-side move succeeds.
        event.preventDefault();
        completed = true;
      }
    });
    submit.click();
    await waitFor(() => completed);
    find('#result').textContent = 'PASS';
  } catch (error) { find('#result').textContent = 'FAIL: ' + error.message; }
})();
