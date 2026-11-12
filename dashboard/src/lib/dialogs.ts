// Small pure rules of the dialogs (components/ui/Dialogs.tsx and the pages' sheets), unit-tested.

/** The acknowledgement tick of a confirmation, with the `open` it belongs to. */
export interface AckState {
  open: boolean;
  ack: boolean;
}

/** The tick is cleared whenever the dialog opens or closes, whoever changed `open`: Radix reports
 * only its own closes (Cancel, Escape), not a parent closing it after a confirmed action, so a
 * second opening would otherwise start ticked (kill switch OFF, a real billed test node). */
export function ackFor(state: AckState, open: boolean): AckState {
  return state.open === open ? state : { open, ack: false };
}

/** A sheet stays open while its request runs: closing it then would hide the answer (an error
 * is shown only inside it). Opening is always allowed. */
export function dismissAllowed(busy: boolean, next: boolean): boolean {
  return next || !busy;
}
