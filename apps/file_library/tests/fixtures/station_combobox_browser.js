
const input=document.querySelector('#station-combobox'), results=document.querySelector('#station-search-results'), chosen=document.querySelector('#chosen-station-id'), confirm=document.querySelector('#station-access-confirm'), activate=document.querySelector('#activate-station-access');
function check(value, message) { if(!value) throw Error(message); }
function key(value) { input.dispatchEvent(new KeyboardEvent('keydown',{key:value,bubbles:true,cancelable:true})); }
function render() { results.innerHTML='<ul id="station-listbox" role="listbox"><li id="station-option-0" role="option" data-station-id="a" data-station-label="Alpha">Alpha</li><li id="station-option-1" role="option" data-station-id="b" data-station-label="Alpine">Alpine</li></ul>'; results.dispatchEvent(new CustomEvent('htmx:afterSwap',{bubbles:true})); }
try {
check(!chosen.disabled && (!activate || activate.disabled),'initial state'); render(); key('ArrowDown'); check(input.getAttribute('aria-activedescendant')==='station-option-0','arrow down'); key('ArrowUp'); check(input.getAttribute('aria-activedescendant')==='station-option-1','arrow wrap'); key('Enter'); check(chosen.value==='b' && (!activate || !activate.disabled) && results.hidden,'keyboard selection'); if(confirm) confirm.checked=true; input.value='Other'; input.dispatchEvent(new Event('input',{bubbles:true})); check(!chosen.value && (!confirm || !confirm.checked) && (!activate || activate.disabled) && !input.hasAttribute('aria-activedescendant'),'typing clears selection'); check(!results.querySelector('[role="option"]'),'typing removes stale options'); const detail={target:results,requestConfig:{parameters:{q:'Older'}},shouldSwap:true}; results.dispatchEvent(new CustomEvent('htmx:beforeSwap',{bubbles:true,detail})); check(!detail.shouldSwap,'stale response rejected'); render(); results.querySelector('[role="option"]').click(); check(chosen.value==='a' && (!activate || !activate.disabled),'pointer selection'); key('Escape'); check(chosen.value==='a' && results.hidden,'escape keeps selection'); key('Tab'); check(input.getAttribute('aria-expanded')==='false','tab closes'); results.querySelector('[role="listbox"]').replaceChildren(); results.dispatchEvent(new CustomEvent('htmx:afterSwap',{bubbles:true})); check(input.getAttribute('aria-expanded')==='false' && document.querySelector('#station-selection-status').textContent.includes('No matching'),'empty results'); if(!activate) {
check(!confirm,'filter has no access confirmation');
const root=chosen.closest('[data-publication-station]'), form=chosen.form;
const request={parameters:{}};
input.dispatchEvent(new CustomEvent('htmx:configRequest',{bubbles:true,detail:request}));
check(request.parameters.q===input.value,'bounded search gets current query');
form.querySelector('[value="all"][name="scope"]').checked=true;
form.querySelector('[value="all"][name="scope"]').dispatchEvent(new Event('change',{bubbles:true}));
check(root.hidden && root.disabled && !new FormData(form).has('station_id'),'all scopes excludes station id');
form.querySelector('[value="station"][name="scope"]').checked=true;
form.querySelector('[value="station"][name="scope"]').dispatchEvent(new Event('change',{bubbles:true}));
check(!root.hidden && !root.disabled && new FormData(form).get('station_id')==='a','station scope restores selection');
check(new FormData(form).getAll('station_id').length===1,'no duplicate station parameters');
check(!new FormData(form).has('q'),'search text is not a station identifier');
const clone=root.closest('#publication-browser').cloneNode(true);
root.closest('#publication-browser').replaceWith(clone);
document.body.dispatchEvent(new CustomEvent('htmx:historyRestore'));
const restored=clone.querySelector('#station-combobox');
restored.value='New search'; restored.dispatchEvent(new Event('input',{bubbles:true}));
check(!clone.querySelector('#chosen-station-id').value,'history restores combobox handlers');
}
document.querySelector('#result').textContent='PASS';
} catch(error) { document.querySelector('#result').textContent='FAIL: '+error.message; }
