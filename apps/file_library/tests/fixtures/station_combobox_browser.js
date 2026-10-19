const input=document.querySelector('#station-combobox'), results=document.querySelector('#station-search-results'), chosen=document.querySelector('#chosen-station-id'), confirm=document.querySelector('#station-access-confirm'), activate=document.querySelector('#activate-station-access');
let submissions=0;
if(!activate) chosen.form.addEventListener('submit',event=>{ event.preventDefault(); submissions++; });
function check(value,message) { if(!value) throw Error(message); }
function key(value) { input.dispatchEvent(new KeyboardEvent('keydown',{key:value,bubbles:true,cancelable:true})); }
function render() { results.innerHTML='<ul id="station-listbox" role="listbox"><li id="station-option-0" role="option" data-station-id="a" data-station-label="Alpha">Alpha</li><li id="station-option-1" role="option" data-station-id="b" data-station-label="Alpine">Alpine</li></ul>'; results.dispatchEvent(new CustomEvent('htmx:afterSwap',{bubbles:true})); }
try {
  check(!chosen.disabled && (!activate || activate.disabled),'initial state');
  render(); key('ArrowDown');
  check(input.getAttribute('aria-activedescendant')==='station-option-0','arrow down');
  key('ArrowUp'); check(input.getAttribute('aria-activedescendant')==='station-option-1','arrow wrap');
  key('Enter'); check(chosen.value==='b' && (!activate || !activate.disabled) && results.hidden,'keyboard selection');
  if(!activate) check(submissions===1,'station selection immediately submits');
  if(confirm) confirm.checked=true;
  input.value='Other'; input.dispatchEvent(new Event('input',{bubbles:true}));
  check((activate ? !chosen.value : chosen.value==='b') && (!confirm || !confirm.checked) && (!activate || activate.disabled) && !input.hasAttribute('aria-activedescendant'),'search preserves active filter but clears temporary authority');
  check(!results.querySelector('[role="option"]'),'typing removes stale options');
  const detail={target:results,requestConfig:{parameters:{q:'Older'}},shouldSwap:true};
  results.dispatchEvent(new CustomEvent('htmx:beforeSwap',{bubbles:true,detail})); check(!detail.shouldSwap,'stale response rejected');
  render(); results.querySelector('[role="option"]').click();
  check(chosen.value==='a' && (!activate || !activate.disabled),'pointer selection');
  if(!activate) check(submissions===2,'pointer selection immediately submits');
  key('Escape'); check(chosen.value==='a' && results.hidden,'escape keeps selection');
  key('Tab'); check(input.getAttribute('aria-expanded')==='false','tab closes');
  results.querySelector('[role="listbox"]').replaceChildren(); results.dispatchEvent(new CustomEvent('htmx:afterSwap',{bubbles:true}));
  check(input.getAttribute('aria-expanded')==='false','empty results close listbox');
  if(activate) check(document.querySelector('#station-selection-status').textContent.includes('No matching'),'empty temporary results');
  if(!activate) {
    check(!confirm,'filter has no access confirmation');
    check(input.name==='q' && input.getAttribute('hx-params')==='q','search query uses native named field');
    check(chosen.form.getAttribute('hx-params')==='scope,station_id,state','filter excludes search text');
    check(new FormData(chosen.form).getAll('station_id').length===1,'no duplicate station parameters');
    const clear=document.querySelector('[data-station-clear]');
    check(clear.getAttribute('hx-get').includes('scope=station') && !clear.getAttribute('hx-get').includes('station_id'),'clear navigates immediately to all stations');
    const root=chosen.closest('#publication-browser'), clone=root.cloneNode(true);
    root.replaceWith(clone); document.body.dispatchEvent(new CustomEvent('htmx:historyRestore'));
    const restored=clone.querySelector('#station-combobox');
    restored.value='New search'; restored.dispatchEvent(new Event('input',{bubbles:true}));
    check(clone.querySelector('#station-search-results').hidden && !clone.querySelector('#station-search-results [role="option"]'),'history restores combobox handlers');
    check(clone.querySelector('#chosen-station-id').value==='a','history retains active station while searching');
  }
  document.querySelector('#result').textContent='PASS';
} catch(error) { document.querySelector('#result').textContent='FAIL: '+error.message; }
