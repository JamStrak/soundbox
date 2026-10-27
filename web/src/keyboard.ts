/** Keep transport shortcuts away from typing, IME and native form navigation. */
export function typingTarget(target:EventTarget|null) {
  if(!(target instanceof HTMLElement))return false;
  if(target.isContentEditable||target.closest('textarea,select,[role="textbox"],[role="combobox"]'))return true;
  const input=target.closest('input');
  return !!input&&!['range','checkbox','radio','button','submit','reset'].includes(input.type);
}
export function nativeSpaceTarget(target:EventTarget|null) {
  return target instanceof HTMLElement&&!!target.closest('input[type="checkbox"],input[type="radio"],summary,[role="checkbox"],[role="switch"]');
}
export function nativeNavigationTarget(target:EventTarget|null) {
  return target instanceof HTMLElement&&!!target.closest('input,textarea,select,summary,[role="slider"],[role="spinbutton"],[role="tab"]');
}
export function visibleDialog() {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"],[role="alertdialog"],dialog[open]')).find(element=>element.getClientRects().length>0);
}
export function isSpace(event:KeyboardEvent) {return event.code==='Space'||event.key===' ';}
