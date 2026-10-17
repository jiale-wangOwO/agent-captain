'use strict';

// Bookkeeping only: never stores draft text and never writes to the terminal.
// Unknown edits stay protected until an actual submit/cancel, not an idle timer.
const VERSION = 1;
const FIELDS = ['humanInputVersion', 'humanLineLength', 'humanLineKnown', 'humanPaste',
  'humanPasteBase', 'humanPasteHadData', 'inputEscape', 'inputString', 'inputStringEscape', 'humanLeaseUntil', 'humanInputRevision'];
const reservations = new WeakMap();

function initialize(session) {
  if (session.humanInputVersion === VERSION) return;
  // A legacy dirty bit contains no evidence of its source or actual length.
  session.humanLineKnown = !session.humanLineLength;
  session.humanLineLength = session.humanLineLength ? 1 : 0;
  session.humanInputVersion = VERSION;
  session.inputEscape = session.inputEscape || '';
  session.inputString = null;
  session.inputStringEscape = false;
  session.humanInputRevision = 0;
}

function captureHumanInput(session) {
  initialize(session);
  return Object.fromEntries(FIELDS.map(key => [key, session[key]]));
}

function updateHumanInput(session, data, now = Date.now()) {
  initialize(session);
  const touch = () => { session.humanLeaseUntil = now + 1500; session.humanInputRevision++; };
  const unknown = () => { session.humanLineKnown = false; session.humanLineLength = Math.max(1, session.humanLineLength); touch(); };
  const clear = () => { session.humanLineKnown = true; session.humanLineLength = 0; touch(); };
  const sequence = value => {
    if (value === '\x1b[200~') {
      session.humanPasteBase = {length: session.humanLineLength, known: session.humanLineKnown};
      session.humanPasteHadData = false; session.humanPaste = true; unknown(); return;
    }
    // Terminal replies and focus/mouse reports are not keyboard drafts. The
    // canonical headless terminal handles queries; do not renew a human lease.
    if (/^\x1b\[(?:\??\d+(?:;\d+)*R|[?>=]?[\d;]*c|[?>]?[\d;]*n|\??[\d;]*\$y|[\d;]*t|[IO]|<\d+;\d+;\d+[Mm])$/.test(value)) return;
    if (/^\x1b(?:\[[\d;]*[AB]|O[AB])$/.test(value)
        || /^\x1b\[(?:Z|(?:1;\d+)?[HF])$/.test(value)) { unknown(); return; }
    // Horizontal movement/delete cannot insert text into a known empty line,
    // but destroys exact accounting once a draft exists.
    if (/^\x1b(?:\[[\d;]*[CDHF]|O[CDHF]|\[(?:1|3|4|7|8)~)$/.test(value)) {
      if (session.humanLineLength) unknown(); else touch();
      return;
    }
    // Function keys do not insert text; unrecognized editing sequences fail
    // closed instead of pretending we know what the foreground application did.
    if (/^\x1b(?:O[P-S]|\[(?:11|12|13|14|15|17|18|19|20|21|23|24)~)$/.test(value)) { touch(); return; }
    unknown();
  };

  for (const char of data) {
    if (session.humanPaste) {
      // Paste payload is user text, even if it resembles OSC/DCS or contains
      // newlines/Ctrl+C. Only the exact paste-end delimiter is a protocol token.
      if (session.inputEscape || char === '\x1b') {
        session.inputEscape += char;
        if ('\x1b[201~'.startsWith(session.inputEscape)) {
          if (session.inputEscape === '\x1b[201~') {
            session.inputEscape = ''; session.humanPaste = false;
            if (!session.humanPasteHadData && session.humanPasteBase) {
              session.humanLineLength = session.humanPasteBase.length;
              session.humanLineKnown = session.humanPasteBase.known;
            }
            session.humanPasteBase = null; touch();
          }
          continue;
        }
        session.inputEscape = '';
      }
      session.humanPasteHadData = true; unknown(); continue;
    }
    if (char === '\x03' || char === '\r' || char === '\n') {
      session.inputString = null; session.inputStringEscape = false; session.inputEscape = ''; clear(); continue;
    }
    if (session.inputString) {
      // OSC uses BEL or ST; DCS/APC/PM/SOS use ST. No payload is retained.
      if (char === '\x9c' || session.inputStringEscape && char === '\\'
          || session.inputString === 'osc' && char === '\x07') {
        session.inputString = null; session.inputStringEscape = false;
      } else session.inputStringEscape = char === '\x1b';
      continue;
    }
    if (session.inputEscape) {
      session.inputEscape += char;
      if (session.inputEscape === '\x1b[' || session.inputEscape === '\x1bO') continue;
      if (/^\x1b[\]PX^_]$/.test(session.inputEscape)) {
        session.inputString = char === ']' ? 'osc' : 'string'; session.inputEscape = ''; unknown(); continue;
      }
      if (/^\x1b\[[0-?]*[ -/]*[@-~]$/.test(session.inputEscape) || /^\x1bO.$/.test(session.inputEscape)) {
        sequence(session.inputEscape); session.inputEscape = '';
      } else if (session.inputEscape.length > 128 || /^\x1b[^\[O]/.test(session.inputEscape)) {
        session.inputEscape = ''; unknown();
      }
      continue;
    }
    if (char === '\x1b') { session.inputEscape = char; continue; }
    if (char === '\x9b') { session.inputEscape = '\x1b['; continue; }
    if ('\x90\x98\x9d\x9e\x9f'.includes(char)) { session.inputString = char === '\x9d' ? 'osc' : 'string'; unknown(); continue; }
    if (!session.humanPaste && (char === '\x7f' || char === '\b')) {
      // DEL is not printable. A backspace on an empty prompt cannot create a
      // draft; exact simple typing can be fully erased without a sticky lock.
      if (session.humanLineKnown) session.humanLineLength = Math.max(0, session.humanLineLength - 1);
      touch(); continue;
    }
    if (char === '\t' || '\x0e\x10\x12\x13\x19\x1a\x1f'.includes(char)) { unknown(); continue; }
    if ('\x01\x05\x0b\x15\x17'.includes(char)) {
      if (session.humanLineLength) unknown(); else touch();
      continue;
    }
    if (char >= ' ' && char <= '~') { session.humanLineLength++; touch(); continue; }
    if (char.codePointAt(0) >= 0xa0) { unknown(); continue; }
    // Other controls do not prove a draft was cleared.
    touch();
  }
  // Incomplete control strings may be literal user edits. Do not leave an
  // unbounded parser that consumes subsequent input while claiming it is idle.
  // Real automatic OSC/DCS/DECRQM responses are suppressed at the UI parser.
  if (session.inputEscape.length > 1) unknown();
}

function reserveHumanInput(session, data, now = Date.now()) {
  let ledger = reservations.get(session);
  if (!ledger) { ledger = {base: captureHumanInput(session), pending: [], revision: session.humanInputRevision}; reservations.set(session, ledger); }
  // These closures live only as long as the existing pending terminal write;
  // no transcript/input body is persisted or included in public status.
  const entry = {apply: view => updateHumanInput(view, data, now)};
  ledger.pending.push(entry);
  const render = () => {
    const view = {...ledger.base};
    for (const item of ledger.pending) item.apply(view);
    for (const key of FIELDS) session[key] = view[key];
    session.humanInputRevision = ++ledger.revision;
    // Pending human writes retain precedence even behind a slow (>1.5s) probe.
    if (ledger.pending.length) session.humanLeaseUntil = Infinity;
    else reservations.delete(session);
  };
  render();
  let done = false;
  return (success, writeUncertain = false) => {
    if (done) return; done = true;
    if (ledger.pending[0] !== entry) throw Error('Human writes settled out of order');
    ledger.pending.shift();
    if (success) entry.apply(ledger.base);
    else if (writeUncertain) {
      // A throwing sink cannot prove that no bytes reached the application.
      // Preserve original drafts and require user reconciliation, never clear.
      ledger.base.humanLineLength = Math.max(1, ledger.base.humanLineLength);
      ledger.base.humanLineKnown = false;
    }
    render();
  };
}

function humanInputState(session, now = Date.now()) {
  initialize(session);
  const draft = !!session.humanLineLength;
  const typing = now < session.humanLeaseUntil;
  return { state: typing ? 'typing' : draft ? 'draft' : 'idle',
    reason: draft ? session.humanLineKnown ? 'unfinished_input' : 'uncertain_input' : typing ? 'recent_input' : null };
}

module.exports = { updateHumanInput, captureHumanInput, reserveHumanInput, humanInputState };
