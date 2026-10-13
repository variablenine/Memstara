// The confirm step for anything that changes the node: a dialog that only
// goes ahead once a word has been typed, so a stray key press can't set
// one off. The typed word is sent along and the server checks it again.
import { $, setText, setData } from "./dom.js";

let pending = null;

export function initConfirm() {
  const word = $("cf-word");
  word.addEventListener("input", () => { $("cf-yes").disabled = !typedOk(); });
  word.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  $("cf-yes").addEventListener("click", go);
  $("cf-no").addEventListener("click", () => $("confirm").close());
  $("confirm").addEventListener("close", () => {
    const p = pending;
    pending = null;
    if (p && p.onClose) p.onClose();
  });
}

// opts: {title, body, word, action, run: async (typedWord) => message, done?: (message) => void}
export function askConfirm(opts) {
  const d = $("confirm");
  if (d.open) d.close();
  pending = { ...opts };
  setText($("cf-title"), opts.title);
  setText($("cf-body"), opts.body || "");
  setText($("cf-label"), `TYPE ${opts.word} TO CONFIRM`);
  setText($("cf-yes"), opts.action || opts.word);
  const input = $("cf-word");
  input.value = "";
  $("cf-yes").disabled = true;
  $("cf-no").disabled = false;
  setText($("cf-result"), "");
  setData($("cf-result"), "level", null);
  d.showModal();
  input.focus();
}

function typedOk() {
  return !!pending && $("cf-word").value.trim().toUpperCase() === pending.word;
}

async function go() {
  if (!typedOk() || pending.busy) return;
  const p = pending;
  const yes = $("cf-yes"), no = $("cf-no"), res = $("cf-result");
  p.busy = true;
  yes.disabled = true; no.disabled = true;
  setData(res, "level", null);
  setText(res, "SENDING…");
  try {
    const msg = await p.run(p.word);
    setData(res, "level", "ok");
    setText(res, msg || "DONE");
    p.done?.(msg);
    setTimeout(() => { if (pending === p && $("confirm").open) $("confirm").close(); }, 1600);
  } catch (e) {
    setData(res, "level", "bad");
    setText(res, e.message || "It didn't work.");
    p.failed?.(e);
    no.disabled = false;
    yes.disabled = !typedOk();
  } finally {
    p.busy = false;
  }
}
